"""The eval harness end to end on FakeLLM (real tools, real sandbox): CI's smoke eval."""

import argparse
import asyncio
import json

import pytest
from evals.env import load_tasks

from evals import run_evals

pytestmark = pytest.mark.docker


def args(**kw: object) -> argparse.Namespace:
    base = dict(suite="smoke", llm="fake", scenario="happy", break_="none", memory="off", templates="",
                limit=0, trials=1, parallel=3, task_timeout=300, label="pytest", baseline=None)  # fmt: skip
    return argparse.Namespace(**{**base, **kw})


async def run_suite(a: argparse.Namespace):  # type: ignore[no-untyped-def]
    tasks = run_evals.select(load_tasks(), a)
    sem = asyncio.Semaphore(a.parallel)
    return await asyncio.gather(*[run_evals.run_one(t, 0, a, sem) for t in tasks])


async def test_smoke_eval_solves_two_different_regressions(seeded_db: None, tmp_path) -> None:  # type: ignore[no-untyped-def]
    results = await run_suite(args(templates="cache_disabled,missing_index"))
    assert [r["success"] for r in results] == [True, True], [
        r["grades"] for r in results if not r["success"]
    ]
    for r in results:
        g = r["grades"]
        assert g["outcome_tests"]["passed"] and g["outcome_benchmark"]["passed"]
        assert g["root_cause_match"]["score"] == 1.0 and g["trace_policy"]["passed"]
        assert r["plans"] == 1 and r["status"] == "completed"


async def test_a_deliberately_broken_config_visibly_lowers_the_metrics(seeded_db: None) -> None:
    """The evals have signal: same scripted scenario, retry policy sabotaged -> success collapses."""
    healthy = await run_suite(
        args(templates="n_plus_one,pricing_per_item_query", scenario="wrong_fix")
    )
    broken = await run_suite(args(templates="n_plus_one,pricing_per_item_query", scenario="wrong_fix",
                                  break_="retry"))  # fmt: skip
    h, b = run_evals.aggregate(healthy), run_evals.aggregate(broken)
    assert (
        h["overall"]["success_rate"]["mean"] == 1.0 and h["overall"]["recovery_rate"]["mean"] == 1.0
    )
    assert (
        b["overall"]["success_rate"]["mean"] == 0.0
        and b["overall"]["human_intervention_rate"]["mean"] == 1.0
    )
    rows = {r["metric"]: r for r in run_evals.compare(b, h)}
    assert rows["success_rate"]["regressed"] and rows["recovery_rate"]["regressed"]


async def test_a_crashing_run_is_recorded_as_a_failure_not_hidden(
    seeded_db: None, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("model exploded")

    monkeypatch.setattr(run_evals, "make_llm", boom)
    (r,) = await run_suite(args(templates="n_plus_one"))
    assert r["status"] == "crashed" and r["success"] is False and "model exploded" in r["error"]
    assert all(g["passed"] is False for g in r["grades"].values())


def test_result_files_carry_version_hashes_so_runs_can_be_compared() -> None:
    v = run_evals.code_version()
    assert set(v) == {"code", "prompts", "policy", "dataset", "graders"} and all(
        len(x) == 12 for x in v.values()
    )
    assert json.dumps(v)  # serialisable into every result file
