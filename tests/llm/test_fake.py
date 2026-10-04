import pytest

from aeos.llm.base import LLMResponse, Message, ToolCall
from aeos.llm.fake import FakeLLM, ScriptExhaustedError, ScriptStep, UnexpectedPromptError

USER = [Message(role="user", content="find the regression")]


async def test_returns_scripted_responses_in_order() -> None:
    llm = FakeLLM(
        [
            ScriptStep(response=LLMResponse(content="one")),
            ScriptStep(response=LLMResponse(content="two")),
        ]
    )
    assert (await llm.complete(USER)).content == "one"
    assert (await llm.complete(USER)).content == "two"
    assert llm.remaining == 0


async def test_supports_tool_call_responses() -> None:
    call = ToolCall(id="c1", name="search_logs", arguments={"q": "error"})
    llm = FakeLLM([ScriptStep(response=LLMResponse(tool_calls=[call]))])
    resp = await llm.complete(USER)
    assert resp.tool_calls[0].name == "search_logs"
    assert resp.tool_calls[0].arguments == {"q": "error"}


async def test_exhausted_script_fails_loudly() -> None:
    llm = FakeLLM([])
    with pytest.raises(ScriptExhaustedError):
        await llm.complete(USER)


async def test_expect_in_prompt_mismatch_fails_loudly() -> None:
    llm = FakeLLM([ScriptStep(response=LLMResponse(content="x"), expect_in_prompt="benchmark")])
    with pytest.raises(UnexpectedPromptError, match="benchmark"):
        await llm.complete(USER)


async def test_expect_in_prompt_match_passes() -> None:
    llm = FakeLLM([ScriptStep(response=LLMResponse(content="ok"), expect_in_prompt="regression")])
    assert (await llm.complete(USER)).content == "ok"


async def test_records_calls_and_ignores_system_for_matching() -> None:
    llm = FakeLLM([ScriptStep(response=LLMResponse(content="ok"), expect_in_prompt="regression")])
    msgs = [Message(role="system", content="you are an agent"), *USER]
    await llm.complete(msgs)
    assert llm.calls == [msgs]


async def test_returned_response_is_a_copy() -> None:
    step = ScriptStep(response=LLMResponse(content="a"))
    llm = FakeLLM([step])
    resp = await llm.complete(USER)
    resp.content = "mutated"
    assert step.response.content == "a"
