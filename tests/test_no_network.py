import socket

import pytest

from aeos.llm.base import LLMResponse, Message
from aeos.llm.fake import FakeLLM, ScriptStep


async def test_fake_llm_makes_zero_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    llm = FakeLLM([ScriptStep(response=LLMResponse(content="hi"))])
    assert (await llm.complete([Message(role="user", content="hello")])).content == "hi"
