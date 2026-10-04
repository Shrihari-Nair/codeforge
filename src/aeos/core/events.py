"""Run events: what the CLI streams now and the API/dashboard will stream later."""

import json
import sys
import time
from typing import IO, Any, Protocol

from pydantic import BaseModel, Field


class Event(BaseModel):
    type: str  # e.g. plan_created, agent_started, llm_call, tool_call, tool_result, step_done, ...
    task_id: str
    agent: str = ""
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    ts: float = Field(default_factory=time.time)


class EventSink(Protocol):
    def emit(self, event: Event) -> None: ...


class NullSink:
    def emit(self, event: Event) -> None:
        pass


class ListSink:
    """Collects events (tests, API replay)."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]


class CompositeSink:
    def __init__(self, *sinks: EventSink) -> None:
        self.sinks = sinks

    def emit(self, event: Event) -> None:
        for s in self.sinks:
            s.emit(event)


class ConsoleSink:
    _ICONS = {
        "plan_created": "📋", "replan": "🔁", "agent_started": "▶", "agent_finished": "■",
        "tool_call": "🔧", "verification": "🔎", "approval_requested": "⏸", "task_completed": "✅",
        "task_failed": "❌", "llm_call": "·",
    }  # fmt: skip

    def __init__(self, stream: IO[str] | None = None, verbose: bool = False) -> None:
        self.stream = stream or sys.stdout
        self.verbose = verbose

    def emit(self, event: Event) -> None:
        if event.type == "llm_call" and not self.verbose:
            return
        icon = self._ICONS.get(event.type, "•")
        who = f"[{event.agent}] " if event.agent else ""
        print(f"{icon} {who}{event.message or event.type}", file=self.stream, flush=True)
        if self.verbose and event.data:
            print("   " + json.dumps(event.data, default=str)[:300], file=self.stream, flush=True)
