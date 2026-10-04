"""Verifier gates end to end in real sandboxes on the demo repo."""

import json
from pathlib import Path

import pytest

from aeos.agents.verifier import run_gates
from aeos.mcp_servers.github.backend import GitBackend
from aeos.sandbox import RepoRef, SandboxManager

pytestmark = pytest.mark.docker

GT = json.loads((Path(__file__).parents[2] / "demo_target" / "GROUND_TRUTH.json").read_text())
BAD, GOOD = GT["root_cause"]["commit_sha"], GT["last_good_commit_sha"]


@pytest.fixture(scope="module")
def mgr() -> SandboxManager:
    import docker

    try:
        docker.from_env().ping()
    except Exception:  # noqa: BLE001
        pytest.skip("Docker not available")
    return SandboxManager()


@pytest.fixture(scope="module")
def remote(remote_template: Path) -> str:
    return str(remote_template)


def make_patch(mgr: SandboxManager, remote: str, edits: dict[str, tuple[str, str]]) -> str:
    with mgr.sandbox(RepoRef(remote=remote, ref="main")) as sid:
        for path, (old, new) in edits.items():
            mgr.write_file(sid, path, mgr.read_file(sid, path).replace(old, new))
        return mgr.snapshot_diff(sid)


async def gate(mgr: SandboxManager, remote: str, patch: str | None):  # type: ignore[no-untyped-def]
    return await run_gates(
        mgr,
        base=RepoRef(remote=remote, ref="main"),
        baseline=RepoRef(remote=remote, ref=GOOD),
        patch=patch,
    )


async def test_real_fix_passes_every_gate(mgr: SandboxManager, remote: str) -> None:
    fix = GitBackend(Path(remote)).get_diff(BAD, GOOD, "checkout/catalog.py", 100_000)[3]
    res = await gate(mgr, remote, fix)
    assert res.status == "passed" and all(res.gates.values()), res.issues
    assert set(res.gates) >= {"patch_present", "patch_applies", "tests_pass", "lint_ok",
                              "benchmark_not_regressed", "diff_size_ok", "paths_allowed",
                              "no_secrets_in_diff"}  # fmt: skip
    assert res.metrics["benchmark"]["p95_change_pct"] <= 10  # type: ignore[index]


async def test_red_herring_fix_fails_on_performance_with_focus(
    mgr: SandboxManager, remote: str
) -> None:
    patch = make_patch(
        mgr, remote, {"checkout/audit.py": ("AUDIT_EVERY = 25", "AUDIT_EVERY = 1000")}
    )
    res = await gate(mgr, remote, patch)
    assert res.status == "failed"
    assert res.gates["tests_pass"] and res.gates["paths_allowed"]  # looks fine on every other axis
    assert not res.gates["benchmark_not_regressed"]
    assert [i.type for i in res.issues] == ["perf_regression"]
    assert "p95" in res.issues[0].detail and "%" in res.issues[0].detail
    assert any("unrelated" in f or "queries" in f for f in res.suggested_focus)


async def test_cheating_by_editing_the_cost_model_is_caught(
    mgr: SandboxManager, remote: str
) -> None:
    patch = make_patch(
        mgr, remote, {"checkout/costs.py": ("DB_QUERY_BASE_MS = 2.0", "DB_QUERY_BASE_MS = 0.0")}
    )
    res = await gate(mgr, remote, patch)
    assert res.status == "failed" and not res.gates["paths_allowed"]
    assert any(i.type == "forbidden_edit" for i in res.issues)


async def test_breaking_a_test_is_reported_by_name(mgr: SandboxManager, remote: str) -> None:
    patch = make_patch(mgr, remote, {"checkout/pricing.py": ('"US": 0.07', '"US": 0.99')})
    res = await gate(mgr, remote, patch)
    assert not res.gates["tests_pass"]
    failure = next(i for i in res.issues if i.type == "test_failure")
    assert "test_tax_rates_by_region" in failure.detail


async def test_syntax_error_fails_lint_and_tests(mgr: SandboxManager, remote: str) -> None:
    patch = make_patch(mgr, remote, {"checkout/pricing.py": ("def tax_rate", "def tax_rate(:")})
    res = await gate(mgr, remote, patch)
    assert res.status == "failed" and not res.gates["lint_ok"] and not res.gates["tests_pass"]


async def test_patch_that_does_not_apply_is_a_clean_failure(
    mgr: SandboxManager, remote: str
) -> None:
    bogus = ("diff --git a/checkout/nope.py b/checkout/nope.py\n--- a/checkout/nope.py\n"
             "+++ b/checkout/nope.py\n@@ -1 +1 @@\n-x\n+y\n")  # fmt: skip
    res = await gate(mgr, remote, bogus)
    assert res.status == "failed" and res.gates["patch_applies"] is False
    assert "benchmark_not_regressed" not in res.gates


async def test_empty_patch_fails_without_touching_docker(mgr: SandboxManager, remote: str) -> None:
    for patch in (None, "", "   \n"):
        res = await gate(mgr, remote, patch)
        assert res.status == "failed" and res.issues[0].type == "no_patch"


async def test_a_baseline_that_is_not_faster_than_the_head_cannot_verify_anything(
    mgr: SandboxManager, remote: str
) -> None:
    """Found by the live eval: with a wrong 'last good' commit the benchmark gate could pass
    trivially. The regression must be reproducible against the baseline."""
    fix = GitBackend(Path(remote)).get_diff(BAD, GOOD, "checkout/catalog.py", 100_000)[3]
    res = await run_gates(
        mgr, base=RepoRef(remote=remote, ref="main"), baseline=RepoRef(remote=remote, ref="main"),
        patch=fix,
    )  # fmt: skip
    assert res.status == "failed" and res.gates["regression_reproduced"] is False
    assert any(i.type == "baseline_not_faster" for i in res.issues)
    assert any("known-good" in f for f in res.suggested_focus)
    # ...while the same patch against the REAL baseline passes (and reproduces the regression)
    ok = await gate(mgr, remote, fix)
    assert ok.status == "passed" and ok.gates["regression_reproduced"] is True
