"""Approval requests and the audit log (Postgres schema `aeos`).

Decisions are compare-and-set on `status = 'pending'`, so an approval can be applied exactly
once, even if two reviewers click at the same moment or the same request is replayed.
"""

import json
import uuid
from datetime import datetime
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from aeos.core.config import get_settings
from aeos.core.risk import arguments_hash
from aeos.store.tasks import DDL as TASKS_DDL
from aeos.store.tasks import enqueue_resume

DDL = """
CREATE SCHEMA IF NOT EXISTS aeos;
CREATE TABLE IF NOT EXISTS aeos.approvals (
    id text PRIMARY KEY,
    task_id text NOT NULL,
    action_key text NOT NULL,
    tool text NOT NULL,
    server text NOT NULL DEFAULT '',
    arguments jsonb NOT NULL,
    arguments_hash text NOT NULL,
    rationale text NOT NULL,
    evidence jsonb NOT NULL DEFAULT '{}',
    status text NOT NULL DEFAULT 'pending',
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    decided_at timestamptz,
    actor text,
    feedback text,
    UNIQUE (task_id, action_key)
);
CREATE INDEX IF NOT EXISTS approvals_status_idx ON aeos.approvals (status);
CREATE TABLE IF NOT EXISTS aeos.audit_log (
    id bigserial PRIMARY KEY,
    ts timestamptz NOT NULL DEFAULT now(),
    task_id text,
    actor text NOT NULL,
    event text NOT NULL,
    data jsonb NOT NULL DEFAULT '{}',
    dedupe_key text
);
ALTER TABLE aeos.audit_log ADD COLUMN IF NOT EXISTS dedupe_key text;
CREATE UNIQUE INDEX IF NOT EXISTS audit_dedupe_idx ON aeos.audit_log (dedupe_key)
    WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS audit_task_idx ON aeos.audit_log (task_id);
"""

Status = Literal["pending", "approved", "rejected", "expired"]


class ApprovalRequest(BaseModel):
    id: str
    task_id: str
    action_key: str
    tool: str
    server: str = ""
    arguments: dict[str, Any]
    arguments_hash: str
    rationale: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    status: Status = "pending"
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    actor: str | None = None
    feedback: str | None = None


class DecisionResult(BaseModel):
    kind: Literal["ok", "already_decided", "expired", "not_found"]
    request: ApprovalRequest | None = None


def _connect() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(get_settings().database_url, row_factory=dict_row)


def ensure_schema() -> None:
    with _connect() as conn:
        conn.execute(DDL)
        conn.execute(TASKS_DDL)  # decisions enqueue task resumes, so the queue table must exist


