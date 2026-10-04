"""Gemini provider (google-genai). The only place that talks to Gemini."""

import asyncio
import base64
import logging
import random
import uuid
from typing import Any

from google import genai
from google.genai import errors, types

from aeos.llm.base import (
    LLMError,
    LLMProvider,
    LLMResponse,
    LLMTransientError,
    Message,
    ToolCall,
    ToolSpec,
    Usage,
)

log = logging.getLogger("aeos.llm.gemini")

_FINISH = {"STOP": "stop", "MAX_TOKENS": "length"}
# Gemini sometimes returns an unusable turn (garbled function call, empty "other"): worth retrying.
_RETRYABLE_FINISH = {"malformed_function_call", "other", "unexpected_tool_call"}


def _to_contents(messages: list[Message]) -> tuple[str | None, list[types.Content]]:
    system: list[str] = []
    contents: list[types.Content] = []
    names: dict[str, str] = {}  # tool_call_id -> function name
    for m in messages:
        if m.role == "system":
            system.append(m.content)
        elif m.role == "user":
            contents.append(types.Content(role="user", parts=[types.Part(text=m.content)]))
        elif m.role == "assistant":
            parts: list[types.Part] = []
            if m.content:
                parts.append(types.Part(text=m.content))
            for tc in m.tool_calls:
                names[tc.id] = tc.name
                call = types.FunctionCall(
                    name=tc.name, args=tc.arguments, id=tc.extra.get("gemini_id")
                )
                part = types.Part(function_call=call)
                if sig := tc.extra.get("thought_signature"):
                    part.thought_signature = base64.b64decode(sig)
                parts.append(part)
            if parts:
                contents.append(types.Content(role="model", parts=parts))
        else:  # tool result
            name = names.get(m.tool_call_id or "", "tool")
            part = types.Part(
                function_response=types.FunctionResponse(name=name, response={"output": m.content})
            )
            last = contents[-1] if contents else None
            if (
                last is not None
                and last.role == "user"
                and last.parts
                and all(p.function_response for p in last.parts)
            ):
                last.parts.append(part)  # parallel results travel in one turn
            else:
                contents.append(types.Content(role="user", parts=[part]))
    return ("\n\n".join(system) if system else None), contents


def _from_response(resp: Any, model: str) -> LLMResponse:
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    finish = ""
    if resp.candidates:
        cand = resp.candidates[0]
        raw = getattr(cand.finish_reason, "name", str(cand.finish_reason))
        finish = _FINISH.get(
            raw, raw.lower()
        )  # keep the real reason, e.g. "malformed_function_call"
        for part in (cand.content.parts or []) if cand.content else []:
            if part.function_call:
                fc = part.function_call
                extra: dict[str, Any] = {}
                if fc.id:
                    extra["gemini_id"] = fc.id
                if part.thought_signature:
                    extra["thought_signature"] = base64.b64encode(part.thought_signature).decode()
                calls.append(
                    ToolCall(
                        id=fc.id or f"call_{uuid.uuid4().hex[:10]}",
                        name=fc.name or "",
                        arguments=dict(fc.args or {}),
                        extra=extra,
                    )
                )
            elif part.text and not part.thought:
                text_parts.append(part.text)
    elif getattr(resp, "prompt_feedback", None):
        finish = "blocked"
    usage = resp.usage_metadata
    return LLMResponse(
        content="".join(text_parts),
        tool_calls=calls,
        usage=Usage(
            input_tokens=(usage.prompt_token_count or 0) if usage else 0,
            output_tokens=((usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0))
            if usage
            else 0,
        ),
        model=model,
        finish_reason="stop" if calls and not finish else finish,
    )


class GeminiProvider(LLMProvider):
    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash",
        *,
        thinking_budget: int | None = 1024,
        max_retries: int = 4,
        client: genai.Client | None = None,
    ) -> None:
        self.model = model
        self._thinking_budget = thinking_budget
        self._max_retries = max_retries
        self._client = client or genai.Client(api_key=api_key)

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        tool_choice: str | None = None,
    ) -> LLMResponse:
        system, contents = _to_contents(messages)
        cfg: dict[str, Any] = {
            "system_instruction": system,
            "max_output_tokens": max_tokens + (self._thinking_budget or 0),
            "temperature": 0.2,
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
        }
        if self._thinking_budget is not None:
            cfg["thinking_config"] = types.ThinkingConfig(thinking_budget=self._thinking_budget)
        if tools:
            cfg["tools"] = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t.name,
                            description=t.description,
                            parameters_json_schema=t.parameters,
                        )
                        for t in tools
                    ]
                )
            ]
            if tool_choice:
                fcc = (
                    types.FunctionCallingConfig(mode=types.FunctionCallingConfigMode.ANY)
                    if tool_choice == "any"
                    else types.FunctionCallingConfig(
                        mode=types.FunctionCallingConfigMode.ANY,
                        allowed_function_names=[tool_choice],
                    )
                )
                cfg["tool_config"] = types.ToolConfig(function_calling_config=fcc)
        config = types.GenerateContentConfig(**cfg)

        last: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.aio.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
                out = _from_response(resp, self.model)
                unusable = not out.tool_calls and not out.content
                if (
                    unusable
                    and out.finish_reason in _RETRYABLE_FINISH
                    and attempt < self._max_retries
                ):
                    log.warning(
                        "gemini returned an unusable turn", extra={"finish": out.finish_reason}
                    )
                    last = LLMTransientError(f"unusable response ({out.finish_reason})")
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                return out
            except errors.APIError as exc:
                transient = exc.code in (429, 500, 502, 503, 504)
                if not transient:
                    raise LLMError(f"gemini error {exc.code}: {exc.message}") from exc
                last = exc
            except (TimeoutError, ConnectionError) as exc:
                last = exc
            if attempt < self._max_retries:
                delay = min(2**attempt, 20) + random.uniform(0, 0.5)  # noqa: S311
                log.warning("gemini transient error, retrying", extra={"attempt": attempt + 1})
                await asyncio.sleep(delay)
        raise LLMTransientError(f"gemini unavailable after retries: {last!r}")
