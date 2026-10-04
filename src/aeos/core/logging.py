import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import IO, Any

_task_id: ContextVar[str | None] = ContextVar("task_id", default=None)
_run_id: ContextVar[str | None] = ContextVar("run_id", default=None)
_agent: ContextVar[str | None] = ContextVar("agent", default=None)

# attributes present on every LogRecord; anything else came from `extra=`
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


@contextmanager
def run_context(
    *, task_id: str | None = None, run_id: str | None = None, agent: str | None = None
) -> Iterator[None]:
    tokens: list[tuple[ContextVar[str | None], Token[str | None]]] = []
    if task_id is not None:
        tokens.append((_task_id, _task_id.set(task_id)))
    if run_id is not None:
        tokens.append((_run_id, _run_id.set(run_id)))
    if agent is not None:
        tokens.append((_agent, _agent.set(agent)))
    try:
        yield
    finally:
        for var, tok in reversed(tokens):
            var.reset(tok)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "task_id": _task_id.get(),
            "run_id": _run_id.get(),
            "agent": _agent.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=repr)


_HANDLER_FLAG = "_aeos_json_handler"


def configure_logging(level: str = "INFO", *, stream: IO[str] | None = None) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _HANDLER_FLAG, False):
            root.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    setattr(handler, _HANDLER_FLAG, True)
    root.addHandler(handler)
    root.setLevel(level)