def audit(
    task_id: str | None,
    actor: str,
    event: str,
    data: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
) -> None:
    """Append an audit entry. With `dedupe_key`, replays after a crash do not write it twice."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO aeos.audit_log (task_id, actor, event, data, dedupe_key)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (task_id, actor, event, Jsonb(data or {}), dedupe_key),
        )


def audit_log(task_id: str | None = None) -> list[dict[str, Any]]:
    with _connect() as conn:
        if task_id:
            rows = conn.execute(
                "SELECT * FROM aeos.audit_log WHERE task_id = %s ORDER BY id", (task_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM aeos.audit_log ORDER BY id").fetchall()
    return rows


def create(
    *,
    task_id: str,
    action_key: str,
    tool: str,
    server: str,
    arguments: dict[str, Any],
    rationale: str,
    evidence: dict[str, Any],
    ttl_s: float = 86_400,
) -> ApprovalRequest:
    """Idempotent on (task_id, action_key): re-running the node returns the same request."""
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO aeos.approvals (id, task_id, action_key, tool, server, arguments,"
            " arguments_hash, rationale, evidence, expires_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))"
            " ON CONFLICT (task_id, action_key) DO NOTHING RETURNING *",
            (f"ap-{uuid.uuid4().hex[:10]}", task_id, action_key, tool, server, Jsonb(arguments),
             arguments_hash(tool, arguments), rationale, Jsonb(evidence), ttl_s),
        ).fetchone()  # fmt: skip
        created = row is not None
        if row is None:
            row = conn.execute(
                "SELECT * FROM aeos.approvals WHERE task_id = %s AND action_key = %s",
                (task_id, action_key),
            ).fetchone()
        assert row is not None
        if created:
            conn.execute(
                "INSERT INTO aeos.audit_log (task_id, actor, event, data) VALUES (%s, 'system',"
                " 'approval_requested', %s)",
                (task_id, Jsonb({"approval_id": row["id"], "tool": tool})),
            )
    return ApprovalRequest(**row)


def get(approval_id: str) -> ApprovalRequest | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM aeos.approvals WHERE id = %s", (approval_id,)).fetchone()
    return ApprovalRequest(**row) if row else None


def list_requests(status: str | None = None, task_id: str | None = None) -> list[ApprovalRequest]:
    where, params = ["true"], []  # type: list[str], list[Any]
    if status:
        where.append("status = %s")
        params.append(status)
    if task_id:
        where.append("task_id = %s")
        params.append(task_id)
    sql = "SELECT * FROM aeos.approvals WHERE " + " AND ".join(where) + " ORDER BY created_at"  # noqa: S608
    with _connect() as conn:
        return [ApprovalRequest(**r) for r in conn.execute(sql, params).fetchall()]


EXPIRED_PAYLOAD = {"decision": "expired", "actor": "system", "feedback": "approval request expired"}


def decide(
    approval_id: str,
    decision: Literal["approved", "rejected"],
    actor: str,
    feedback: str | None = None,
) -> DecisionResult:
    """Apply a human decision exactly once. Expired requests cannot be approved.

    The decision and the task's resume payload are written in ONE transaction, so a crash can
    never leave an approved request whose task will not be resumed.
    """
    with _connect() as conn:
        row = conn.execute(
            "UPDATE aeos.approvals SET status = %s, actor = %s, feedback = %s, decided_at = now()"
            " WHERE id = %s AND status = 'pending' AND expires_at > now() RETURNING *",
            (decision, actor, feedback, approval_id),
        ).fetchone()
        if row is not None:
            conn.execute(
                "INSERT INTO aeos.audit_log (task_id, actor, event, data) VALUES (%s, %s, %s, %s)",
                (row["task_id"], actor, f"approval_{decision}",
                 Jsonb({"approval_id": approval_id, "feedback": feedback})),
            )  # fmt: skip
            payload = {
                "decision": decision,
                "actor": actor,
                "feedback": feedback or "",
                "approval_id": approval_id,
            }
            enqueue_resume(conn, row["task_id"], payload)
            return DecisionResult(kind="ok", request=ApprovalRequest(**row))
        current = conn.execute(
            "SELECT * FROM aeos.approvals WHERE id = %s", (approval_id,)
        ).fetchone()
        if current is None:
            return DecisionResult(kind="not_found")
        req = ApprovalRequest(**current)
        if req.status == "pending":  # pending but past its deadline
            conn.execute(
                "UPDATE aeos.approvals SET status = 'expired', decided_at = now()"
                " WHERE id = %s AND status = 'pending'",
                (approval_id,),
            )
            conn.execute(
                "INSERT INTO aeos.audit_log (task_id, actor, event, data) VALUES (%s, 'system',"
                " 'approval_expired', %s)",
                (req.task_id, Jsonb({"approval_id": approval_id})),
            )
            enqueue_resume(conn, req.task_id, {**EXPIRED_PAYLOAD, "approval_id": approval_id})
            req.status = "expired"
            return DecisionResult(kind="expired", request=req)
        return DecisionResult(
            kind="expired" if req.status == "expired" else "already_decided", request=req
        )


def cancel_pending(task_id: str, actor: str) -> list[ApprovalRequest]:
    """A cancelled task no longer needs its pending approvals: close them (no resume is queued)."""
    with _connect() as conn:
        rows = conn.execute(
            "UPDATE aeos.approvals SET status = 'expired', decided_at = now()"
            " WHERE task_id = %s AND status = 'pending' RETURNING *",
            (task_id,),
        ).fetchall()
        for r in rows:
            conn.execute(
                "INSERT INTO aeos.audit_log (task_id, actor, event, data) VALUES (%s, %s,"
                " 'approval_cancelled', %s)",
                (task_id, actor, Jsonb({"approval_id": r["id"]})),
            )
    return [ApprovalRequest(**r) for r in rows]


def expire_due() -> list[ApprovalRequest]:
    """Mark pending requests past their deadline as expired. Returns the newly expired ones."""
    with _connect() as conn:
        rows = conn.execute(
            "UPDATE aeos.approvals SET status = 'expired', decided_at = now()"
            " WHERE status = 'pending' AND expires_at <= now() RETURNING *"
        ).fetchall()
        for r in rows:
            conn.execute(
                "INSERT INTO aeos.audit_log (task_id, actor, event, data) VALUES (%s, 'system',"
                " 'approval_expired', %s)",
                (r["task_id"], Jsonb({"approval_id": r["id"]})),
            )
            enqueue_resume(conn, r["task_id"], {**EXPIRED_PAYLOAD, "approval_id": r["id"]})
    return [ApprovalRequest(**r) for r in rows]


def to_json(req: ApprovalRequest) -> str:
    return json.dumps(req.model_dump(mode="json"), default=str)
