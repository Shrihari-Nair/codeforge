"""A2A client side: the supervisor's view of a remote specialist.

`A2ASpecialist` implements the same `Specialist` protocol as the in-process classes, so the graph
does not know or care which one it talks to. Failures (service down, hung, rejecting) become a
`SpecialistResult(status="error")`, which flows into the normal retry/escalation policy; it never
hangs the run.
"""

import asyncio
import contextlib
import logging
from typing import Any

import httpx
from a2a.client.client import Client, ClientConfig
from a2a.client.client_factory import create_client
from a2a.helpers import get_data_parts
from a2a.types import a2a_pb2
from a2a.utils import errors as a2a_errors
from google.protobuf import json_format
from google.protobuf.struct_pb2 import Struct

from a2a import types as t
from aeos.a2a.server import REQUEST_KEY, data_part
from aeos.agents.specialists import SpecialistRequest, SpecialistResult
from aeos.core.budget import BudgetTracker
from aeos.core.events import Event, EventSink
from aeos.llm.base import LLMError
from aeos.telemetry.tracing import inject_context, traced

log = logging.getLogger("aeos.a2a.client")

_A2A_ERRORS: tuple[type[Exception], ...] = tuple(
    v for v in vars(a2a_errors).values() if isinstance(v, type) and issubclass(v, Exception)
)

TERMINAL = {
    a2a_pb2.TaskState.TASK_STATE_COMPLETED,
    a2a_pb2.TaskState.TASK_STATE_FAILED,
    a2a_pb2.TaskState.TASK_STATE_CANCELED,
    a2a_pb2.TaskState.TASK_STATE_REJECTED,
}


class A2AUnavailableError(Exception):
    """This endpoint is unreachable, hung, or misbehaving; try another."""


