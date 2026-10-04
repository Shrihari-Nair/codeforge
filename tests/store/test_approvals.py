import concurrent.futures
import time
import uuid

import pytest

from aeos.store import approvals


@pytest.fixture(autouse=True)
def schema(seeded_db: None) -> None:
    approvals.ensure_schema()


def make(ttl_s: float = 3600, task: str | None = None) -> approvals.ApprovalRequest:
    task = task or f"t-{uuid.uuid4().hex[:6]}"
    return approvals.create(
        task_id=task, action_key="open_pr", tool="create_pull_request", server="github",
        arguments={"title": "t", "head": "aeos/x"}, rationale="r", evidence={"k": 1}, ttl_s=ttl_s,
    )  # fmt: skip


def test_create_is_idempotent_per_task_and_action() -> None:
    task = f"t-same-{uuid.uuid4().hex[:6]}"  # fresh per run: the audit log is append-only
    a = make(task=task)
    b = make(task=task)
    assert a.id == b.id and a.status == "pending" and a.arguments_hash
    assert (
        len([e for e in approvals.audit_log(task) if e["event"] == "approval_requested"]) == 1
    )


def test_approve_records_actor_time_and_audit_entry() -> None:
    req = make()
    res = approvals.decide(req.id, "approved", "alice", "looks good")
    assert res.kind == "ok" and res.request is not None
    assert res.request.status == "approved" and res.request.actor == "alice"
    assert res.request.decided_at is not None and res.request.feedback == "looks good"
    events = [(e["actor"], e["event"]) for e in approvals.audit_log(req.task_id)]
    assert ("alice", "approval_approved") in events


def test_a_decision_applies_exactly_once() -> None:
    req = make()
    assert approvals.decide(req.id, "approved", "alice").kind == "ok"
    again = approvals.decide(req.id, "approved", "bob")
    assert again.kind == "already_decided" and again.request.actor == "alice"  # type: ignore[union-attr]
    flip = approvals.decide(req.id, "rejected", "bob")
    assert flip.kind == "already_decided" and flip.request.status == "approved"  # type: ignore[union-attr]


def test_concurrent_decisions_have_a_single_winner() -> None:
    req = make()
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda i: approvals.decide(req.id, "approved", f"u{i}"), range(8)))
    assert [r.kind for r in results].count("ok") == 1


def test_reject_with_feedback() -> None:
    req = make()
    res = approvals.decide(req.id, "rejected", "carol", "too risky")
    assert res.request is not None and res.request.status == "rejected"
    assert res.request.feedback == "too risky"


def test_expired_requests_cannot_be_approved() -> None:
    req = make(ttl_s=0.2)
    time.sleep(0.4)
    res = approvals.decide(req.id, "approved", "alice")
    assert res.kind == "expired" and res.request is not None and res.request.status == "expired"
    assert approvals.get(req.id).status == "expired"  # type: ignore[union-attr]
    assert approvals.decide(req.id, "approved", "alice").kind == "expired"  # stays expired
    assert any(e["event"] == "approval_expired" for e in approvals.audit_log(req.task_id))


def test_expire_due_sweeps_only_overdue_pending() -> None:
    old, fresh = make(ttl_s=0.1), make(ttl_s=3600)
    time.sleep(0.3)
    swept = {r.id for r in approvals.expire_due()}
    assert old.id in swept and fresh.id not in swept
    assert approvals.get(fresh.id).status == "pending"  # type: ignore[union-attr]


def test_unknown_id_and_listing() -> None:
    assert approvals.decide("ap-nope", "approved", "x").kind == "not_found"
    assert approvals.get("ap-nope") is None
    req = make()
    assert req.id in {r.id for r in approvals.list_requests(status="pending")}
    assert [r.id for r in approvals.list_requests(task_id=req.task_id)] == [req.id]
