"""Phase 6 acceptance: real approval pauses, durable resume from the exact checkpoint, rejection
with feedback, expiry, exactly-once decisions, and approval bound to the exact action."""

import asyncio
import json
import shutil
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from aeos.api.app import create_app
from aeos.core.config import Settings, get_settings
from aeos.core.events import ListSink
from aeos.demo.trajectories import (
    CODING,
    JUDGE,
    REPLAN,
    coding_script_good_fix,
    happy_path_llm,
    judge_ok,
    turn,
)
from aeos.graph.build import run_task
from aeos.graph.checkpoint import open_checkpointer
from aeos.hitl import auto_approver, scripted_approver
from aeos.llm.fake import FakeLLM, RoutedFakeLLM
from aeos.runtime import open_runtime
from aeos.store import approvals

pytestmark = pytest.mark.docker

TRUTH = json.loads((Path(__file__).parents[2] / "demo_target" / "GROUND_TRUTH.json").read_text())


@pytest.fixture
def settings(seeded_db: None, remote_template: Path) -> Settings:
    work = Path(tempfile.mkdtemp()) / "checkout-service.git"
    shutil.copytree(remote_template, work)
    return get_settings().model_copy(update={"git_remote_path": str(work)})


def db_scalar(sql: str, *params: Any) -> Any:
    with psycopg.connect(get_settings().database_url) as conn:
        row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


@asynccontextmanager
async def running_app(settings: Settings, llm: Any) -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    """An API 'process': its own runtime, its own MCP servers, same Postgres."""
    app = create_app(settings=settings, runtime_factory=lambda: open_runtime(settings, llm=llm),
                     checkpointer_factory=lambda: open_checkpointer(settings))  # fmt: skip
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://aeos") as client:
            yield client, app.state.ctx


async def wait_for(predicate: Any, limit_s: float = 180.0, every: float = 0.5) -> Any:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(every)
    raise AssertionError("timed out waiting for condition")


async def settle(client: httpx.AsyncClient, task_id: str) -> None:
    """Wait until the task is paused or finished (neither queued nor running)."""
    await asyncio.sleep(0.3)

    async def idle() -> bool:
        row = (await client.get(f"/tasks/{task_id}")).json()
        return row.get("queue_status") not in ("queued", "running")

    await wait_for(idle)


async def pending_for(client: httpx.AsyncClient, task_id: str) -> dict[str, Any] | None:
    r = await client.get("/approvals", params={"task_id": task_id, "status": "pending"})
    items = r.json()
    return items[0] if items else None


def silent_llm() -> RoutedFakeLLM:
    """A model that has nothing scripted: any LLM call after the restart would raise."""
    markers = ["ROLE: supervisor-planner", "ROLE: analytics", "ROLE: research",
               "ROLE: supervisor-synthesis", "ROLE: coding", "ROLE: verifier-judge",
               "ROLE: supervisor-replan"]  # fmt: skip
    return RoutedFakeLLM({m: FakeLLM([]) for m in markers})


