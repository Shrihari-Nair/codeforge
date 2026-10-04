"""Tool definitions and the tool runtime: every agent tool call goes through `ToolRuntime`.

The runtime enforces the risk gate, records the call (with a deterministic idempotency key for
writes), traces it, truncates results, and labels returned data with its trust origin so agents
treat it as data, never as instructions.
"""

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from opentelemetry.trace import Status, StatusCode

from aeos.core.budget import BudgetTracker
from aeos.core.events import Event, EventSink
from aeos.core.risk import ApprovalGrant, ApprovalRequired, CallContext, RiskGate
from aeos.core.state import Origin, RiskDecision, ToolCallRecord
from aeos.failure.crash import maybe_crash
from aeos.llm.base import ToolCall, ToolSpec
from aeos.mcp_client import McpClient, McpToolError, McpTransportError
from aeos.telemetry import metrics as m
from aeos.telemetry import traced

MAX_RESULT_CHARS = 6000
IDEMPOTENCY_ARG = "idempotency_key"


@dataclass(frozen=True)
class ToolContext:
    task_id: str
    step_id: str
    agent: str
    attempt: int = 1


@dataclass
class ToolOutput:
    content: str
    is_error: bool = False
    data: Any = None


ToolFn = Callable[[dict[str, Any], ToolContext], Awaitable[ToolOutput]]


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]
    run: ToolFn
    server: str
    risk: str = "read"
    result_origin: Origin = "untrusted"
    injects_idempotency_key: bool = False
    mutates: bool = False  # changes state the model observes (resets stagnation counting)

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)


def idempotency_key(ctx: ToolContext, tool: str, args: dict[str, Any]) -> str:
    """Stable across resume: same task/step/attempt/tool/args -> same key -> no duplicate effect."""
    canon = json.dumps(args, sort_keys=True, default=str)
    raw = f"{ctx.task_id}|{ctx.step_id}|{ctx.attempt}|{tool}|{canon}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# Results of these tools are numbers/metadata we produce or compute, not free text from third
# parties. Everything else (logs, code, pages, command output) is treated as untrusted data.
_TRUSTED_RESULTS = {
    "get_metrics", "get_error_rate", "compare_time_windows", "list_deployments", "list_commits",
    "get_repository", "get_schema", "explain_query",
}  # fmt: skip


