"""Process-level telemetry bootstrap used by every entrypoint (CLI, workers, MCP servers)."""

from aeos.core.config import Settings, get_settings
from aeos.telemetry.tracing import is_configured, register_secrets, setup_telemetry


def init_telemetry(service: str, settings: Settings | None = None) -> None:
    """Register secrets for redaction (always) and start exporting if enabled and not yet done."""
    s = settings or get_settings()
    secrets = [
        v.get_secret_value()
        for v in (s.gemini_api_key, s.langfuse_secret_key, s.langfuse_public_key, s.a2a_api_key)
        if v is not None
    ]
    register_secrets(secrets)
    if s.telemetry_enabled and not is_configured():
        setup_telemetry(service, endpoint=s.otel_exporter_endpoint)
