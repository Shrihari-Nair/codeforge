"""A2A server side: expose one specialist agent as an independently deployed service.

MCP = agent <-> tools/data.   A2A = agent <-> agent.   This module is the A2A half: it never talks
to tools itself; the wrapped specialist owns its own MCP connections.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from a2a.helpers import get_data_parts, new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from fastapi import FastAPI, Request, Response
from google.protobuf import json_format
from google.protobuf.struct_pb2 import Value

from a2a import types as t
from aeos.agents.specialists import Specialist, SpecialistRequest, SpecialistResult
from aeos.core.budget import BudgetTracker
from aeos.core.events import Event
from aeos.core.state import Budget
from aeos.telemetry.tracing import remote_context, traced

log = logging.getLogger("aeos.a2a.server")

RPC_PATH = "/a2a"
REQUEST_KEY = "request"


def data_part(obj: dict[str, Any]) -> t.Part:
    return t.Part(data=json_format.ParseDict(obj, Value()), media_type="application/json")


def build_card(
    *,
    owner: str,
    description: str,
    skills: list[t.AgentSkill],
    base_url: str,
    version: str = "0.1.0",
) -> t.AgentCard:
    return t.AgentCard(
        name=f"aeos-{owner}",
        description=description,
        version=version,
        supported_interfaces=[
            t.AgentInterface(
                url=f"{base_url.rstrip('/')}{RPC_PATH}",
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            )  # fmt: skip
        ],
        capabilities=t.AgentCapabilities(streaming=True),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=skills,
        security_schemes={
            "bearer": t.SecurityScheme(
                http_auth_security_scheme=t.HTTPAuthSecurityScheme(scheme="bearer")
            )
        },  # fmt: skip
        security_requirements=[t.SecurityRequirement(schemes={"bearer": t.StringList()})],
    )


class _ProgressSink:
    """Turns the specialist's events into streamed A2A status updates (ordered, non-blocking)."""

    def __init__(self, push: Callable[[str], Awaitable[None]]) -> None:
        self._q: asyncio.Queue[str | None] = asyncio.Queue()
        self._drain = asyncio.create_task(self._run(push))

    async def _run(self, push: Callable[[str], Awaitable[None]]) -> None:
        while (msg := await self._q.get()) is not None:
            with contextlib.suppress(Exception):
                await push(msg)

    def emit(self, event: Event) -> None:
        if event.type != "llm_call":
            self._q.put_nowait(event.message or event.type)

    async def close(self) -> None:
        self._q.put_nowait(None)
        await self._drain


class SpecialistExecutor(AgentExecutor):
    def __init__(self, specialist: Specialist) -> None:
        self.specialist = specialist
        self._running: dict[str, asyncio.Task[SpecialistResult]] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        assert context.message is not None and context.task_id and context.context_id
        if context.current_task is None:
            await event_queue.enqueue_event(new_task_from_user_message(context.message))
        up = TaskUpdater(event_queue, context.task_id, context.context_id)
        await up.start_work()

        meta = (
            json_format.MessageToDict(context.message.metadata) if context.message.metadata else {}
        )
        try:
            payload = get_data_parts(context.message.parts)[0][REQUEST_KEY]
            req = SpecialistRequest.model_validate(payload)
            budget = Budget(**meta["budget"]) if "budget" in meta else Budget()
        except Exception as exc:
            await up.failed(up.new_agent_message([t.Part(text=f"invalid request: {exc}")]))
            return

        async def push(text: str) -> None:
            await up.update_status(t.TaskState.TASK_STATE_WORKING,
                                   up.new_agent_message([t.Part(text=text)]))  # fmt: skip

        sink = _ProgressSink(push)
        tracker = BudgetTracker(budget)
        before = tracker.budget.model_copy(deep=True)

        async def traced_run() -> SpecialistResult:
            # one trace across processes: our spans become children of the supervisor's delegation
            with remote_context(meta.get("trace")), traced(
                f"a2a.serve {req.step_id}", agent=getattr(self.specialist, "owner", "?"),
                task_id=req.task_id, step_id=req.step_id,
            ):  # fmt: skip
                return await self.specialist.run(req, tracker, sink)

        run = asyncio.create_task(traced_run())
        self._running[context.task_id] = run
        try:
            result = await run
        except asyncio.CancelledError:
            await sink.close()
            raise
        except Exception as exc:  # a crash inside the specialist is a failed task, not a hung one
            log.exception("specialist crashed")
            await sink.close()
            await up.failed(up.new_agent_message([t.Part(text=f"{type(exc).__name__}: {exc}")]))
            return
        finally:
            self._running.pop(context.task_id, None)
        await sink.close()
        after = tracker.budget
        result.usage = {
            "llm_calls": after.llm_calls - before.llm_calls,
            "tool_calls": after.tool_calls - before.tool_calls,
            "tokens": after.tokens_used - before.tokens_used,
            "steps": after.steps_used - before.steps_used,
            "cost_usd": after.cost_usd - before.cost_usd,
        }
        await up.add_artifact([data_part(result.model_dump(mode="json"))], name="result")
        await up.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        assert context.task_id and context.context_id
        if (run := self._running.get(context.task_id)) is not None:
            run.cancel()
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


def create_specialist_app(
    specialist: Specialist,
    card: t.AgentCard,
    *,
    api_key: str | None = None,
    on_shutdown: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    handler = DefaultRequestHandler(
        agent_executor=SpecialistExecutor(specialist),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    app = FastAPI(title=card.name)
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),  # discovery is public by design
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url=RPC_PATH),
    )

    @app.middleware("http")
    async def require_bearer(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if api_key and request.url.path == RPC_PATH:
            if request.headers.get("authorization") != f"Bearer {api_key}":
                return Response("unauthorized", status_code=401)
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "agent": card.name}

    if on_shutdown is not None:
        app.router.on_shutdown.append(on_shutdown)
    return app
