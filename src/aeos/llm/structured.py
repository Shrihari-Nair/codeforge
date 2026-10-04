"""One-shot structured output: force a `submit` tool call whose arguments are a Pydantic model."""

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from aeos.llm.base import LLMError, LLMProvider, Message, ToolSpec, Usage


class StructuredOutputError(LLMError):
    pass


def clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Drop pydantic noise (titles) that LLM tool schemas do not need."""

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    out: dict[str, Any] = walk(schema)
    return out


async def complete_structured[T: BaseModel](
    llm: LLMProvider,
    *,
    system: str,
    user: str,
    model: type[T],
    max_tokens: int = 4096,
    retries: int = 1,
) -> tuple[T, Usage]:
    spec = ToolSpec(
        name="submit",
        description=f"Submit the final {model.__name__}. Call exactly once.",
        parameters=clean_schema(model.model_json_schema()),
    )
    messages = [
        Message(role="system", content=system),
        Message(role="user", content=user),
    ]
    total = Usage()
    error = "no tool call"
    for _ in range(retries + 1):
        resp = await llm.complete(
            messages, tools=[spec], max_tokens=max_tokens, tool_choice="submit"
        )
        total = Usage(
            input_tokens=total.input_tokens + resp.usage.input_tokens,
            output_tokens=total.output_tokens + resp.usage.output_tokens,
        )
        call = next((c for c in resp.tool_calls if c.name == "submit"), None)
        if call is None:
            error = f"model did not call submit (finish_reason={resp.finish_reason!r})"
        else:
            try:
                return model.model_validate(call.arguments), total
            except ValidationError as exc:
                error = str(exc)[:600]
                messages += [
                    Message(role="assistant", tool_calls=[call]),
                    Message(role="tool", tool_call_id=call.id, content=f"INVALID: {error}"),
                    Message(role="user", content="Fix the errors and call submit again."),
                ]
                continue
        messages.append(Message(role="user", content="You must call the submit tool."))
    raise StructuredOutputError(
        f"{model.__name__}: {error} :: {json.dumps(messages[-1].content)[:200]}"
    )