# ============================================================ the headline acceptance test
async def test_run_pauses_process_dies_later_approval_resumes_without_repeating_work(
    settings,
) -> None:  # type: ignore[no-untyped-def]
    # ---- process 1: start the task and let it reach create_pull_request
    async with running_app(settings, happy_path_llm(TRUTH)) as (client, _):
        r = await client.post(
            "/tasks", json={"goal": "Investigate the checkout latency regression"}
        )
        assert r.status_code == 202
        task_id = r.json()["task_id"]
        approval = await wait_for(lambda: pending_for(client, task_id))
        before = (await client.get(f"/tasks/{task_id}")).json()
        assert before["status"] == "awaiting_approval"
        assert before["pending_approval_id"] == approval["id"]

    # ---- process 1 is gone. The approval and the checkpoint live in Postgres.
    assert db_scalar("SELECT status FROM aeos.approvals WHERE id = %s", approval["id"]) == "pending"
    assert (
        db_scalar(
            "SELECT count(*) FROM vcs.pull_requests WHERE idempotency_key IS NOT NULL "
            "AND head_branch LIKE %s",
            f"aeos/{task_id}%",
        )
        == 0
    )  # nothing published yet

    # ---- process 2: fresh runtime whose model has NOTHING scripted
    silent = silent_llm()
    async with running_app(settings, silent) as (client, _):
        pending = (await client.get("/approvals")).json()
        assert approval["id"] in [p["id"] for p in pending]  # still pending after the restart
        view = (await client.get(f"/tasks/{task_id}")).json()
        assert view["status"] == "awaiting_approval"
        assert "⏸ awaiting approval" in view["status_line"]
        ev = pending[0]["evidence"] if pending[0]["id"] == approval["id"] else approval["evidence"]
        assert (
            ev["gates"] and ev["patch"] and ev["benchmark"]["p95_change_pct"] <= 10
        )  # evidence shown

        r = await client.post(f"/approvals/{approval['id']}/approve", params={"wait": "true"},
                              headers={"X-Actor": "alice"}, json={"feedback": "ship it"})  # fmt: skip
        assert r.status_code == 200, r.text
        final = r.json()["task"]

    assert final["status"] == "completed", final["error"]
    assert final["pull_request"]["number"] >= 1 and final["pull_request"]["replayed"] is False
    # no repeated earlier work: the restarted model was never consulted...
    assert all(not llm.calls for llm in silent.routes.values())
    # ...and the only new side effect was the PR (exactly one for this task)
    prs = db_scalar(
        "SELECT count(*) FROM vcs.pull_requests WHERE head_branch LIKE %s", f"aeos/{task_id}%"
    )
    assert prs == 1
    # audit trail names the actor and the sequence
    events = [(e["actor"], e["event"]) for e in approvals.audit_log(task_id)]
    assert events == [("system", "approval_requested"), ("alice", "approval_approved"),
                      ("system", "pr_created")]  # fmt: skip
    # the policy decisions that governed it are recorded in the task state
    decisions = [(d["tool"], d["decision"], d["policy_rule"]) for d in final["risk_decisions"]]
    assert ("create_pull_request", "require_approval", "pr-needs-approval") in decisions
    assert any(rule.startswith("human_approved:") for _, _, rule in decisions)


# ============================================================ rejection -> revised plan
async def test_rejection_with_feedback_leads_to_a_revised_plan_and_a_new_pr(settings) -> None:  # type: ignore[no-untyped-def]
    llm = happy_path_llm(TRUTH)
    llm.routes[CODING] = FakeLLM(coding_script_good_fix() * 2)
    llm.routes[JUDGE] = FakeLLM([judge_ok(), judge_ok()])
    llm.routes[REPLAN] = FakeLLM([turn(("submit", {
        "reason": "Human rejected the PR asking for a regression test; redo the change with one.",
        "steps": [{"id": "s4", "owner": "coding", "description": "redo the fix and add a regression test"}],
    }))])  # fmt: skip
    sink = ListSink()
    approver = scripted_approver(
        {"decision": "rejected", "actor": "carol", "feedback": "please add a regression test"},
        {"decision": "approved", "actor": "carol", "feedback": ""},
    )
    async with (
        open_runtime(settings, llm=llm, sink=sink) as rt,
        open_checkpointer(settings) as saver,
    ):
        state = await run_task(rt, "Investigate the checkout latency regression",
                               checkpointer=saver, approver=approver)  # fmt: skip

    assert state.status == "completed", state.error
    assert [p.version for p in state.plans] == [1, 2]
    assert "regression test" in (state.plans[1].reason_for_change or "")
    assert [h["feedback"] for h in state.human_feedback] == ["please add a regression test"]
    # the rejection reached the replanner and the coding agent
    replan_prompt = llm.routes[REPLAN].calls[0][1].content
    assert "please add a regression test" in replan_prompt and "human_rejection" in replan_prompt
    assert "please add a regression test" in llm.routes[CODING].calls[-1][1].content
    # exactly one PR: for the revised branch, none for the rejected one
    task = state.task_id
    assert db_scalar("SELECT count(*) FROM vcs.pull_requests WHERE head_branch LIKE %s",
                     f"aeos/{task}%") == 1  # fmt: skip
    assert db_scalar("SELECT head_branch FROM vcs.pull_requests WHERE head_branch LIKE %s",
                     f"aeos/{task}%").endswith("-v2")  # fmt: skip
    events = [e["event"] for e in approvals.audit_log(task)]
    assert events == ["approval_requested", "approval_rejected", "approval_requested",
                      "approval_approved", "pr_created"]  # fmt: skip
    assert "replan" in sink.types()


