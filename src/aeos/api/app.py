"""HTTP API: submit tasks, inspect them, list/approve/reject pending approvals.

Tasks go through the durable queue (`aeos.tasks`). Workers (embedded here, or separate
`aeos worker` processes) run them, so the API process can restart without losing anything.

Authentication/RBAC is added in Phase 13; until then decisions require an `X-Actor` header so
every decision has a named actor in the audit log.
"""

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import BaseModel, Field

from aeos.core.config import Settings, get_settings
from aeos.core.state import Budget, TaskState
from aeos.core.stats import task_stats
from aeos.graph.build import _snapshot, build_graph
from aeos.graph.checkpoint import open_checkpointer
from aeos.hitl import DecisionError, apply_decision
from aeos.runtime import Runtime, open_runtime
from aeos.store import approvals, events, tasks
from aeos.worker import Worker

log = logging.getLogger("aeos.api")

RuntimeFactory = Callable[[], AbstractAsyncContextManager[Runtime]]
CheckpointerFactory = Callable[[], AbstractAsyncContextManager[BaseCheckpointSaver[Any]]]


class TaskRequest(BaseModel):
    goal: str = Field(min_length=3, max_length=4000)
    max_cost_usd: float | None = Field(default=None, gt=0, le=50)
    # regression: investigate a latency regression in the seeded demo repo.
    # change: any code change request (e.g. dependency upgrade) on the real target repo.
    kind: Literal["regression", "change"] | None = None


class DecisionRequest(BaseModel):
    feedback: str | None = Field(default=None, max_length=2000)


class AppContext:
    def __init__(self, rt: Runtime, saver: BaseCheckpointSaver[Any]) -> None:
        self.rt, self.saver = rt, saver
        self.worker: Worker | None = None
        self.worker_task: asyncio.Task[None] | None = None
        self.stop = asyncio.Event()


def task_view(state: TaskState, row: tasks.TaskRow | None = None) -> dict[str, Any]:
    art = state.artifacts
    return {
        "task_id": state.task_id,
        "goal": state.goal,
        "kind": state.kind,
        "status": state.status,
        "queue_status": row.status if row else None,
        "attempts": row.attempts if row else None,
        "status_line": state.status_line(),
        "error": state.error,
        "plan_version": state.plan.version if state.plan else None,
        "plans": [
            {
                "version": p.version,
                "reason_for_change": p.reason_for_change,
                "steps": [s.model_dump() for s in p.steps],
            }
            for p in state.plans
        ],  # fmt: skip
        "pending_approval_id": state.pending_approval_id,
        "root_cause": art.root_cause.model_dump() if art.root_cause else None,
        "pull_request": art.pull_request,
        "verifications": [v.model_dump() for v in state.verifications],
        "risk_decisions": [d.model_dump() for d in state.risk_decisions],
        "budget": state.budget.model_dump(),
        "tool_calls": [t.model_dump() for t in state.tool_calls],
        "observations": [o.model_dump() for o in state.observations],
        "human_feedback": state.human_feedback,
        "memory_trace": state.memory_trace,
        "artifacts": {
            "patch": art.patch,
            "report": art.report,
            "baseline_ref": art.baseline_ref,
            "research": art.research,
            "base_ref": art.base_ref,
            "branch": art.branch,
            "rejected_root_causes": [r.model_dump() for r in art.rejected_root_causes],
        },
        "trace_id": state.trace.get("traceparent", "").split("-")[1]
        if state.trace.get("traceparent")
        else None,
    }


