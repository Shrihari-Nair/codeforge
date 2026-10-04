from pathlib import Path

import psycopg
import pytest

from aeos.core.config import get_settings


@pytest.fixture(scope="session")
def remote_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from demo_target.build_repo import build, build_remote

    build()
    return build_remote(dest=tmp_path_factory.mktemp("remote") / "checkout-service.git")


@pytest.fixture(scope="session")
def seeded_db() -> None:
    """Ensure the demo schema is loaded; skip MCP DB tests if Postgres is unavailable."""
    from demo_target.build_repo import load_commits
    from demo_target.datagen import generate
    from demo_target.seed import DDL, load

    try:
        with psycopg.connect(get_settings().database_url, connect_timeout=2) as conn:
            conn.execute(DDL)  # idempotent; also creates the read-only role
            conn.commit()
            exists = conn.execute("SELECT to_regclass('demo.metrics')").fetchone()
            count = 0
            if exists and exists[0]:
                row = conn.execute("SELECT count(*) FROM demo.metrics").fetchone()
                count = row[0] if row else 0
            if count != 1440:
                load(conn, generate(load_commits()))
    except psycopg.OperationalError:
        pytest.skip("Postgres not available")


@pytest.fixture(autouse=True)
def memory_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Long-term memory is on in production but off in tests unless a test turns it on."""
    monkeypatch.setenv("AEOS_MEMORY_ENABLED", "false")
    monkeypatch.setenv("AEOS_TELEMETRY_ENABLED", "false")  # no exporting from tests (or children)
    get_settings.cache_clear()


@pytest.fixture
def spans():  # type: ignore[no-untyped-def]
    """In-memory span capture for the duration of a test."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from aeos.telemetry import tracing

    tracing.reset_for_tests()
    exporter = InMemorySpanExporter()
    tracing.setup_telemetry("aeos-test", exporter=exporter)
    yield exporter
    tracing.reset_for_tests()