async def test_repeated_human_rejections_escalate_instead_of_looping_forever(settings) -> None:  # type: ignore[no-untyped-def]
    llm = happy_path_llm(TRUTH)
    llm.routes[CODING] = FakeLLM(coding_script_good_fix() * 3)
    llm.routes[JUDGE] = FakeLLM([judge_ok()] * 3)
    llm.routes[REPLAN] = FakeLLM([
        turn(("submit", {"reason": "redo 1", "steps": [{"id": "s4", "owner": "coding", "description": "d"}]})),
        turn(("submit", {"reason": "redo 2", "steps": [
            {"id": "s7", "owner": "research", "description": "r"},
            {"id": "s8", "owner": "coding", "depends_on": ["s7"], "description": "d"}]})),
    ])  # fmt: skip
    llm.routes["ROLE: research"] = FakeLLM(
        happy_path_llm(TRUTH).routes["ROLE: research"]._script * 2  # type: ignore[attr-defined]
    )
    llm.routes["ROLE: supervisor-synthesis"] = FakeLLM(
        happy_path_llm(TRUTH).routes["ROLE: supervisor-synthesis"]._script * 2  # type: ignore[attr-defined]
    )
    no = {"decision": "rejected", "actor": "carol", "feedback": "no"}
    async with open_runtime(settings, llm=llm) as rt, open_checkpointer(settings) as saver:
        state = await run_task(
            rt, "Investigate", checkpointer=saver, approver=scripted_approver(no, no, no)
        )
    assert state.status == "escalated" and "human" in (state.error or "")
    assert state.artifacts.pull_request is None
    assert db_scalar("SELECT count(*) FROM vcs.pull_requests WHERE head_branch LIKE %s",
                     f"aeos/{state.task_id}%") == 0  # fmt: skip


# ============================================================ API behaviour
async def test_decisions_are_exactly_once_and_need_an_actor(settings) -> None:  # type: ignore[no-untyped-def]
    async with running_app(settings, happy_path_llm(TRUTH)) as (client, _):
        task_id = (await client.post("/tasks", json={"goal": "Investigate the regression"})).json()[
            "task_id"
        ]
        ap = await wait_for(lambda: pending_for(client, task_id))
        url = f"/approvals/{ap['id']}/approve"

        assert (await client.post(url, json={})).status_code == 400  # no X-Actor
        assert (await client.post(f"/approvals/{ap['id']}/reject", json={})).status_code == 400
        assert (
            await client.post("/approvals/ap-nope/approve", headers={"X-Actor": "a"}, json={})
        ).status_code == 404

        first = await client.post(
            url, headers={"X-Actor": "alice"}, json={}, params={"wait": "true"}
        )
        assert first.status_code == 200 and first.json()["task"]["status"] == "completed"
        second = await client.post(url, headers={"X-Actor": "bob"}, json={})
        flip = await client.post(
            f"/approvals/{ap['id']}/reject", headers={"X-Actor": "bob"}, json={}
        )
        assert second.status_code == 409 and flip.status_code == 409
        assert "alice" in second.json()["detail"]
    assert db_scalar("SELECT count(*) FROM vcs.pull_requests WHERE head_branch LIKE %s",
                     f"aeos/{task_id}%") == 1  # fmt: skip


async def test_concurrent_approvals_create_exactly_one_pull_request(settings) -> None:  # type: ignore[no-untyped-def]
    async with running_app(settings, happy_path_llm(TRUTH)) as (client, _):
        task_id = (await client.post("/tasks", json={"goal": "Investigate the regression"})).json()[
            "task_id"
        ]
        ap = await wait_for(lambda: pending_for(client, task_id))
        url = f"/approvals/{ap['id']}/approve"
        results = await asyncio.gather(*[
            client.post(url, headers={"X-Actor": f"u{i}"}, json={}) for i in range(5)])  # fmt: skip
        assert sorted(r.status_code for r in results).count(200) == 1
        await settle(client, task_id)
        view = (await client.get(f"/tasks/{task_id}")).json()
        assert view["status"] == "completed"
    assert db_scalar("SELECT count(*) FROM vcs.pull_requests WHERE head_branch LIKE %s",
                     f"aeos/{task_id}%") == 1  # fmt: skip


