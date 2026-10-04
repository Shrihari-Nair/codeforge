"""Durable checkpointing: LangGraph state in Postgres, written after every node."""

import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel

import aeos.core.state as state_module
from aeos.core.config import Settings, get_settings


def state_types() -> list[tuple[str, str]]:
    """Every pydantic model in the task state, explicitly allowed for checkpoint deserialization
    (LangGraph is moving to an allowlist, so unknown types will be refused)."""
    return [
        (state_module.__name__, name)
        for name, cls in inspect.getmembers(state_module, inspect.isclass)
        if issubclass(cls, BaseModel) and cls.__module__ == state_module.__name__
    ]


@asynccontextmanager
async def open_checkpointer(settings: Settings | None = None) -> AsyncIterator[AsyncPostgresSaver]:
    s = settings or get_settings()
    serde = JsonPlusSerializer(allowed_msgpack_modules=state_types())
    async with AsyncPostgresSaver.from_conn_string(s.database_url, serde=serde) as saver:
        await saver.setup()  # idempotent
        yield saver
