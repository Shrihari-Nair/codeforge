import base64

from google.genai import types

from aeos.llm.base import Message, ToolCall
from aeos.llm.gemini import _from_response, _to_contents


def test_conversion_roundtrips_tool_calls_with_signatures() -> None:
    sig = base64.b64encode(b"sig-bytes").decode()
    call = ToolCall(
        id="c1", name="get_metrics", arguments={"a": 1}, extra={"thought_signature": sig}
    )
    msgs = [
        Message(role="system", content="sys A"),
        Message(role="system", content="sys B"),
        Message(role="user", content="go"),
        Message(role="assistant", content="thinking", tool_calls=[call]),
        Message(role="tool", tool_call_id="c1", content="result text"),
    ]
    system, contents = _to_contents(msgs)
    assert system == "sys A\n\nsys B"
    assert [c.role for c in contents] == ["user", "model", "user"]
    model_parts = contents[1].parts or []
    assert model_parts[0].text == "thinking"
    assert model_parts[1].function_call.name == "get_metrics"  # type: ignore[union-attr]
    assert model_parts[1].thought_signature == b"sig-bytes"
    resp = (contents[2].parts or [])[0].function_response
    assert resp is not None and resp.name == "get_metrics"
    assert resp.response == {"output": "result text"}


def test_parallel_tool_results_share_one_turn() -> None:
    calls = [ToolCall(id=f"c{i}", name="t", arguments={}) for i in range(2)]
    msgs = [
        Message(role="user", content="go"),
        Message(role="assistant", tool_calls=calls),
        Message(role="tool", tool_call_id="c0", content="r0"),
        Message(role="tool", tool_call_id="c1", content="r1"),
    ]
    _, contents = _to_contents(msgs)
    assert len(contents) == 3 and len(contents[2].parts or []) == 2


def test_response_parsing_extracts_calls_usage_and_signature() -> None:
    part = types.Part(
        function_call=types.FunctionCall(name="x", args={"k": "v"}), thought_signature=b"abc"
    )
    resp = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[part, types.Part(text="hi")]),
                finish_reason=types.FinishReason.STOP,
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=10, candidates_token_count=5, thoughts_token_count=7
        ),
    )
    out = _from_response(resp, "m")
    assert out.content == "hi" and out.finish_reason == "stop"
    assert out.tool_calls[0].name == "x" and out.tool_calls[0].arguments == {"k": "v"}
    assert base64.b64decode(out.tool_calls[0].extra["thought_signature"]) == b"abc"
    assert (out.usage.input_tokens, out.usage.output_tokens) == (10, 12)  # thoughts are output


def test_empty_candidates_is_blocked_not_a_crash() -> None:
    resp = types.GenerateContentResponse(candidates=[])
    out = _from_response(resp, "m")
    assert out.content == "" and out.tool_calls == []