def _clip(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    return (
        text if len(text) <= limit else text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
    )


async def mcp_tooldefs(
    client: McpClient, server: str, *, include: set[str] | None = None
) -> dict[str, ToolDef]:
    """Wrap an MCP server's tools. Idempotency keys are hidden from the model and injected."""
    defs: dict[str, ToolDef] = {}
    for name, info in (await client.list_tools()).items():
        if include is not None and name not in include:
            continue
        schema = json.loads(json.dumps(info.input_schema))
        props = schema.get("properties", {})
        injects = IDEMPOTENCY_ARG in props
        if injects:
            props.pop(IDEMPOTENCY_ARG)
            schema["required"] = [r for r in schema.get("required", []) if r != IDEMPOTENCY_ARG]

        async def run(
            args: dict[str, Any], ctx: ToolContext, _name: str = name, _inject: bool = injects
        ) -> ToolOutput:
            call_args = dict(args)
            if _inject:
                call_args[IDEMPOTENCY_ARG] = idempotency_key(ctx, _name, args)
            try:
                result = await client.call(_name, call_args)
            except McpToolError as exc:
                return ToolOutput(f"[{exc.code}] {exc.message}", is_error=True)
            except McpTransportError as exc:
                return ToolOutput(f"[transport_error] {exc}", is_error=True)
            data = result.data if result.data is not None else result.text
            return ToolOutput(json.dumps(data, default=str), data=data)

        defs[name] = ToolDef(
            name=name,
            description=info.description,
            parameters=schema,
            run=run,
            server=server,
            risk=info.risk or "write_irreversible",  # unknown risk is treated as the worst case
            result_origin="trusted" if name in _TRUSTED_RESULTS else "untrusted",
            injects_idempotency_key=injects,
            mutates=info.risk not in (None, "read"),
        )
    return defs


@dataclass
class ToolRuntime:
    tools: dict[str, ToolDef]
    ctx: ToolContext
    gate: RiskGate
    sink: EventSink
    tracker: BudgetTracker
    records: list[ToolCallRecord] = field(default_factory=list)
    decisions: list[RiskDecision] = field(default_factory=list)
    grants: list[ApprovalGrant] = field(default_factory=list)
    consumed_untrusted: bool = False  # has this agent read untrusted data yet?
    last_output: ToolOutput | None = None
    diff_lines: int = 0
    files_touched: int = 0

    def specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self.tools.values()]

    async def execute(self, call: ToolCall) -> str:
        """Run one tool call; returns the text the model sees. Raises ApprovalRequired."""
        tool = self.tools.get(call.name)
        if tool is None:
            known = sorted(self.tools)
            return f'<tool_error tool="{call.name}">unknown tool; available: {known}</tool_error>'
        decision = self.gate.decide(
            CallContext(
                tool=tool.name,
                risk=tool.risk,
                server=tool.server,
                arguments=call.arguments,
                origin="untrusted_only" if self.consumed_untrusted else "trusted",
                step_id=self.ctx.step_id,
                diff_lines=self.diff_lines,
                files_touched=self.files_touched,
                grant=next((g for g in self.grants if g.tool == tool.name), None),
            )
        )
        self.decisions.append(decision)
        if decision.decision == "deny":
            return (
                f'<tool_error tool="{tool.name}">DENIED by policy: {decision.reason}</tool_error>'
            )
        if decision.decision == "require_approval":
            raise ApprovalRequired(decision, tool.name, call.arguments)

        self.tracker.add_tool_call()
        self.sink.emit(
            Event(
                type="tool_call", task_id=self.ctx.task_id, agent=self.ctx.agent,
                message=f"{tool.name}({_brief(call.arguments)})",
                data={"tool": tool.name, "server": tool.server, "risk": tool.risk},
            )
        )  # fmt: skip
        start = time.perf_counter()
        maybe_crash(f"tool:{tool.name}:before")
        with traced(
            f"tool.call {tool.name}",
            attributes={
                "aeos.tool.name": tool.name, "aeos.tool.server": tool.server,
                "aeos.tool.risk": tool.risk, "aeos.tool.result_origin": tool.result_origin,
                "aeos.agent": self.ctx.agent, "aeos.step.id": self.ctx.step_id,
                "aeos.policy.rule": decision.policy_rule, "aeos.tool.arguments": call.arguments,
                "langfuse.observation.type": "span", "langfuse.observation.input": call.arguments,
            },
        ) as span:  # fmt: skip
            try:
                out = await tool.run(call.arguments, self.ctx)
            except Exception as exc:  # a tool bug must not kill the agent loop
                out = ToolOutput(f"[tool_crashed] {type(exc).__name__}: {exc}", is_error=True)
            span.set("aeos.tool.is_error", out.is_error)
            span.set("langfuse.observation.output", _clip(out.content, 1500))
            if out.is_error:
                span.span.set_status(Status(StatusCode.ERROR, _clip(out.content, 200)))
        duration_ms = (time.perf_counter() - start) * 1000
        outcome = "error" if out.is_error else "ok"
        m.tool_calls.add(1, {"server": tool.server, "tool": tool.name, "outcome": outcome})
        m.tool_duration.record(duration_ms, {"server": tool.server, "tool": tool.name})
        maybe_crash(f"tool:{tool.name}:after")  # effect happened, record not yet checkpointed
        self.last_output = out
        self.records.append(
            ToolCallRecord(
                id=call.id,
                step_id=self.ctx.step_id,
                agent=self.ctx.agent,
                server=tool.server,
                tool=tool.name,
                arguments=call.arguments,
                result_preview=_clip(out.content, 500),
                is_error=out.is_error,
                risk=tool.risk,
                origin=tool.result_origin,
                idempotency_key=idempotency_key(self.ctx, tool.name, call.arguments)
                if tool.injects_idempotency_key
                else None,
                duration_ms=round((time.perf_counter() - start) * 1000, 2),
            )
        )
        text = _clip(out.content)
        if not out.is_error and tool.result_origin == "untrusted":
            self.consumed_untrusted = True
        if out.is_error:
            return f'<tool_error tool="{tool.name}">{text}</tool_error>'
        origin = tool.result_origin
        return f'<tool_result tool="{tool.name}" origin="{origin}">\n{text}\n</tool_result>'


def _brief(args: dict[str, Any], limit: int = 90) -> str:
    text = json.dumps(args, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."
