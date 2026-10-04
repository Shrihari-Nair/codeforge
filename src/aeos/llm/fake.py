from pydantic import BaseModel

from aeos.llm.base import LLMError, LLMProvider, LLMResponse, Message, ToolSpec


class ScriptStep(BaseModel):
    response: LLMResponse
    expect_in_prompt: str | None = None


class ScriptExhaustedError(LLMError):
    pass


class UnexpectedPromptError(LLMError):
    pass


class FakeLLM(LLMProvider):
    """Scripted, deterministic provider. Replies in order; fails loudly on drift."""

    def __init__(self, script: list[ScriptStep]) -> None:
        self._script = list(script)
        self._index = 0
        self.calls: list[list[Message]] = []

    @property
    def remaining(self) -> int:
        return len(self._script) - self._index

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        tool_choice: str | None = None,
    ) -> LLMResponse:
        self.calls.append([m.model_copy(deep=True) for m in messages])  # snapshot, not alias
        if self._index >= len(self._script):
            raise ScriptExhaustedError(
                f"FakeLLM script exhausted after {len(self._script)} step(s)"
            )
        step = self._script[self._index]
        if step.expect_in_prompt is not None:
            last = next((m.content for m in reversed(messages) if m.role != "system"), "")
            if step.expect_in_prompt not in last:
                raise UnexpectedPromptError(
                    f"step {self._index}: expected {step.expect_in_prompt!r} "
                    f"in prompt, got {last!r}"
                )
        self._index += 1
        return step.response.model_copy(deep=True)


class RoutedFakeLLM(LLMProvider):
    """Routes each call to a per-role FakeLLM, chosen by a marker in the system prompt.

    Concurrent agents (e.g. analytics + research) each follow their own script, so the run is
    deterministic regardless of scheduling.
    """

    def __init__(self, routes: dict[str, FakeLLM]) -> None:
        self.routes = routes

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        tool_choice: str | None = None,
    ) -> LLMResponse:
        system = "\n".join(m.content for m in messages if m.role == "system")
        for marker, llm in self.routes.items():
            if marker in system:
                return await llm.complete(
                    messages, tools=tools, max_tokens=max_tokens, tool_choice=tool_choice
                )
        raise UnexpectedPromptError(f"no scripted route matches system prompt: {system[:120]!r}")

    @property
    def remaining(self) -> dict[str, int]:
        return {marker: llm.remaining for marker, llm in self.routes.items()}
