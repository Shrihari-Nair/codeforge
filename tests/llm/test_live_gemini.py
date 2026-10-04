"""Live Gemini checks. Excluded from default runs: `uv run pytest -m live`."""

import pytest
from pydantic import BaseModel

from aeos.core.config import get_settings
from aeos.llm.base import Message, ToolSpec
from aeos.llm.factory import get_llm
from aeos.llm.structured import complete_structured

pytestmark = pytest.mark.live


@pytest.fixture
def llm():  # type: ignore[no-untyped-def]
    s = get_settings()
    if s.gemini_api_key is None:
        pytest.skip("no AEOS_GEMINI_API_KEY")
    return get_llm(s.model_copy(update={"llm_provider": "gemini"}))


async def test_tool_call_roundtrip(llm) -> None:  # type: ignore[no-untyped-def]
    spec = ToolSpec(
        name="add",
        description="Add two integers",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    )
    r1 = await llm.complete(
        [Message(role="user", content="What is 19 + 23? Use the tool.")], tools=[spec]
    )
    assert r1.tool_calls and r1.tool_calls[0].name == "add"
    call = r1.tool_calls[0]
    r2 = await llm.complete(
        [
            Message(role="user", content="What is 19 + 23? Use the tool."),
            Message(role="assistant", tool_calls=[call]),
            Message(role="tool", tool_call_id=call.id, content="42"),
        ],
        tools=[spec],
    )
    assert "42" in r2.content
    assert r2.usage.input_tokens > 0


async def test_structured_output(llm) -> None:  # type: ignore[no-untyped-def]
    class City(BaseModel):
        name: str
        country: str

    out, usage = await complete_structured(
        llm, system="Extract data.", user="Paris is the capital of France.", model=City
    )
    assert out.name == "Paris" and out.country == "France" and usage.output_tokens > 0
