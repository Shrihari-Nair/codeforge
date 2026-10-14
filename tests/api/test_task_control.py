"""Cancel and retry: store behaviour plus the thin HTTP layer (no worker, no LLM)."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from aeos.api.app import create_app
from aeos.core.config import get_settings
from aeos.store import approvals, tasks


@pytest.fixture(autouse=True)
def schema(seeded_db: None) -> None:
    tasks.ensure_schema()
    approvals.ensure_schema()
    with psycopg.connect(get_settings().database_url) as conn:
        conn.execute("DELETE FROM aeos.tasks")


def tid() -> str:
    return f"t-{uuid.uuid4().hex[:6]}"


class FakeSandbox:
    def __init__(self) -> None:
        self.reaped: list[str] = []

    def reap_task(self, task_id: str) -> None:
        self.reaped.append(task_id)


@pytest.fixture
def client() -> tuple[TestClient, FakeSandbox]:
    sandbox = FakeSandbox()

    @asynccontextmanager
    async def runtime() -> AsyncIterator[Any]:
        yield SimpleNamespace(sandbox=sandbox)

    @asynccontextmanager
    async def saver() -> AsyncIterator[Any]:
        yield MemorySaver()

    app = create_app(runtime_factory=runtime, checkpointer_factory=saver, embedded_worker=False)
    with TestClient(app) as c:
        yield c, sandbox


def test_cancel_queued_task_is_final_and_fences_a_worker() -> None:
    t = tid()
    tasks.enqueue(t, "goal")
    claimed = tasks.claim("w1", 30)
    assert claimed is not None
    row = tasks.cancel(t, "cancelled by Shrihari")
    assert row is not None and row.status == "cancelled" and row.worker_id is None
    assert tasks.heartbeat(t, "w1", 30) is False  # the running worker is told to stop
    assert tasks.cancel(t, "again") is None  # already final
    assert tasks.claim("w2", 30) is None  # and never picked up again


def test_cancel_endpoint_closes_approval_audits_and_reaps(
    client: tuple[TestClient, FakeSandbox],
) -> None:
    c, sandbox = client
    t = tid()
    tasks.enqueue(t, "goal")
    req = approvals.create(
        task_id=t, action_key="k", tool="create_pull_request", server="github", arguments={},
        rationale="r",
        evidence={}, ttl_s=60,
    )  # fmt: skip
    assert c.post(f"/tasks/{t}/cancel").status_code == 400  # needs an actor
    r = c.post(f"/tasks/{t}/cancel", headers={"X-Actor": "Shrihari"})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert approvals.get(req.id).status == "expired"  # type: ignore[union-attr]
    assert sandbox.reaped == [t]
    log = [(a["actor"], a["event"]) for a in approvals.audit_log(t)]
    assert ("Shrihari", "task_cancelled") in log
    assert c.post(f"/tasks/{t}/cancel", headers={"X-Actor": "Shrihari"}).status_code == 409
    assert c.post("/tasks/t-nope/cancel", headers={"X-Actor": "Shrihari"}).status_code == 404


def test_retry_makes_a_new_task_only_for_finished_ones(
    client: tuple[TestClient, FakeSandbox],
) -> None:
    c, _ = client
    t = tid()
    tasks.enqueue(t, "fix the thing", {"max_cost_usd": 1.5}, "change")
    assert c.post(f"/tasks/{t}/retry").status_code == 409  # still queued
    tasks.cancel(t, "x")
    r = c.post(f"/tasks/{t}/retry")
    assert r.status_code == 202 and r.json()["retry_of"] == t
    new = tasks.get(r.json()["task_id"])
    assert new is not None and new.goal == "fix the thing" and new.kind == "change"
    assert new.budget == {"max_cost_usd": 1.5} and new.status == "queued"
    assert c.post("/tasks/t-nope/retry").status_code == 404