async def test_expired_approval_cannot_be_approved_and_the_task_escalates(settings) -> None:  # type: ignore[no-untyped-def]
    short = settings.model_copy(update={"approval_ttl_s": 1.0})
    async with running_app(short, happy_path_llm(TRUTH)) as (client, _):
        task_id = (await client.post("/tasks", json={"goal": "Investigate the regression"})).json()[
            "task_id"
        ]
        ap = await wait_for(lambda: pending_for(client, task_id))
        await asyncio.sleep(1.5)
        late = await client.post(
            f"/approvals/{ap['id']}/approve", headers={"X-Actor": "alice"}, json={}
        )
        assert late.status_code == 410
        await settle(client, task_id)
        await wait_for(lambda: _task_status(client, task_id, "escalated"))
        view = (await client.get(f"/tasks/{task_id}")).json()
        assert view["status"] == "escalated" and "expired" in view["error"]
    assert db_scalar("SELECT count(*) FROM vcs.pull_requests WHERE head_branch LIKE %s",
                     f"aeos/{task_id}%") == 0  # fmt: skip
    assert "approval_expired" in [e["event"] for e in approvals.audit_log(task_id)]


async def _task_status(client: httpx.AsyncClient, task_id: str, status: str) -> bool:
    return (await client.get(f"/tasks/{task_id}")).json()["status"] == status


async def test_sweeper_expires_overdue_approvals_and_resumes_their_tasks(settings) -> None:  # type: ignore[no-untyped-def]
    short = settings.model_copy(update={"approval_ttl_s": 1.0})
    async with running_app(short, happy_path_llm(TRUTH)) as (client, _):
        task_id = (await client.post("/tasks", json={"goal": "Investigate the regression"})).json()[
            "task_id"
        ]
        ap = await wait_for(lambda: pending_for(client, task_id))
        await asyncio.sleep(1.5)
        swept = (await client.post("/approvals/expire")).json()
        assert ap["id"] in swept["expired"]
        await settle(client, task_id)
        assert (await client.get(f"/tasks/{task_id}")).json()["status"] == "escalated"


# ============================================================ approval is bound to the action
async def test_tampering_with_the_approved_arguments_is_blocked(settings) -> None:  # type: ignore[no-untyped-def]
    llm = happy_path_llm(TRUTH)
    async with open_runtime(settings, llm=llm) as rt, open_checkpointer(settings) as saver:
        state = await run_task(rt, "Investigate the regression", checkpointer=saver)
        assert state.status == "awaiting_approval" and state.pending_approval_id
        from aeos.graph.build import resume_task
        from aeos.hitl import apply_decision

        req, payload = await apply_decision(state.pending_approval_id, "approved", "alice")
        # an attacker with DB access swaps the PR contents AFTER the human approved
        with psycopg.connect(get_settings().database_url) as conn:
            conn.execute("UPDATE aeos.approvals SET arguments = jsonb_set(arguments, '{title}', "
                         "'\"totally different pull request\"') WHERE id = %s", (req.id,))  # fmt: skip
        final = await resume_task(rt, state.task_id, payload, checkpointer=saver)
    assert final.status == "escalated" and "blocked" in (final.error or "")
    assert final.artifacts.pull_request is None
    assert db_scalar("SELECT count(*) FROM vcs.pull_requests WHERE head_branch LIKE %s",
                     f"aeos/{state.task_id}%") == 0  # fmt: skip


async def test_no_approver_means_the_run_stays_paused_not_completed(settings) -> None:  # type: ignore[no-untyped-def]
    async with (
        open_runtime(settings, llm=happy_path_llm(TRUTH)) as rt,
        open_checkpointer(settings) as saver,
    ):
        state = await run_task(rt, "Investigate the regression", checkpointer=saver)
    assert state.status == "awaiting_approval" and state.artifacts.pull_request is None
    assert state.artifacts.branch is not None  # the reversible work is done; the PR is not
    assert any(r.tool == "commit_patch" for r in state.tool_calls)
    assert not any(r.tool == "create_pull_request" for r in state.tool_calls)


async def test_auto_approver_completes_the_run(settings) -> None:  # type: ignore[no-untyped-def]
    async with (
        open_runtime(settings, llm=happy_path_llm(TRUTH)) as rt,
        open_checkpointer(settings) as saver,
    ):
        state = await run_task(
            rt, "Investigate the regression", checkpointer=saver, approver=auto_approver
        )
    assert state.status == "completed" and state.artifacts.pull_request is not None


