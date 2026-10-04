from datetime import timedelta

import pytest
from demo_target.datagen import BURST_START, DEPLOY_TS, END, START, Dataset, generate


@pytest.fixture(scope="module")
def data(commits_module: list[dict[str, str]]) -> Dataset:
    return generate(commits_module, seed=7)


@pytest.fixture(scope="module")
def commits_module() -> list[dict[str, str]]:
    from demo_target.build_repo import build

    return build()


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def test_reproducible_by_seed(commits_module: list[dict[str, str]]) -> None:
    assert generate(commits_module, seed=7) == generate(commits_module, seed=7)
    assert (
        generate(commits_module, seed=8)["metrics"] != generate(commits_module, seed=7)["metrics"]
    )


def test_timestamps_within_window(data: Dataset) -> None:
    for table in ("logs", "metrics", "spans"):
        key = "start_ts" if table == "spans" else "ts"
        assert all(START <= r[key] < END for r in data[table]), table


def test_latency_steps_up_at_deploy(data: Dataset) -> None:
    before = [m["p95_ms"] for m in data["metrics"] if m["ts"] < DEPLOY_TS]
    after = [m["p95_ms"] for m in data["metrics"] if m["ts"] >= DEPLOY_TS]
    assert _mean(after) >= 3 * _mean(before)


def test_deployments_end_with_bad_commit_range(
    data: Dataset, commits_module: list[dict[str, str]]
) -> None:
    last = data["deployments"][-1]
    assert last["ts"] == DEPLOY_TS and last["version"] == "v1.2.0"
    assert len(data["deployments"]) == 3
    assert last["commit_sha"] in {c["sha"] for c in commits_module}


def test_request_logs_show_db_query_growth(data: Dataset) -> None:
    def q(after: bool) -> float:
        rows = [
            r["attrs"]["db_queries"]
            for r in data["logs"]
            if r["message"] == "request completed" and ((r["ts"] >= DEPLOY_TS) == after)
        ]
        return _mean(rows)

    assert q(True) >= 3 * q(False)


def test_traces_show_per_product_queries_only_after_deploy(data: Dataset) -> None:
    def product_queries(trace_id: str) -> int:
        return sum(
            1
            for s in data["spans"]
            if s["trace_id"] == trace_id
            and "FROM products WHERE id = ?" in s["attrs"].get("db.statement", "")
        )

    roots = [s for s in data["spans"] if s["parent_id"] is None]
    pre = [product_queries(s["trace_id"]) for s in roots if s["start_ts"] < DEPLOY_TS]
    post = [product_queries(s["trace_id"]) for s in roots if s["start_ts"] >= DEPLOY_TS]
    assert max(pre) == 0
    assert _mean(post) >= 3


def test_red_herring_slow_query_rate_unchanged(data: Dataset) -> None:
    def per_hour(after: bool) -> float:
        n = sum(
            1
            for r in data["logs"]
            if r["message"] == "slow query inventory_audit" and ((r["ts"] >= DEPLOY_TS) == after)
        )
        return n / 12

    before, after = per_hour(False), per_hour(True)
    assert before > 20 and abs(after - before) / before < 0.15


def test_unrelated_error_burst_exists_before_deploy(data: Dataset) -> None:
    burst = [
        r
        for r in data["logs"]
        if r["level"] == "ERROR" and BURST_START <= r["ts"] < BURST_START + timedelta(minutes=5)
    ]
    assert len(burst) >= 10
    assert all("cache backend timeout" in r["message"] for r in burst)
