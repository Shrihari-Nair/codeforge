import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_infra import unhealthy  # noqa: E402


def line(service: str, state: str, health: str = "") -> str:
    return json.dumps({"Service": service, "State": state, "Health": health})


def test_all_healthy() -> None:
    out = "\n".join([line("postgres", "running", "healthy"), line("redis", "running", "healthy")])
    assert unhealthy(out) == []


def test_flags_unhealthy_and_stopped() -> None:
    out = "\n".join(
        [
            line("postgres", "running", "unhealthy"),
            line("redis", "exited"),
            line("otel-collector", "running"),  # no healthcheck: running is enough
        ]
    )
    assert unhealthy(out) == ["postgres", "redis"]


def test_empty_output_means_nothing_running() -> None:
    assert unhealthy("") == ["<no services running>"]