async def test_stats_endpoint_reports_cost_tools_and_approval_wait(settings) -> None:  # type: ignore[no-untyped-def]
    async with running_app(settings, happy_path_llm(TRUTH)) as (client, _):
        task_id = (await client.post("/tasks", json={"goal": "Investigate the regression"})).json()[
            "task_id"
        ]
        ap = await wait_for(lambda: pending_for(client, task_id))
        paused = (await client.get(f"/tasks/{task_id}/stats")).json()
        assert paused["status"] == "awaiting_approval" and paused["approvals"]["requested"] == 1
        await client.post(f"/approvals/{ap['id']}/approve", headers={"X-Actor": "alice"}, json={},
                          params={"wait": "true"})  # fmt: skip
        stats = (await client.get(f"/tasks/{task_id}/stats")).json()
        assert (await client.get("/tasks/t-nope/stats")).status_code == 404
    assert stats["status"] == "completed" and stats["duration_s"] > 0
    assert stats["llm"]["calls"] > 10 and stats["llm"]["cost_usd"] > 0
    assert {"observability", "github", "sandbox"} <= set(stats["tools"]["by_server"])
    assert stats["approvals"]["decided"] == 1 and stats["approvals"]["total_wait_s"] >= 0
    assert stats["replans"] == 0 and stats["verifications"] == {"total": 1, "failed": 0}


async def test_events_are_persisted_replayable_and_streamable(settings) -> None:  # type: ignore[no-untyped-def]
    """The dashboard's timeline: events survive a refresh and can be followed live over SSE.
    (Needs a REAL http server: httpx's in-memory ASGI transport buffers streaming responses.)"""
    import socket
    import threading

    import uvicorn

    from aeos.store.events import DbSink

    def factory():  # type: ignore[no-untyped-def]
        return open_runtime(settings, llm=happy_path_llm(TRUTH), sink=DbSink())

    app = create_app(settings=settings, runtime_factory=factory,
                     checkpointer_factory=lambda: open_checkpointer(settings))  # fmt: skip
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.1)
        async with httpx.AsyncClient(base_url=base, timeout=30) as client:
            task_id = (
                await client.post("/tasks", json={"goal": "Investigate the regression"})
            ).json()["task_id"]
            await wait_for(lambda: pending_for(client, task_id))
            events = (await client.get(f"/tasks/{task_id}/events")).json()
            types = [e["type"] for e in events]
            assert {"plan_created", "step_started", "verification", "approval_requested"} <= set(
                types
            )
            assert [e["id"] for e in events] == sorted(
                e["id"] for e in events
            )  # ordered, resumable
            mid = events[len(events) // 2]["id"]
            tail = (await client.get(f"/tasks/{task_id}/events", params={"after": mid})).json()
            assert tail and tail[0]["id"] > mid  # reconnect with a cursor loses nothing

            got: list[dict[str, Any]] = []
            async with client.stream(
                "GET", f"/tasks/{task_id}/events/stream", params={"after": mid}
            ) as r:
                assert r.headers["content-type"].startswith("text/event-stream")
                async for line in r.aiter_lines():
                    if line.startswith("data: "):
                        got.append(json.loads(line[6:]))
                    if len(got) >= 3:
                        break
            assert [g["id"] for g in got] == [
                e["id"] for e in tail[:3]
            ]  # exactly the tail, in order

            # a LIVE follow: open the stream at the end, approve, and see the new events arrive
            last = events[-1]["id"]
            ap = (await client.get("/approvals", params={"task_id": task_id})).json()[0]["id"]
            live: list[str] = []

            async def follow() -> None:
                async with client.stream(
                    "GET", f"/tasks/{task_id}/events/stream", params={"after": last}
                ) as r2:
                    async for line in r2.aiter_lines():
                        if line.startswith("data: "):
                            live.append(json.loads(line[6:])["type"])
                            if "task_completed" in live:
                                return

            follower = asyncio.create_task(follow())
            await asyncio.sleep(0.5)
            await client.post(f"/approvals/{ap}/approve", headers={"X-Actor": "alice"}, json={})
            await asyncio.wait_for(follower, 90)
            assert (
                "approval_decided" in live and "pr_created" in live and live[-1] == "task_completed"
            )

            meta = (await client.get("/meta")).json()
            assert meta["langfuse_url"].startswith("http") and "approval_ttl_s" in meta
            detail = (await client.get(f"/tasks/{task_id}")).json()
            assert detail["tool_calls"] and detail["observations"] and detail["artifacts"]["patch"]
            cors = await client.options("/tasks", headers={"Origin": "http://localhost:3100",
                                                           "Access-Control-Request-Method": "POST"})  # fmt: skip
            assert cors.headers.get("access-control-allow-origin") == "http://localhost:3100"
            denied = await client.options("/tasks", headers={"Origin": "http://evil.example",
                                                             "Access-Control-Request-Method": "POST"})  # fmt: skip
            assert "access-control-allow-origin" not in denied.headers
    finally:
        server.should_exit = True
        thread.join(timeout=15)
