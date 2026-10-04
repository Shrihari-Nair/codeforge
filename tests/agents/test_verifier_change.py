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
