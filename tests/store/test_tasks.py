import concurrent.futures
import time
import uuid

import psycopg
import pytest

from aeos.core.config import get_settings
from aeos.store import approvals, tasks


@pytest.fixture(autouse=True)
def schema(seeded_db: None) -> None:
    tasks.ensure_schema()
    approvals.ensure_schema()
    with psycopg.connect(get_settings().database_url) as conn:
        conn.execute("DELETE FROM aeos.tasks")  # these tests own the queue


def tid() -> str:
    return f"t-{uuid.uuid4().hex[:6]}"


def test_enqueue_is_idempotent_and_claim_marks_running() -> None:
    t = tid()
    a, b = tasks.enqueue(t, "goal"), tasks.enqueue(t, "goal")
    assert a.task_id == b.task_id and a.status == "queued"
    row = tasks.claim("w1", 30)
    assert row is not None and row.task_id == t and row.status == "running"
    assert row.worker_id == "w1" and row.attempts == 1 and row.lease_expires_at is not None
    assert tasks.claim("w2", 30) is None  # nothing else to run


def test_only_one_of_many_concurrent_workers_claims_a_task() -> None:
    tasks.enqueue(tid(), "goal")
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda i: tasks.claim(f"w{i}", 30), range(8)))
    assert len([r for r in results if r is not None]) == 1


def test_tasks_are_claimed_oldest_first() -> None:
    first, second = tid(), tid()
    tasks.enqueue(first, "g")
    time.sleep(0.01)
    tasks.enqueue(second, "g")
    assert tasks.claim("w", 30).task_id == first  # type: ignore[union-attr]
    assert tasks.claim("w", 30).task_id == second  # type: ignore[union-attr]


def test_dead_worker_lease_expires_and_another_worker_takes_over() -> None:
    t = tid()
    tasks.enqueue(t, "g")
    assert tasks.claim("dead-worker", 0.2) is not None
    assert tasks.claim("w2", 30) is None  # lease still valid
    time.sleep(0.4)  # the worker was kill -9'd and never heartbeated
    row = tasks.claim("w2", 30)
    assert row is not None and row.task_id == t and row.worker_id == "w2" and row.attempts == 2


def test_heartbeat_renews_and_a_fenced_worker_is_told_to_stop() -> None:
    t = tid()
    tasks.enqueue(t, "g")
    tasks.claim("w1", 0.3)
    assert tasks.heartbeat(t, "w1", 5)
    time.sleep(0.5)
    assert tasks.claim("w2", 30) is None  # renewed lease protects it
    # force expiry, let w2 steal it, then w1 (a zombie) tries to heartbeat/finish
    with psycopg.connect(get_settings().database_url) as conn:
        conn.execute("UPDATE aeos.tasks SET lease_expires_at = now() - interval '1 second'")
    assert tasks.claim("w2", 30) is not None
    assert tasks.heartbeat(t, "w1", 5) is False
    assert tasks.finish(t, "w1", "completed") is None  # fenced: cannot overwrite w2's task
    assert tasks.get(t).status == "running"  # type: ignore[union-attr]


def test_finish_records_outcomes() -> None:
    t = tid()
    tasks.enqueue(t, "g")
    tasks.claim("w1", 30)
    row = tasks.finish(t, "w1", "completed")
    assert row is not None and row.status == "completed" and row.worker_id is None
    assert tasks.claim("w2", 30) is None  # terminal tasks are not re-run


def test_crash_loop_guard_escalates_a_task_that_keeps_killing_workers() -> None:
    t = tid()
    tasks.enqueue(t, "g")
    for i in range(3):
        assert tasks.claim(f"w{i}", 0.05, max_attempts=3) is not None
        time.sleep(0.1)  # each worker dies
    assert tasks.claim("w9", 30, max_attempts=3) is None
    row = tasks.get(t)
    assert (
        row is not None and row.status == "escalated" and "needs a human" in (row.last_error or "")
    )


def test_a_decision_that_races_the_pause_is_not_lost() -> None:
    """Approve arrives while the worker is still winding down (task still 'running')."""
    t = tid()
    tasks.enqueue(t, "g")
    tasks.claim("w1", 30)
    req = approvals.create(task_id=t, action_key="open_pr", tool="create_pull_request",
                           server="github", arguments={"title": "x"}, rationale="r",
                           evidence={})  # fmt: skip
    assert approvals.decide(req.id, "approved", "alice").kind == "ok"  # task is 'running' here
    assert tasks.get(t).pending_resume["decision"] == "approved"  # type: ignore[union-attr,index]
    row = tasks.finish(t, "w1", "paused")  # worker now reports the pause
    assert row is not None and row.status == "queued"  # ...and the task is woken, not stranded


def test_decision_on_an_already_paused_task_wakes_it_atomically() -> None:
    t = tid()
    tasks.enqueue(t, "g")
    tasks.claim("w1", 30)
    tasks.finish(t, "w1", "paused")
    assert tasks.get(t).status == "paused"  # type: ignore[union-attr]
    req = approvals.create(task_id=t, action_key="open_pr", tool="create_pull_request",
                           server="github", arguments={"title": "x"}, rationale="r",
                           evidence={})  # fmt: skip
    approvals.decide(req.id, "rejected", "bob", "no thanks")
    row = tasks.get(t)
    assert row is not None and row.status == "queued"
    assert row.pending_resume == {"decision": "rejected", "actor": "bob", "feedback": "no thanks",
                                  "approval_id": req.id}  # fmt: skip


def test_expiry_wakes_the_task_with_an_expired_payload() -> None:
    t = tid()
    tasks.enqueue(t, "g")
    tasks.claim("w1", 30)
    tasks.finish(t, "w1", "paused")
    req = approvals.create(task_id=t, action_key="open_pr", tool="create_pull_request",
                           server="github", arguments={}, rationale="r", evidence={}, ttl_s=0.1)  # fmt: skip
    time.sleep(0.3)
    approvals.expire_due()
    row = tasks.get(t)
    assert row is not None and row.status == "queued"
    assert (
        row.pending_resume["decision"] == "expired" and row.pending_resume["approval_id"] == req.id
    )  # type: ignore[index]


def test_consumed_resume_only_clears_the_payload_that_was_delivered() -> None:
    t = tid()
    tasks.enqueue(t, "g")
    tasks.claim("w1", 30)
    old = {"decision": "rejected", "approval_id": "ap-1"}
    new = {"decision": "approved", "approval_id": "ap-2"}
    with psycopg.connect(get_settings().database_url) as conn:
        tasks.enqueue_resume(conn, t, new)  # a newer decision arrived while we processed `old`
    tasks.consumed_resume(t, "w1", old)
    assert tasks.get(t).pending_resume == new  # type: ignore[union-attr]
    tasks.consumed_resume(t, "w1", new)
    assert tasks.get(t).pending_resume is None  # type: ignore[union-attr]


def test_active_task_ids_covers_queued_running_paused_only() -> None:
    a, b, c = tid(), tid(), tid()
    for t in (a, b, c):
        tasks.enqueue(t, "g")
    tasks.claim("w", 30)  # a -> running
    tasks.claim("w", 30)  # b -> running
    tasks.finish(b, "w", "paused")
    tasks.finish(a, "w", "completed")
    assert tasks.active_task_ids() == {b, c}
