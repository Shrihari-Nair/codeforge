"""Episodic + semantic memory in Postgres (pgvector).

Semantic facts are keyed by (subject, predicate). A new value for an existing key SUPERSEDES the old
one (kept for history, no longer returned); the same value only refreshes `last_confirmed`.
"""

import asyncio
import uuid
from datetime import datetime
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from aeos.core.config import get_settings
from aeos.memory.embedder import DIM, Embedder
from aeos.memory.gate import FactProposal

DDL = f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS aeos;
CREATE TABLE IF NOT EXISTS aeos.episodes (
    id text PRIMARY KEY,
    task_id text UNIQUE NOT NULL,
    goal text NOT NULL,
    outcome text NOT NULL,
    summary jsonb NOT NULL,
    text text NOT NULL,
    embedding vector({DIM}) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS episodes_emb_idx ON aeos.episodes USING hnsw (embedding vector_cosine_ops);
CREATE TABLE IF NOT EXISTS aeos.facts (
    id text PRIMARY KEY,
    subject text NOT NULL,
    predicate text NOT NULL,
    value text NOT NULL,
    confidence real NOT NULL,
    source_task text,
    evidence jsonb NOT NULL DEFAULT '[]',
    origin text NOT NULL DEFAULT 'trusted',
    status text NOT NULL DEFAULT 'active',
    superseded_by text,
    invalidated_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_confirmed timestamptz NOT NULL DEFAULT now(),
    embedding vector({DIM}) NOT NULL
);
CREATE INDEX IF NOT EXISTS facts_key_idx ON aeos.facts (subject, predicate) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS facts_emb_idx ON aeos.facts USING hnsw (embedding vector_cosine_ops);
"""

Outcome = Literal["completed", "escalated", "failed", "cancelled"]


class Episode(BaseModel):
    task_id: str
    goal: str
    outcome: Outcome
    summary: dict[str, Any]
    lessons: list[str] = Field(default_factory=list)

    def embedding_text(self) -> str:
        s = self.summary
        parts = [self.goal, str(s.get("root_cause_summary", "")), str(s.get("root_cause_file", "")),
                 str(s.get("fix_summary", "")), " ".join(self.lessons)]  # fmt: skip
        return " ".join(p for p in parts if p)


class EpisodeHit(BaseModel):
    id: str
    task_id: str
    goal: str
    outcome: str
    summary: dict[str, Any]
    similarity: float
    created_at: datetime


class Fact(BaseModel):
    id: str
    subject: str
    predicate: str
    value: str
    confidence: float
    status: str
    superseded_by: str | None = None
    source_task: str | None = None
    created_at: datetime
    last_confirmed: datetime


class FactHit(Fact):
    similarity: float


class UpsertResult(BaseModel):
    kind: Literal["created", "superseded", "confirmed"]
    fact_id: str
    superseded_id: str | None = None


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def _connect() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(get_settings().database_url, row_factory=dict_row)


class MemoryStore:
    def __init__(self, embedder: Embedder) -> None:
        self.embedder = embedder

    async def ensure_schema(self) -> None:
        def run() -> None:
            with _connect() as conn:
                conn.execute(DDL)

        await asyncio.to_thread(run)

    # ------------------------------------------------------------------ episodes
    async def save_episode(self, ep: Episode) -> bool:
        """Idempotent per task. Returns True if a new episode was written."""
        (emb,) = await self.embedder.embed([ep.embedding_text()])

        def run() -> bool:
            with _connect() as conn:
                row = conn.execute(
                    "INSERT INTO aeos.episodes (id, task_id, goal, outcome, summary, text, embedding)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s::vector)"
                    " ON CONFLICT (task_id) DO NOTHING RETURNING id",
                    (f"ep-{uuid.uuid4().hex[:10]}", ep.task_id, ep.goal, ep.outcome,
                     Jsonb({**ep.summary, "lessons": ep.lessons}), ep.embedding_text(), _vec(emb)),
                ).fetchone()  # fmt: skip
            return row is not None

        return await asyncio.to_thread(run)

    async def search_episodes(
        self, query: str, k: int = 3, min_similarity: float = 0.3, outcome: str | None = None
    ) -> list[EpisodeHit]:
        (emb,) = await self.embedder.embed([query], query=True)

        def run() -> list[EpisodeHit]:
            with _connect() as conn:
                rows = conn.execute(
                    "SELECT id, task_id, goal, outcome, summary, created_at,"
                    " 1 - (embedding <=> %s::vector) AS similarity FROM aeos.episodes"
                    " WHERE (%s::text IS NULL OR outcome = %s)"
                    " ORDER BY embedding <=> %s::vector LIMIT %s",
                    (_vec(emb), outcome, outcome, _vec(emb), k),
                ).fetchall()
            return [EpisodeHit(**r) for r in rows if r["similarity"] >= min_similarity]

        return await asyncio.to_thread(run)

    async def episode_count(self) -> int:
        def run() -> int:
            with _connect() as conn:
                row = conn.execute("SELECT count(*) AS n FROM aeos.episodes").fetchone()
            return int(row["n"]) if row else 0

        return await asyncio.to_thread(run)

    # ------------------------------------------------------------------ facts
    async def active_fact(self, subject: str, predicate: str) -> Fact | None:
        def run() -> Fact | None:
            with _connect() as conn:
                row = conn.execute(
                    "SELECT * FROM aeos.facts WHERE subject = %s AND predicate = %s AND status = 'active'",
                    (subject, predicate),
                ).fetchone()
            return Fact(**row) if row else None

        return await asyncio.to_thread(run)

    async def upsert_fact(self, fact: FactProposal, source_task: str | None) -> UpsertResult:
        existing = await self.active_fact(fact.subject, fact.predicate)
        if existing is not None and existing.value == fact.value:

            def confirm() -> None:
                with _connect() as conn:
                    conn.execute("UPDATE aeos.facts SET last_confirmed = now() WHERE id = %s",
                                 (existing.id,))  # fmt: skip

            await asyncio.to_thread(confirm)
            return UpsertResult(kind="confirmed", fact_id=existing.id)
        (emb,) = await self.embedder.embed([f"{fact.subject} {fact.predicate} {fact.value}"])
        new_id = f"fact-{uuid.uuid4().hex[:10]}"

        def write() -> None:
            with _connect() as conn, conn.transaction():
                conn.execute(
                    "INSERT INTO aeos.facts (id, subject, predicate, value, confidence, source_task,"
                    " evidence, origin, embedding) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector)",
                    (new_id, fact.subject, fact.predicate, fact.value, fact.confidence, source_task,
                     Jsonb(fact.evidence), fact.origin, _vec(emb)),
                )  # fmt: skip
                if existing is not None:
                    conn.execute(
                        "UPDATE aeos.facts SET status = 'superseded', superseded_by = %s"
                        " WHERE id = %s AND status = 'active'",
                        (new_id, existing.id),
                    )

        await asyncio.to_thread(write)
        if existing is not None:
            return UpsertResult(kind="superseded", fact_id=new_id, superseded_id=existing.id)
        return UpsertResult(kind="created", fact_id=new_id)

    async def invalidate_fact(self, fact_id: str, reason: str) -> bool:
        def run() -> bool:
            with _connect() as conn:
                cur = conn.execute(
                    "UPDATE aeos.facts SET status = 'invalidated', invalidated_reason = %s"
                    " WHERE id = %s AND status = 'active'",
                    (reason, fact_id),
                )
                return cur.rowcount == 1

        return await asyncio.to_thread(run)

    async def search_facts(
        self, query: str, k: int = 6, min_similarity: float = 0.2
    ) -> list[FactHit]:
        (emb,) = await self.embedder.embed([query], query=True)

        def run() -> list[FactHit]:
            with _connect() as conn:
                rows = conn.execute(
                    "SELECT id, subject, predicate, value, confidence, status, superseded_by,"
                    " source_task, created_at, last_confirmed,"
                    " 1 - (embedding <=> %s::vector) AS similarity FROM aeos.facts"
                    " WHERE status = 'active' ORDER BY embedding <=> %s::vector LIMIT %s",
                    (_vec(emb), _vec(emb), k),
                ).fetchall()
            return [FactHit(**r) for r in rows if r["similarity"] >= min_similarity]

        return await asyncio.to_thread(run)

    async def all_facts(self, subject: str | None = None) -> list[Fact]:
        def run() -> list[Fact]:
            with _connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM aeos.facts WHERE (%s::text IS NULL OR subject = %s)"
                    " ORDER BY created_at", (subject, subject),
                ).fetchall()  # fmt: skip
            return [Fact(**r) for r in rows]

        return await asyncio.to_thread(run)