def create_app(
    *,
    settings: Settings | None = None,
    runtime_factory: RuntimeFactory | None = None,
    checkpointer_factory: CheckpointerFactory | None = None,
    embedded_worker: bool = True,
    poll_s: float = 0.2,
) -> FastAPI:
    s = settings or get_settings()
    make_rt = runtime_factory or (lambda: open_runtime(s))
    make_saver = checkpointer_factory or (lambda: open_checkpointer(s))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(approvals.ensure_schema)
        await asyncio.to_thread(tasks.ensure_schema)
        await asyncio.to_thread(events.ensure_schema)
        async with make_rt() as rt, make_saver() as saver:
            ctx = AppContext(rt, saver)
            app.state.ctx = ctx
            if embedded_worker:
                ctx.worker = Worker(rt, saver, poll_s=poll_s, sweep_s=5.0)
                ctx.worker_task = asyncio.create_task(ctx.worker.run_forever(ctx.stop))
            try:
                yield
            finally:
                ctx.stop.set()
                if ctx.worker_task:
                    ctx.worker_task.cancel()  # like a crashed worker: its lease will expire
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await ctx.worker_task

    app = FastAPI(title="AEOS", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in s.cors_origins.split(",") if o.strip()],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    def ctx() -> AppContext:
        c: AppContext = app.state.ctx
        return c

    async def view(task_id: str) -> dict[str, Any]:
        c = ctx()
        row = await asyncio.to_thread(tasks.get, task_id)
        if row is None:
            raise HTTPException(404, f"no task {task_id}")
        graph = build_graph(c.rt, c.saver)
        snap = await graph.aget_state({"configurable": {"thread_id": task_id}})
        if not snap.values:  # queued, not started yet
            return {"task_id": task_id, "goal": row.goal, "status": row.status,
                    "queue_status": row.status, "attempts": row.attempts, "status_line":
                    f"Task {task_id} [{row.status}]"}  # fmt: skip
        state, _ = await _snapshot(graph, task_id)
        return task_view(state, row)

    async def wait_idle(task_id: str, limit_s: float = 600.0) -> None:
        """Wait until the task is paused or finished (not queued/running)."""
        deadline = time.monotonic() + limit_s
        await asyncio.sleep(0.05)
        while time.monotonic() < deadline:
            row = await asyncio.to_thread(tasks.get, task_id)
            if row is None or row.status not in ("queued", "running"):
                return
            await asyncio.sleep(0.2)

    # ------------------------------------------------------------------ tasks
    @app.post("/tasks", status_code=202)
    async def create_task(req: TaskRequest) -> dict[str, str]:
        task_id = f"t-{uuid.uuid4().hex[:8]}"
        budget = Budget(max_cost_usd=req.max_cost_usd).model_dump() if req.max_cost_usd else None
        kind = req.kind or ("change" if s.target_ready else "regression")
        if kind == "change" and not s.target_ready:
            raise HTTPException(409, "change tasks need AEOS_GITHUB_REPO and AEOS_GITHUB_TOKEN")
        await asyncio.to_thread(tasks.enqueue, task_id, req.goal, budget, kind)
        return {"task_id": task_id}

    @app.get("/tasks")
    async def list_tasks(status: str | None = None) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(tasks.list_tasks, status)
        return [r.model_dump(mode="json") for r in rows]

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        return await view(task_id)

    @app.get("/tasks/{task_id}/stats")
    async def get_stats(task_id: str) -> dict[str, Any]:
        """Cost, tokens, latency, tool use, approvals, retries and replans for one task."""
        c = ctx()
        row = await asyncio.to_thread(tasks.get, task_id)
        if row is None:
            raise HTTPException(404, f"no task {task_id}")
        graph = build_graph(c.rt, c.saver)
        snap = await graph.aget_state({"configurable": {"thread_id": task_id}})
        if not snap.values:
            raise HTTPException(409, "task has not started yet")
        state, _ = await _snapshot(graph, task_id)
        reqs = await asyncio.to_thread(approvals.list_requests, None, task_id)
        return task_stats(state, reqs)

    @app.get("/tasks/{task_id}/audit")
    async def get_audit(task_id: str) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(approvals.audit_log, task_id)
        return [{**r, "ts": r["ts"].isoformat()} for r in rows]

    # ------------------------------------------------------------------ approvals
    @app.get("/approvals")
    async def list_approvals(
        status: str | None = Query(default="pending"), task_id: str | None = None
    ) -> list[dict[str, Any]]:
        reqs = await asyncio.to_thread(approvals.list_requests, status, task_id)
        return [r.model_dump(mode="json") for r in reqs]

    @app.get("/approvals/{approval_id}")
    async def get_approval(approval_id: str) -> dict[str, Any]:
        req = await asyncio.to_thread(approvals.get, approval_id)
        if req is None:
            raise HTTPException(404, f"no approval {approval_id}")
        return req.model_dump(mode="json")

    async def decide(
        approval_id: str, decision: str, actor: str | None, body: DecisionRequest, wait: bool
    ) -> dict[str, Any]:
        if not actor or not actor.strip():
            raise HTTPException(400, "X-Actor header is required to record who decided")
        try:
            req, _ = await apply_decision(
                approval_id,
                decision,  # type: ignore[arg-type]
                actor.strip(),
                body.feedback,
            )
        except DecisionError as exc:
            status = {"not_found": 404, "expired": 410, "already_decided": 409}[exc.code]
            raise HTTPException(status, exc.message) from exc
        # The decision and the resume request were committed together; a worker will pick it up.
        out: dict[str, Any] = {"approval": req.model_dump(mode="json"), "task_id": req.task_id}
        if wait:
            await wait_idle(req.task_id)
            out["task"] = await view(req.task_id)
        else:
            out["status"] = "resuming"
        return out

    @app.post("/approvals/{approval_id}/approve")
    async def approve(
        approval_id: str,
        body: DecisionRequest | None = None,
        x_actor: str | None = Header(default=None),
        wait: bool = False,
    ) -> dict[str, Any]:
        return await decide(approval_id, "approved", x_actor, body or DecisionRequest(), wait)

    @app.post("/approvals/{approval_id}/reject")
    async def reject(
        approval_id: str,
        body: DecisionRequest | None = None,
        x_actor: str | None = Header(default=None),
        wait: bool = False,
    ) -> dict[str, Any]:
        return await decide(approval_id, "rejected", x_actor, body or DecisionRequest(), wait)

    @app.post("/approvals/expire")
    async def expire_overdue() -> dict[str, Any]:
        """Sweep overdue approvals; each paused task is woken so it can escalate cleanly."""
        expired = await asyncio.to_thread(approvals.expire_due)
        return {"expired": [r.id for r in expired]}

    # ------------------------------------------------------------------ events (timeline)
    @app.get("/tasks/{task_id}/events")
    async def get_events(task_id: str, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(events.list_events, task_id, after, min(limit, 2000))
        return [e.model_dump(mode="json") for e in rows]

    @app.get("/tasks/{task_id}/events/stream")
    async def stream_events(task_id: str, request: Request, after: int = 0) -> StreamingResponse:
        """Server-Sent Events: replays history after `after`, then follows live. Reconnect-safe:
        the browser sends its last event id, so a refresh or dropped connection loses nothing."""
        last_header = request.headers.get("last-event-id")
        cursor = int(last_header) if last_header and last_header.isdigit() else after

        async def gen() -> AsyncIterator[str]:
            nonlocal cursor
            idle = 0.0
            while not await request.is_disconnected():
                rows = await asyncio.to_thread(events.list_events, task_id, cursor, 200)
                for e in rows:
                    cursor = e.id
                    yield f"id: {e.id}\nevent: aeos\ndata: {e.model_dump_json()}\n\n"
                if rows:
                    idle = 0.0
                else:
                    idle += 0.5
                    if idle >= 15:  # keep-alive so proxies do not drop the connection
                        idle = 0.0
                        yield ": keep-alive\n\n"
                    await asyncio.sleep(0.5)

        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)

    @app.get("/meta")
    async def meta() -> dict[str, Any]:
        """What the dashboard needs to know about this deployment."""
        return {
            "langfuse_url": s.langfuse_public_url,
            "a2a_mode": s.a2a_mode,
            "llm_provider": s.llm_provider,
            "memory_enabled": s.memory_enabled,
            "approval_ttl_s": s.approval_ttl_s,
            "target_repo": s.github_repo if s.target_ready else None,
            "github_publish": s.github_publish,
        }

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
