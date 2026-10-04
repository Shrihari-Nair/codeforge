from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    # Opaque provider data that must be echoed back (e.g. Gemini thought signatures).
    extra: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class LLMResponse(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    model: str = ""
    finish_reason: str = ""  # "stop", "length", "blocked", ... (provider-normalised)


class LLMError(Exception):
    """Base class for provider errors."""


class LLMTransientError(LLMError):
    """Rate limit / overload / timeout: safe to retry later."""


class LLMProvider(ABC):
    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
        tool_choice: str | None = None,
    ) -> LLMResponse:
        """`tool_choice`: None = model decides; "any" = must call some tool; else a tool name."""
        ...
