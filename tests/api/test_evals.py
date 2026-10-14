import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aeos.api.evals import evals_router


def write(dir_: Path, name: str, **extra: object) -> None:
    body = {
        "meta": {"run_id": name.split("_")[0], "label": name.split("_", 1)[1], "llm": "fake"},
        "aggregate": {
            "overall": {"tasks": 2, "success_rate": {"mean": 0.5, "stderr": 0.1, "n": 2}}
        },
        "runs": [{"task_id": "t1", "template": "a", "success": True, "root_cause": {"big": "x"}}],
        **extra,
    }
    (dir_ / f"{name}.json").write_text(json.dumps(body))


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    write(tmp_path, "20261001T000000Z_old")
    write(tmp_path, "20261002T000000Z_new", vs_baseline={"deltas": []})
    (tmp_path / "20261003T000000Z_broken.json").write_text("{not json")
    (tmp_path / "notes.json").write_text("{}")  # not a run id: ignored
    app = FastAPI()
    app.include_router(evals_router(tmp_path))
    return TestClient(app)


def test_lists_newest_first_and_skips_bad_files(client: TestClient) -> None:
    runs = client.get("/evals").json()
    assert [r["id"] for r in runs] == ["20261002T000000Z_new", "20261001T000000Z_old"]
    assert runs[0]["compared_to_baseline"] is True and runs[1]["compared_to_baseline"] is False
    assert runs[0]["overall"]["success_rate"]["mean"] == 0.5


def test_run_detail_slims_rows(client: TestClient) -> None:
    run = client.get("/evals/20261002T000000Z_new").json()
    assert run["aggregate"]["overall"]["tasks"] == 2
    assert run["runs"] == [
        {
            "task_id": "t1", "template": "a", "status": None, "success": True, "latency_s": None,
            "cost_usd": None, "llm_calls": None, "plans": None, "verifications": None,
            "had_failure": None,
        }
    ]  # fmt: skip


@pytest.mark.parametrize("bad", ["nope", "..%2F..%2Fetc%2Fpasswd", "20261003T000000Z_broken"])
def test_unknown_or_unreadable_run_is_404(client: TestClient, bad: str) -> None:
    assert client.get(f"/evals/{bad}").status_code == 404
