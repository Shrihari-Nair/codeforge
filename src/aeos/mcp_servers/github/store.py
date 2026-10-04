"""Pull-request metadata store (Postgres schema `vcs`)."""

from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel

from aeos.core.config import get_settings

DDL = """
CREATE SCHEMA IF NOT EXISTS vcs;
CREATE TABLE IF NOT EXISTS vcs.pull_requests (
    number serial PRIMARY KEY,
    repo text NOT NULL,
    title text NOT NULL,
    body text NOT NULL,
    base_branch text NOT NULL,
    head_branch text NOT NULL,
    head_sha text NOT NULL,
    status text NOT NULL DEFAULT 'open',
    idempotency_key text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (repo, idempotency_key)
);
ALTER TABLE vcs.pull_requests ADD COLUMN IF NOT EXISTS remote_url text;
ALTER TABLE vcs.pull_requests ADD COLUMN IF NOT EXISTS remote_number integer;
CREATE TABLE IF NOT EXISTS vcs.branch_creations (
    repo text NOT NULL,
    idempotency_key text NOT NULL,
    name text NOT NULL,
    base_sha text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo, idempotency_key)
);
"""


class PullRequest(BaseModel):
    number: int
    repo: str
    title: str
    body: str
    base_branch: str
    head_branch: str
    head_sha: str
    status: str
    idempotency_key: str
    created_at: datetime
    url: str = ""
    remote_url: str | None = None  # the real GitHub PR, once mirrored
    remote_number: int | None = None


def _connect() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(get_settings().database_url, row_factory=dict_row)


def _pr(row: dict[str, Any]) -> PullRequest:
    url = row.get("remote_url") or f"local://{row['repo']}/pull/{row['number']}"
    return PullRequest(**row, url=url)


def ensure_schema() -> None:
    with _connect() as conn:
        conn.execute(DDL)


def get_by_key(repo: str, key: str) -> PullRequest | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM vcs.pull_requests WHERE repo = %s AND idempotency_key = %s", (repo, key)
        ).fetchone()
    return _pr(row) if row else None


def get_open(repo: str, base: str, head: str) -> PullRequest | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM vcs.pull_requests WHERE repo = %s AND base_branch = %s"
            " AND head_branch = %s AND status = 'open'",
            (repo, base, head),
        ).fetchone()
    return _pr(row) if row else None


def create(
    repo: str, title: str, body: str, base: str, head: str, head_sha: str, key: str
) -> PullRequest:
    """Atomic insert; a concurrent replay with the same key returns the existing row."""
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO vcs.pull_requests (repo, title, body, base_branch, head_branch, head_sha,"
            " idempotency_key) VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (repo, idempotency_key) DO NOTHING RETURNING *",
            (repo, title, body, base, head, head_sha, key),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM vcs.pull_requests WHERE repo = %s AND idempotency_key = %s",
                (repo, key),
            ).fetchone()
    assert row is not None
    return _pr(row)


def set_remote(repo: str, number: int, remote_number: int, remote_url: str) -> PullRequest:
    with _connect() as conn:
        row = conn.execute(
            "UPDATE vcs.pull_requests SET remote_number = %s, remote_url = %s"
            " WHERE repo = %s AND number = %s RETURNING *",
            (remote_number, remote_url, repo, number),
        ).fetchone()
    assert row is not None
    return _pr(row)


def get(repo: str, number: int) -> PullRequest | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM vcs.pull_requests WHERE repo = %s"
            " AND (remote_number = %s OR number = %s)"
            " ORDER BY (remote_number = %s) DESC NULLS LAST LIMIT 1",
            (repo, number, number, number),
        ).fetchone()
    return _pr(row) if row else None


def get_branch_creation(repo: str, key: str) -> tuple[str, str] | None:
    """(branch name, base sha) recorded for this idempotency key, if the call already happened."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT name, base_sha FROM vcs.branch_creations"
            " WHERE repo = %s AND idempotency_key = %s",
            (repo, key),
        ).fetchone()
    return (row["name"], row["base_sha"]) if row else None


def record_branch_creation(repo: str, key: str, name: str, base_sha: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO vcs.branch_creations (repo, idempotency_key, name, base_sha)"
            " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (repo, key, name, base_sha),
        )
