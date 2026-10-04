"""Tracing wrapper for any LLM provider: one `llm.call` span per call, with the prompt, the
completion, token usage and estimated cost (Langfuse-compatible attributes), plus metrics."""

import json
import time
from typing import Any

from opentelemetry.trace import SpanKind

from aeos.llm.base import LLMProvider, LLMResponse, Message, ToolSpec
from aeos.llm.pricing import estimate_cost
from aeos.telemetry import metrics as m
from aeos.telemetry.tracing import traced


def _messages_json(messages: list[Message]) -> str:
    return json.dumps(
        [
            {
                "role": x.role,
                "content": x.content[:3000],
                **(
                    {
                        "tool_calls": [
                            {"name": c.name, "arguments": c.arguments} for c in x.tool_calls
                        ]
                    }
                    if x.tool_calls
                    else {}
                ),
            }  # fmt: skip
            for x in messages
        ],
        default=str,
    )


class TracedLLM(LLMProvider):
    def __init__(self, inner: LLMProvider, system: str = "gemini") -> None:
        self.inner, self.system = inner, system

    def __getattr__(self, name: str) -> Any:  # transparently expose .model, .routes, ...
        return getattr(self.inner, name)

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        tool_choice: str | None = None,
    ) -> LLMResponse:
        model = str(getattr(self.inner, "model", "") or "unknown")
        start = time.perf_counter()
        with traced(
            f"llm.call {model}",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.system": self.system,
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": model,
                "gen_ai.request.max_tokens": max_tokens,
                "langfuse.observation.type": "generation",
                "langfuse.observation.input": _messages_json(messages),
                "aeos.llm.tools_offered": len(tools or []),
                "aeos.llm.tool_choice": tool_choice,
            },
        ) as span:
            resp = await self.inner.complete(
                messages, tools=tools, max_tokens=max_tokens, tool_choice=tool_choice
            )
            used = resp.model or model
            cost = estimate_cost(used, resp.usage.input_tokens, resp.usage.output_tokens)
            calls = [{"name": c.name, "arguments": c.arguments} for c in resp.tool_calls]
            output = {"content": resp.content, "tool_calls": calls}
            span.set("gen_ai.response.model", used)
            span.set("gen_ai.response.finish_reasons", resp.finish_reason)
            span.set("gen_ai.usage.input_tokens", resp.usage.input_tokens)
            span.set("gen_ai.usage.output_tokens", resp.usage.output_tokens)
            span.set("aeos.llm.cost_usd", round(cost, 6))
            span.set("langfuse.observation.model.name", used)
            span.set("langfuse.observation.output", json.dumps(output, default=str))
            usage = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}
            span.set("langfuse.observation.usage_details", usage)
            span.set("langfuse.observation.cost_details", {"total": round(cost, 6)})
        attrs = {"model": used}
        m.llm_calls.add(1, attrs)
        m.llm_tokens.add(resp.usage.input_tokens, {**attrs, "direction": "input"})
        m.llm_tokens.add(resp.usage.output_tokens, {**attrs, "direction": "output"})
        m.llm_cost.add(cost, attrs)
        m.llm_duration.record((time.perf_counter() - start) * 1000, attrs)
        return resp
