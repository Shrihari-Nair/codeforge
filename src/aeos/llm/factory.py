from aeos.core.config import Settings, get_settings
from aeos.llm.base import LLMError, LLMProvider


def get_llm(settings: Settings | None = None, *, strong: bool = False) -> LLMProvider:
    """Build the configured real provider. (Tests construct FakeLLM directly.)"""
    s = settings or get_settings()
    if s.llm_provider == "gemini":
        from aeos.llm.gemini import GeminiProvider

        if s.gemini_api_key is None:
            raise LLMError("AEOS_GEMINI_API_KEY is not set")
        return GeminiProvider(
            s.gemini_api_key.get_secret_value(),
            s.gemini_strong_model if strong else s.gemini_model,
            thinking_budget=s.gemini_thinking_budget,
        )
    raise LLMError(
        f"llm_provider={s.llm_provider!r} has no live implementation; "
        "set AEOS_LLM_PROVIDER=gemini or inject a FakeLLM"
    )
