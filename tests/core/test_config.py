import pytest
from pydantic import ValidationError

from aeos.core.config import Settings


def test_defaults_point_at_local_compose() -> None:
    s = Settings(_env_file=None)
    assert s.database_url.startswith("postgresql://")
    assert s.redis_url.startswith("redis://")
    assert s.llm_provider == "fake"


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEOS_LOG_LEVEL", "DEBUG")
    assert Settings(_env_file=None).log_level == "DEBUG"


def test_invalid_log_level_names_the_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEOS_LOG_LEVEL", "LOUD")
    with pytest.raises(ValidationError, match="log_level"):
        Settings(_env_file=None)


def test_invalid_database_url_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEOS_DATABASE_URL", "not-a-url")
    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None)


def test_empty_secret_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEOS_GEMINI_API_KEY", "")
    assert Settings(_env_file=None).gemini_api_key is None


def test_secrets_not_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEOS_GEMINI_API_KEY", "sk-super-secret")
    s = Settings(_env_file=None)
    assert "sk-super-secret" not in repr(s)
    assert "sk-super-secret" not in str(s.model_dump())
