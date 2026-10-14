import json

import pytest

from aeos.agents.verifier_change import lockfile_issues, run_change_gates, satisfies
from aeos.sandbox import RepoRef, SandboxManager

PKG = {"dependencies": {"ws": "^8.18.0"}}
LOCK = {
    "packages": {"": {"dependencies": {"ws": "^8.18.0"}}, "node_modules/ws": {"version": "8.18.0"}}
}


def test_semver_satisfaction() -> None:
    assert satisfies("8.18.0", "^8.14.2") and satisfies("8.14.2", "^8.14.2")
    assert not satisfies("9.0.0", "^8.14.2") and not satisfies("8.14.1", "^8.14.2")
    assert satisfies("0.3.5", "^0.3.1") and not satisfies("0.4.0", "^0.3.1")
    assert satisfies("1.2.9", "~1.2.3") and not satisfies("1.3.0", "~1.2.3")
    assert satisfies("1.0.0", "1.0.0") and not satisfies("1.0.1", "1.0.0")
    assert satisfies("1.0.0", "latest")  # unknown specs are not guessed at


def test_lockfile_must_mirror_package_json() -> None:
    assert lockfile_issues(json.dumps(PKG), json.dumps(LOCK)) == []
    stale = {"dependencies": {"ws": "^8.19.0"}}
    assert "lockfile root" in lockfile_issues(json.dumps(stale), json.dumps(LOCK))[0]
    drift = json.loads(json.dumps(LOCK))
    drift["packages"]["node_modules/ws"]["version"] = "7.5.0"
    assert "does not satisfy" in lockfile_issues(json.dumps(PKG), json.dumps(drift))[0]
    assert "invalid JSON" in lockfile_issues("{", json.dumps(LOCK))[0]


@pytest.fixture(scope="module")
def mgr() -> SandboxManager:
    try:
        return SandboxManager()
    except Exception:  # noqa: BLE001
        pytest.skip("Docker not available")


def _patch(path: str, old: str, new: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-{old}\n+{new}\n"
    )


pytestmark = pytest.mark.docker


def _remote(tmp_path, files: dict[str, str]):  # type: ignore[no-untyped-def]
    import subprocess

    work = tmp_path / "w"
    work.mkdir()
    for name, text in files.items():
        (work / name).write_text(text)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin", "HOME": str(work)}  # fmt: skip
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "i"]):
        subprocess.run(["git", *cmd], cwd=work, check=True, env=env, capture_output=True)
    bare = tmp_path / "r.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    return bare


async def _gates(mgr: SandboxManager, tmp_path, files: dict[str, str], patch: str):  # type: ignore[no-untyped-def]
    bare = _remote(tmp_path, files)
    return await run_change_gates(mgr, base=RepoRef(remote=str(bare), ref="main"), patch=patch)


async def test_syntax_error_fails_the_gate(mgr, tmp_path) -> None:  # type: ignore[no-untyped-def]
    r = await _gates(
        mgr, tmp_path, {"a.js": "const a = 1;\n"}, _patch("a.js", "const a = 1;", "const (")
    )
    assert r.status == "failed" and r.gates["syntax_ok"] is False
    assert r.issues[0].type == "syntax_error" and "a.js" in r.issues[0].detail


async def test_valid_js_and_json_pass(mgr, tmp_path) -> None:  # type: ignore[no-untyped-def]
    r = await _gates(
        mgr, tmp_path, {"a.js": "const a = 1;\n"}, _patch("a.js", "const a = 1;", "const a = 2;")
    )
    assert r.status == "passed" and r.gates["syntax_ok"] is True
    assert r.metrics["tests"]["ran"] is False  # honest: nothing claims tests passed


async def test_package_json_change_without_lockfile_fails(mgr, tmp_path) -> None:  # type: ignore[no-untyped-def]
    files = {"package.json": json.dumps(PKG) + "\n", "package-lock.json": json.dumps(LOCK) + "\n"}
    patch = _patch("package.json", json.dumps(PKG), json.dumps({"dependencies": {"ws": "^8.19.0"}}))
    r = await _gates(mgr, tmp_path, files, patch)
    assert r.status == "failed" and r.gates["lockfile_consistent"] is False
    assert any(i.type == "lockfile_inconsistent" for i in r.issues)


