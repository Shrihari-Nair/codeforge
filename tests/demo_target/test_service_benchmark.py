import re

from demo_target.build_repo import REPO_DIR

from tests.demo_target.conftest import run_benchmark


def test_benchmark_is_deterministic(tree) -> None:  # type: ignore[no-untyped-def]
    d = tree("region")
    assert run_benchmark(d) == run_benchmark(d)


def test_bad_commit_regresses_p95_at_least_2_5x(tree) -> None:  # type: ignore[no-untyped-def]
    good = run_benchmark(tree("region"))
    bad = run_benchmark(tree("n-plus-one"))
    assert bad["p95_ms"] >= 2.5 * good["p95_ms"]
    assert bad["db_queries"] > 3 * good["db_queries"]


def test_head_still_regressed(tree) -> None:  # type: ignore[no-untyped-def]
    good = run_benchmark(tree("region"))
    head = run_benchmark(tree("version"))
    assert head["p95_ms"] >= 2.5 * good["p95_ms"]


def test_correct_fix_restores_baseline(tree, sha) -> None:  # type: ignore[no-untyped-def]
    # Reverting catalog.py to the batched version = the acceptable fix.
    import subprocess

    d = tree("version")
    good_catalog = subprocess.run(
        ["git", "-C", str(REPO_DIR), "show", f"{sha['region']}:checkout/catalog.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    (d / "checkout" / "catalog.py").write_text(good_catalog)
    fixed = run_benchmark(d)
    baseline = run_benchmark(tree("region"))
    assert fixed["p95_ms"] <= baseline["p95_ms"] * 1.05


def test_red_herring_only_fix_does_not_pass_the_p95_gate(tree) -> None:  # type: ignore[no-untyped-def]
    # "Fix" the audit scan (the red herring) and leave the N+1: p95 must stay regressed.
    d = tree("version")
    costs = d / "checkout" / "costs.py"
    costs.write_text(re.sub(r"AUDIT_SCAN_MS = [\d.]+", "AUDIT_SCAN_MS = 0.0", costs.read_text()))
    herring = run_benchmark(d)
    baseline = run_benchmark(tree("region"))
    assert herring["p95_ms"] >= 2.0 * baseline["p95_ms"]
