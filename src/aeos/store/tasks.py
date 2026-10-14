"""Task queue with leases (Postgres schema `aeos`).

A worker claims a task with a lease and renews it by heartbeat. If the worker dies (kill -9), the
lease expires and any worker re-claims the task; it resumes from the last checkpoint. Writes by a
worker that lost its lease are fenced out (`worker_id` must still match).
"""

from datetime import datetime
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from aeos.core.config import get_settings

DDL = """
CREATE SCHEMA IF NOT EXISTS aeos;
CREATE TABLE IF NOT EXISTS aeos.tasks (
    task_id text PRIMARY KEY,
    goal text NOT NULL,
    budget jsonb,
    status text NOT NULL DEFAULT 'queued',
    worker_id text,
    lease_expires_at timestamptz,
    heartbeat_at timestamptz,
    attempts int NOT NULL DEFAULT 0,
    pending_resume jsonb,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tasks_status_idx ON aeos.tasks (status);
ALTER TABLE aeos.tasks ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'regression';
"""

TaskStatus = Literal["queued", "running", "paused", "completed", "failed", "escalated", "cancelled"]
TERMINAL = ("completed", "failed", "escalated", "cancelled")


class TaskRow(BaseModel):
    task_id: str
    goal: str
    kind: str = "regression"
    budget: dict[str, Any] | None = None
    status: str
    worker_id: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    attempts: int = 0
    pending_resume: dict[str, Any] | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


def _connect() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(get_settings().database_url, row_factory=dict_row)


def ensure_schema() -> None:
    with _connect() as conn:
        conn.execute(DDL)


def enqueue(
    task_id: str, goal: str, budget: dict[str, Any] | None = None, kind: str = "regression"
) -> TaskRow:
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO aeos.tasks (task_id, goal, budget, kind) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (task_id) DO NOTHING RETURNING *",
            (task_id, goal, Jsonb(budget) if budget else None, kind),
        ).fetchone()
        if row is None:
            row = conn.execute("SELECT * FROM aeos.tasks WHERE task_id = %s", (task_id,)).fetchone()
    assert row is not None
    return TaskRow(**row)


def get(task_id: str) -> TaskRow | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM aeos.tasks WHERE task_id = %s", (task_id,)).fetchone()
    return TaskRow(**row) if row else None


def list_tasks(status: str | None = None, limit: int = 100) -> list[TaskRow]:
    with _connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM aeos.tasks WHERE status = %s ORDER BY created_at DESC LIMIT %s",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM aeos.tasks ORDER BY created_at DESC LIMIT %s", (limit,)
            ).fetchall()
    return [TaskRow(**r) for r in rows]


def claim(worker_id: str, lease_s: float, max_attempts: int = 5) -> TaskRow | None:
    """Claim one runnable task: queued, or running with an expired lease (dead worker)."""
    with _connect() as conn:
        # crash-loop guard: a task that keeps killing its workers goes to a human
        conn.execute(
            "UPDATE aeos.tasks SET status = 'escalated', worker_id = NULL, lease_expires_at = NULL,"
            " last_error = 'worker died or failed ' || attempts || ' times; needs a human',"
            " updated_at = now()"
            " WHERE attempts >= %s AND (status = 'queued'"
            " OR (status = 'running' AND lease_expires_at < now()))",
            (max_attempts,),
        )
        row = conn.execute(
            "UPDATE aeos.tasks SET status = 'running', worker_id = %s,"
            " lease_expires_at = now() + make_interval(secs => %s), heartbeat_at = now(),"
            " attempts = attempts + 1, updated_at = now()"
            " WHERE task_id = (SELECT task_id FROM aeos.tasks WHERE status = 'queued'"
            "   OR (status = 'running' AND lease_expires_at < now())"
            "   ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *",
            (worker_id, lease_s),
        ).fetchone()
    return TaskRow(**row) if row else None


def heartbeat(task_id: str, worker_id: str, lease_s: float) -> bool:
    """Renew the lease. False means the lease was lost: the caller must stop working."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE aeos.tasks SET lease_expires_at = now() + make_interval(secs => %s),"
            " heartbeat_at = now() WHERE task_id = %s AND worker_id = %s AND status = 'running'",
            (lease_s, task_id, worker_id),
        )
        return cur.rowcount == 1


def finish(task_id: str, worker_id: str, status: str, error: str | None = None) -> TaskRow | None:
    """Release the task with its outcome.

    `paused` becomes `queued` if a human decision already arrived while the run was winding down.
    """
    with _connect() as conn:
        row = conn.execute(
            "UPDATE aeos.tasks SET"
            " status = CASE WHEN %s = 'paused' AND pending_resume IS NOT NULL"
            " THEN 'queued' ELSE %s END,"
            " pending_resume = CASE WHEN %s = 'paused' THEN pending_resume ELSE NULL END,"
            " worker_id = NULL, lease_expires_at = NULL, last_error = %s, updated_at = now()"
            " WHERE task_id = %s AND worker_id = %s AND status = 'running' RETURNING *",
            (status, status, status, error, task_id, worker_id),
        ).fetchone()
    return TaskRow(**row) if row else None


def consumed_resume(task_id: str, worker_id: str, payload: dict[str, Any]) -> None:
    """The stored resume payload has been delivered to the graph.

    Compare-and-clear: a NEWER decision that arrived meanwhile (different approval_id) survives.
    """
    with _connect() as conn:
        conn.execute(
            "UPDATE aeos.tasks SET pending_resume = NULL"
            " WHERE task_id = %s AND worker_id = %s AND pending_resume = %s",
            (task_id, worker_id, Jsonb(payload)),
        )


def release_with_error(task_id: str, worker_id: str, error: str) -> None:
    """The run failed unexpectedly: put it back in the queue (attempt counter protects loops)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE aeos.tasks SET status = 'queued', worker_id = NULL, lease_expires_at = NULL,"
            " last_error = %s, updated_at = now() WHERE task_id = %s AND worker_id = %s",
            (error, task_id, worker_id),
        )


def cancel(task_id: str, reason: str) -> TaskRow | None:
    """Stop a task that has not finished (queued, running or waiting for approval).

    A running worker is fenced out: its next heartbeat sees the row is no longer `running` under
    its id and it abandons the work. None means the task is unknown or already finished.
    """
    with _connect() as conn:
        row = conn.execute(
            "UPDATE aeos.tasks SET status = 'cancelled', worker_id = NULL, lease_expires_at = NULL,"
            " pending_resume = NULL, last_error = %s, updated_at = now()"
            " WHERE task_id = %s AND status IN ('queued', 'running', 'paused') RETURNING *",
            (reason, task_id),
        ).fetchone()
    return TaskRow(**row) if row else None


def active_task_ids() -> set[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT task_id FROM aeos.tasks WHERE status IN ('queued', 'running', 'paused')"
        ).fetchall()
    return {r["task_id"] for r in rows}


def enqueue_resume(
    conn: psycopg.Connection[dict[str, Any]], task_id: str, payload: dict[str, Any]
) -> None:
    """Store a resume payload; wake the task if it is already paused. Same txn as the decision."""
    conn.execute(
        "UPDATE aeos.tasks SET pending_resume = %s,"
        " status = CASE WHEN status = 'paused' THEN 'queued' ELSE status END, updated_at = now()"
        " WHERE task_id = %s",
        (Jsonb(payload), task_id),
    )