async def test_protected_locations_are_rejected(mgr, tmp_path) -> None:  # type: ignore[no-untyped-def]
    r = await _gates(mgr, tmp_path, {".env": "A=1\n"}, _patch(".env", "A=1", "A=2"))
    assert r.gates["paths_allowed"] is False


# ---------------------------------------------------------------- the repo's own tests
NODE_PKG = json.dumps({"name": "x", "scripts": {"test": "node --test"}}) + "\n"
ADD_OK = "exports.add = (a, b) => a + b;\n"
ADD_TEST = (
    "const test = require('node:test');\nconst assert = require('node:assert');\n"
    "const { add } = require('./add');\ntest('adds', () => { assert.strictEqual(add(1, 2), 3); });\n"
)


async def test_node_suite_passes_and_is_reported(mgr, tmp_path) -> None:  # type: ignore[no-untyped-def]
    files = {"package.json": NODE_PKG, "add.js": ADD_OK, "add.test.js": ADD_TEST}
    r = await _gates(
        mgr, tmp_path, files, _patch("add.js", ADD_OK.strip(), "exports.add = (a, b) => b + a;")
    )
    assert r.status == "passed" and r.gates["tests_pass"] is True
    assert r.metrics["tests"]["ran"] is True and r.metrics["tests"]["runner"] == "node --test"
    assert r.metrics["tests"]["total"] == 1


async def test_a_patch_that_breaks_the_suite_is_caught(mgr, tmp_path) -> None:  # type: ignore[no-untyped-def]
    files = {"package.json": NODE_PKG, "add.js": ADD_OK, "add.test.js": ADD_TEST}
    r = await _gates(
        mgr, tmp_path, files, _patch("add.js", ADD_OK.strip(), "exports.add = (a, b) => a - b;")
    )
    assert r.status == "failed" and r.gates["tests_pass"] is False
    assert r.issues[0].type == "test_regression" and "none before" in r.issues[0].detail


async def test_a_suite_that_already_fails_is_not_blamed_on_the_patch(mgr, tmp_path) -> None:  # type: ignore[no-untyped-def]
    broken = "exports.add = (a, b) => 0;\n"  # the test fails before the change too
    files = {"package.json": NODE_PKG, "add.js": broken, "add.test.js": ADD_TEST}
    r = await _gates(
        mgr, tmp_path, files, _patch("add.js", broken.strip(), "exports.add = (a, b) => 1;")
    )
    assert "tests_pass" not in r.gates and r.status == "passed"
    assert "already fails" in r.metrics["tests"]["note"]


async def test_a_suite_that_needs_npm_install_is_reported_as_not_run(mgr, tmp_path) -> None:  # type: ignore[no-untyped-def]
    pkg = json.dumps({"scripts": {"test": "jest"}, "devDependencies": {"jest": "^29.0.0"}}) + "\n"
    r = await _gates(
        mgr,
        tmp_path,
        {"package.json": pkg, "a.js": "const a = 1;\n"},
        _patch("a.js", "const a = 1;", "const a = 2;"),
    )
    assert r.metrics["tests"]["ran"] is False and "needs npm install" in r.metrics["tests"]["note"]
    assert "tests_pass" not in r.gates  # never claims a pass it did not observe


def _calc_patch(new_body: str) -> str:
    return (
        "diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n"
        f" def add(a, b):\n-    return a + b\n+    {new_body}\n"
    )


async def test_python_repo_runs_pytest(mgr, tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "ok").mkdir()
    (tmp_path / "bad").mkdir()
    files = {
        "calc.py": "def add(a, b):\n    return a + b\n",
        "test_calc.py": "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    }
    ok = await _gates(mgr, tmp_path / "ok", files, _calc_patch("return b + a"))
    assert ok.metrics["tests"].get("runner") == "pytest", (ok.metrics["tests"], ok.issues)
    assert ok.gates["tests_pass"] is True
    bad = await _gates(mgr, tmp_path / "bad", files, _calc_patch("return a - b"))
    assert bad.status == "failed" and bad.issues[0].type == "test_regression"
