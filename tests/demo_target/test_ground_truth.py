import json
from pathlib import Path

from demo_target.build_repo import REPO_DIR
from demo_target.history import BAD_COMMIT_KEY

from demo_target import __file__ as _  # noqa: F401
from tests.demo_target.conftest import run_benchmark

GT_PATH = Path(__file__).resolve().parents[2] / "demo_target" / "GROUND_TRUTH.json"


def _gt() -> dict:  # type: ignore[type-arg]
    return json.loads(GT_PATH.read_text())


def test_ground_truth_is_machine_readable_and_complete() -> None:
    gt = _gt()
    for key in ("root_cause", "red_herrings", "acceptable_fixes", "benchmark", "symptom"):
        assert gt[key]
    assert len(gt["red_herrings"]) >= 2


def test_ground_truth_sha_matches_the_build(sha: dict[str, str]) -> None:
    gt = _gt()
    assert gt["root_cause"]["commit_sha"] == sha[BAD_COMMIT_KEY]
    assert gt["last_good_commit_sha"] == sha["region"]
    assert gt["root_cause"]["commit_sha"] in gt["deploy_range_commits"]


def test_ground_truth_benchmark_numbers_match_reality(tree) -> None:  # type: ignore[no-untyped-def]
    gt = _gt()["benchmark"]
    assert run_benchmark(tree("region")) == {**gt["baseline"], "requests": 500}
    assert run_benchmark(tree("version")) == {**gt["regressed"], "requests": 500}


def test_repo_dir_constant() -> None:
    assert REPO_DIR.name == "checkout-service"