class A2ASpecialist:
    def __init__(
        self,
        owner: str,
        endpoints: list[str],
        *,
        api_key: str | None = None,
        timeout_s: float = 900.0,
        stall_s: float = 120.0,
    ) -> None:
        if not endpoints:
            raise ValueError(f"no A2A endpoints configured for {owner}")
        self.owner, self.endpoints = owner, endpoints
        self.api_key, self.timeout_s, self.stall_s = api_key, timeout_s, stall_s

    # ------------------------------------------------------------------ discovery
    async def discover(self, endpoint: str) -> t.AgentCard:
        """Fetch and sanity-check the agent card (also usable standalone)."""
        async with httpx.AsyncClient(timeout=10.0) as http:
            r = await http.get(f"{endpoint.rstrip('/')}/.well-known/agent-card.json")
            r.raise_for_status()
        card = t.AgentCard()
        json_format.Parse(r.text, card)
        if card.name != f"aeos-{self.owner}":
            raise A2AUnavailableError(f"{endpoint} is {card.name!r}, expected aeos-{self.owner}")
        return card

    # ------------------------------------------------------------------ run
    async def run(
        self, req: SpecialistRequest, tracker: BudgetTracker, sink: EventSink
    ) -> SpecialistResult:
        errors: list[str] = []
        for endpoint in self.endpoints:  # failover order
            try:
                with traced("a2a.delegate", owner=self.owner, endpoint=endpoint):
                    result = await asyncio.wait_for(
                        self._delegate(endpoint, req, tracker, sink), timeout=self.timeout_s
                    )
                return result
            except (A2AUnavailableError, TimeoutError, httpx.HTTPError, OSError, LLMError) as exc:
                errors.append(f"{endpoint}: {type(exc).__name__}: {exc}")
                last = endpoint == self.endpoints[-1]
                sink.emit(
                    Event(
                        type="a2a_failover",
                        task_id=req.task_id,
                        agent=self.owner,
                        message=f"{self.owner} service {endpoint} failed ({type(exc).__name__}); "
                        + ("no more endpoints" if last else "trying next endpoint"),
                    )
                )
        return SpecialistResult(
            owner=self.owner, status="error",
            detail="A2A delegation failed on every endpoint: " + " | ".join(errors),
        )  # fmt: skip

    async def _delegate(
        self, endpoint: str, req: SpecialistRequest, tracker: BudgetTracker, sink: EventSink
    ) -> SpecialistResult:
        card = await self.discover(endpoint)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        http = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(10.0, read=self.stall_s),  # a silent service is a dead service
        )
        client: Client = await create_client(
            card,
            ClientConfig(
                httpx_client=http,
                streaming=True,
                supported_protocol_bindings=["JSONRPC"],
                accepted_output_modes=["application/json"],
            ),  # fmt: skip
        )
        meta = Struct()
        meta.update({"trace": inject_context(),
                     "budget": tracker.budget.model_dump(mode="json")})  # fmt: skip
        message = t.Message(
            message_id=f"{req.task_id}-{req.step_id}-{req.attempt}",
            role=t.Role.ROLE_USER,
            parts=[data_part({REQUEST_KEY: req.model_dump(mode="json")})],
            metadata=meta,
        )
        result: SpecialistResult | None = None
        remote_task_id: str | None = None
        final_state: int | None = None
        final_text = ""
        try:
            async for ev in client.send_message(t.SendMessageRequest(message=message)):
                kind = ev.WhichOneof("payload")
                if kind == "task":
                    remote_task_id = ev.task.id
                elif kind == "status_update":
                    remote_task_id = ev.status_update.task_id
                    st = ev.status_update.status
                    text = (
                        " ".join(p.text for p in st.message.parts if p.text) if st.message else ""
                    )
                    if st.state in TERMINAL:
                        final_state, final_text = st.state, text
                    elif text:
                        sink.emit(Event(type="a2a_progress", task_id=req.task_id,
                                        agent=self.owner, message=text))  # fmt: skip
                elif kind == "artifact_update":
                    parts = get_data_parts(ev.artifact_update.artifact.parts)
                    if parts:
                        result = SpecialistResult.model_validate(parts[0])
        except (httpx.HTTPError, ConnectionError, *_A2A_ERRORS) as exc:  # type: ignore[misc]
            await self._best_effort_cancel(client, remote_task_id)
            raise A2AUnavailableError(str(exc) or type(exc).__name__) from exc
        except asyncio.CancelledError:  # our own deadline fired: tell the service to stop work
            await self._best_effort_cancel(client, remote_task_id)
            raise
        finally:
            with contextlib.suppress(Exception):
                await client.close()
            await http.aclose()

        if final_state == a2a_pb2.TaskState.TASK_STATE_COMPLETED and result is not None:
            for key, attr in (("llm_calls", "llm_calls"), ("tool_calls", "tool_calls"),
                              ("steps", "steps_used")):  # fmt: skip
                setattr(
                    tracker.budget,
                    attr,
                    getattr(tracker.budget, attr) + int(result.usage.get(key, 0)),
                )
            tracker.budget.tokens_used += int(result.usage.get("tokens", 0))
            tracker.budget.cost_usd += float(result.usage.get("cost_usd", 0.0))
            return result
        state_name = (
            a2a_pb2.TaskState.Name(final_state) if final_state is not None else "NO_FINAL_STATE"
        )
        detail = f"remote {self.owner} ended {state_name}: {final_text}"[:500]
        return SpecialistResult(owner=self.owner, status="error", detail=detail)

    @staticmethod
    async def _best_effort_cancel(client: Client, task_id: str | None) -> None:
        if task_id:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.cancel_task(t.CancelTaskRequest(id=task_id)), 5)


def a2a_error_types() -> tuple[type[BaseException], ...]:
    from a2a.utils import errors

    return tuple(
        v for v in vars(errors).values() if isinstance(v, type) and issubclass(v, Exception)
    )


def parse_endpoints(raw: str) -> dict[str, list[str]]:
    import json

    if not raw.strip():
        return {}
    data: dict[str, Any] = json.loads(raw)
    return {
        owner: [u.strip() for u in str(urls).split(",") if u.strip()]
        for owner, urls in data.items()
    }
