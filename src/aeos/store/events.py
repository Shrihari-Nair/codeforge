"""Persisted run events (Postgres): the dashboard's live timeline and its refresh-proof history.

`DbSink` plugs into the existing `EventSink` seam; writes happen on a background thread so
emitting from the agent loop never blocks on the database.
"""

import queue
import threading
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from aeos.core.config import get_settings
from aeos.core.events import Event

DDL = """
CREATE SCHEMA IF NOT EXISTS aeos;
CREATE TABLE IF NOT EXISTS aeos.events (
    id bigserial PRIMARY KEY,
    task_id text NOT NULL,
    ts timestamptz NOT NULL,
    type text NOT NULL,
    agent text NOT NULL DEFAULT '',
    message text NOT NULL DEFAULT '',
    data jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_task_idx ON aeos.events (task_id, id);
"""


class StoredEvent(BaseModel):
    id: int
    task_id: str
    ts: datetime
    type: str
    agent: str
    message: str
    data: dict[str, Any]


def _connect() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(get_settings().database_url, row_factory=dict_row)


def ensure_schema() -> None:
    with _connect() as conn:
        conn.execute(DDL)


def list_events(task_id: str, after_id: int = 0, limit: int = 500) -> list[StoredEvent]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM aeos.events WHERE task_id = %s AND id > %s ORDER BY id LIMIT %s",
            (task_id, after_id, limit),
        ).fetchall()
    return [StoredEvent(**r) for r in rows]


class DbSink:
    """Writes events to Postgres from a daemon thread (never blocks the caller)."""

    def __init__(self) -> None:
        self._q: queue.Queue[Event | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="event-writer", daemon=True)
        self._thread.start()

    def emit(self, event: Event) -> None:
        if event.type != "llm_call":  # per-call token chatter lives in the trace, not the timeline
            self._q.put(event)

    def flush(self, timeout: float = 5.0) -> None:
        done = threading.Event()
        marker = Event(type="__flush__", task_id="")
        marker.data["done"] = done
        self._q.put(marker)
        done.wait(timeout)

    def _run(self) -> None:
        conn: psycopg.Connection[Any] | None = None
        while True:
            ev = self._q.get()
            if ev is None:
                return
            if ev.type == "__flush__":
                ev.data["done"].set()
                continue
            try:
                if conn is None or conn.closed:
                    conn = psycopg.connect(get_settings().database_url, autocommit=True)
                conn.execute(
                    "INSERT INTO aeos.events (task_id, ts, type, agent, message, data)"
                    " VALUES (%s, to_timestamp(%s), %s, %s, %s, %s)",
                    (
                        ev.task_id,
                        ev.ts,
                        ev.type,
                        ev.agent,
                        ev.message[:2000],
                        Jsonb(ev.data, dumps=_dumps),
                    ),
                )
            except Exception:  # an unavailable DB must not take the run down
                conn = None


def _dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, default=str)
