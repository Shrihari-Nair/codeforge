import argparse

import pytest

from aeos.cli import _llm
from aeos.llm.base import LLMError
from aeos.llm.fake import RoutedFakeLLM


def ns(**kw: object) -> argparse.Namespace:
    return argparse.Namespace(**{"replay": None, "llm": None, **kw})


def test_replay_flag_selects_the_scripted_llm() -> None:
    assert isinstance(_llm(ns(replay="happy")), RoutedFakeLLM)


def test_replay_env_var_works_for_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEOS_REPLAY", "wrong_fix")
    assert isinstance(_llm(ns()), RoutedFakeLLM)


def test_no_replay_and_fake_provider_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AEOS_REPLAY", raising=False)
    with pytest.raises(LLMError, match="no live implementation"):
        _llm(ns())


def test_agent_subcommand_gets_the_replay_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: `aeos agent` ignored AEOS_REPLAY because its call site had its own condition."""
    import aeos.a2a.service as service
    from aeos import cli

    seen: dict[str, object] = {}

    async def fake_serve(owner: str, **kw: object) -> None:
        seen.update(owner=owner, **kw)

    monkeypatch.setattr(service, "serve", fake_serve)
    monkeypatch.setenv("AEOS_REPLAY", "happy")
    assert cli.main(["agent", "research", "--port", "9999"]) == 0
    assert isinstance(seen["llm"], RoutedFakeLLM)
