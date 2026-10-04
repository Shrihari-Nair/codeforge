"""Worker: claims tasks with a lease, runs the graph, survives crashes.

If a worker is killed (kill -9) its lease expires and any worker re-claims the task, which
resumes from the last Postgres checkpoint. Side effects are made safe by idempotency keys.
"""

import asyncio
import contextlib
import logging
import os
import uuid
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from aeos.core.state import Budget, TaskState
from aeos.graph.build import Approver, _config, build_graph, drive
from aeos.runtime import Runtime
from aeos.store import approvals, tasks

log = logging.getLogger("aeos.worker")


class Worker:
    def __init__(
        self,
        rt: Runtime,
        saver: BaseCheckpointSaver[Any],
        *,
        worker_id: str | None = None,
        lease_s: float = 30.0,
        poll_s: float = 1.0,
        sweep_s: float = 10.0,
        max_attempts: int = 5,
        reap_grace_s: float = 120.0,
        approver: Approver | None = None,
    ) -> None:
        self.rt, self.saver = rt, saver
        self.worker_id = worker_id or f"w-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.lease_s, self.poll_s, self.sweep_s = lease_s, poll_s, sweep_s
        self.max_attempts, self.approver = max_attempts, approver
        self.reap_grace_s = reap_grace_s

    # ------------------------------------------------------------------ loop
    async def run_forever(self, stop: asyncio.Event) -> None:
        await asyncio.to_thread(tasks.ensure_schema)
        await asyncio.to_thread(approvals.ensure_schema)
        last_sweep = 0.0
        while not stop.is_set():
            loop_time = asyncio.get_running_loop().time()
            if loop_time - last_sweep >= self.sweep_s:
                last_sweep = loop_time
                await self.housekeeping()
            if not await self.run_once():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_s)

    async def housekeeping(self) -> None:
        """Expire overdue approvals (waking their tasks) and reap orphaned sandboxes."""
        try:
            await asyncio.to_thread(approvals.expire_due)
            active = await asyncio.to_thread(tasks.active_task_ids)
            await asyncio.to_thread(
                self.rt.sandbox.reap, active_tasks=active, grace_s=self.reap_grace_s
            )
        except Exception:
            log.exception("housekeeping failed")

    async def run_once(self) -> bool:
        row = await asyncio.to_thread(tasks.claim, self.worker_id, self.lease_s, self.max_attempts)
        if row is None:
            return False
        log.info("claimed task", extra={"task_id": row.task_id, "attempt": row.attempts})
        work = asyncio.create_task(self._execute(row))
        beat = asyncio.create_task(self._heartbeat(row.task_id, work))
        try:
            status, error = await work
        except asyncio.CancelledError:
            log.warning("lost lease or cancelled", extra={"task_id": row.task_id})
            return True
        except Exception as exc:
            log.exception("task run failed", extra={"task_id": row.task_id})
            await asyncio.to_thread(
                tasks.release_with_error, row.task_id, self.worker_id, repr(exc)[:500]
            )
            return True
        finally:
            beat.cancel()
        await asyncio.to_thread(tasks.finish, row.task_id, self.worker_id, status, error)
        if status != "paused":  # terminal: nothing may still be running on its behalf
            await asyncio.to_thread(self.rt.sandbox.reap_task, row.task_id)
        return True

    async def _heartbeat(self, task_id: str, work: asyncio.Task[Any]) -> None:
        while True:
            await asyncio.sleep(self.lease_s / 3)
            alive = await asyncio.to_thread(tasks.heartbeat, task_id, self.worker_id, self.lease_s)
            if not alive:  # fenced: someone else owns this task now
                work.cancel()
                return

    # ------------------------------------------------------------------ one task
    async def _execute(self, row: tasks.TaskRow) -> tuple[str, str | None]:
        graph = build_graph(self.rt, self.saver)
        config = _config(row.task_id)
        snap = await graph.aget_state(config)
        first: Any = None
        if not snap.values:  # never started
            budget = Budget(**row.budget) if row.budget else Budget()
            first = await graph.ainvoke(
                TaskState(
                    task_id=row.task_id,
                    goal=row.goal,
                    budget=budget,
                    kind=row.kind,
                ),
                config,
            )
        elif snap.interrupts:  # paused at an approval
            if row.pending_resume:
                payload = row.pending_resume
                first = await graph.ainvoke(Command(resume=payload), config)
                await asyncio.to_thread(tasks.consumed_resume, row.task_id, self.worker_id, payload)
        elif snap.next:  # crashed mid-run: continue from the last checkpoint
            state = TaskState.model_validate(snap.values)
            log.info("resuming after crash: " + state.status_line(), extra={"task_id": row.task_id})
            first = await graph.ainvoke(None, config)
        state = await drive(graph, row.task_id, first, self.approver)
        if state.status == "awaiting_approval":
            return "paused", None
        return state.status, state.error
