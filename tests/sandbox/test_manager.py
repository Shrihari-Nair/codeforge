import pytest

from aeos.sandbox import SandboxError, SandboxManager

pytestmark = pytest.mark.docker


def test_exec_returns_structured_result(mgr: SandboxManager, sbx: str) -> None:
    r = mgr.exec(sbx, ["python", "hello.py"])
    assert (r.stdout, r.stderr, r.exit_code) == ("hi\n", "", 0)
    assert r.duration_s > 0 and not r.timed_out and not r.truncated


def test_nonzero_exit_and_stderr_are_captured(mgr: SandboxManager, sbx: str) -> None:
    r = mgr.exec(sbx, ["python", "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"])
    assert r.exit_code == 3 and r.stderr == "boom"


def test_policy_is_enforced_by_exec(mgr: SandboxManager, sbx: str) -> None:
    for argv in (["curl", "http://example.com"], ["sh", "-c", "id"], ["git", "-c", "x=y", "log"]):
        with pytest.raises(SandboxError) as ei:
            mgr.exec(sbx, argv)
        assert ei.value.code == "command_denied"


def test_container_is_hardened(mgr: SandboxManager, sbx: str) -> None:
    hc = mgr.client.containers.get(sbx).attrs["HostConfig"]
    assert hc["NetworkMode"] == "none" and hc["ReadonlyRootfs"] is True
    assert hc["Memory"] == hc["MemorySwap"] == 512 * 1024 * 1024
    assert hc["PidsLimit"] == 128 and hc["CapDrop"] == ["ALL"]
    assert "no-new-privileges" in hc["SecurityOpt"]
    assert hc["Binds"] is None  # no host mounts at all


def test_files_roundtrip_and_nested_dirs(mgr: SandboxManager, sbx: str) -> None:
    mgr.write_file(sbx, "a/b/c.txt", "nested\n")
    assert mgr.read_file(sbx, "a/b/c.txt") == "nested\n"
    assert mgr.read_file(sbx, "/workspace/hello.py") == "print('hi')\n"
    assert mgr.exec(sbx, ["cat", "a/b/c.txt"]).stdout == "nested\n"


@pytest.mark.parametrize(
    "path", ["../etc/passwd", "/etc/passwd", "/workspace/../etc/x", "a/../../x", ""]
)
def test_file_api_rejects_paths_outside_workspace(mgr: SandboxManager, sbx: str, path: str) -> None:
    with pytest.raises(SandboxError) as ei:
        mgr.read_file(sbx, path)
    assert ei.value.code == "invalid_path"
    with pytest.raises(SandboxError) as ei:
        mgr.write_file(sbx, path, "x")
    assert ei.value.code == "invalid_path"


def test_read_missing_file_and_directory(mgr: SandboxManager, sbx: str) -> None:
    with pytest.raises(SandboxError) as ei:
        mgr.read_file(sbx, "nope.txt")
    assert ei.value.code == "not_found"
    mgr.write_file(sbx, "d/f.txt", "x")
    with pytest.raises(SandboxError) as ei:
        mgr.read_file(sbx, "d")
    assert ei.value.code == "not_a_file"


def test_snapshot_diff_apply_patch_roundtrip(mgr: SandboxManager) -> None:
    files = {"m.py": "x = 1\n", "n.py": "y = 2\n"}
    with mgr.sandbox(files=files) as a, mgr.sandbox(files=files) as b:
        assert mgr.snapshot_diff(a) == ""  # clean tree => empty diff
        mgr.write_file(a, "m.py", "x = 100\n")
        mgr.write_file(a, "new.txt", "hello\n")
        patch = mgr.snapshot_diff(a)
        assert "x = 100" in patch and "new.txt" in patch
        mgr.apply_patch(b, patch)
        assert mgr.read_file(b, "m.py") == "x = 100\n"
        assert mgr.snapshot_diff(b) == patch


def test_apply_patch_rejects_bad_and_escaping_patches(mgr: SandboxManager, sbx: str) -> None:
    escaping = (
        "diff --git a/../evil b/../evil\nnew file mode 100644\n--- /dev/null\n"
        "+++ b/../evil\n@@ -0,0 +1 @@\n+x\n"
    )
    for patch in ("this is not a patch", escaping):
        with pytest.raises(SandboxError) as ei:
            mgr.apply_patch(sbx, patch)
        assert ei.value.code == "patch_failed"
    with pytest.raises(SandboxError) as ei:
        mgr.apply_patch(sbx, "  ")
    assert ei.value.code == "invalid_patch"
    assert mgr.snapshot_diff(sbx) == ""


def test_sandboxes_are_isolated_from_each_other(mgr: SandboxManager) -> None:
    with mgr.sandbox(files={"only_a.txt": "a"}) as a, mgr.sandbox() as b:
        assert mgr.exec(a, ["ls"]).stdout.split() == ["only_a.txt"]
        assert mgr.exec(b, ["ls"]).stdout.split() == []


def test_destroy_is_idempotent_and_use_after_destroy_fails_clearly(mgr: SandboxManager) -> None:
    sid = mgr.create_sandbox()
    mgr.destroy(sid)
    mgr.destroy(sid)
    with pytest.raises(SandboxError) as ei:
        mgr.exec(sid, ["true"])
    assert ei.value.code == "not_found"


def test_context_manager_cleans_up_even_on_error(mgr: SandboxManager) -> None:
    captured = ""
    with pytest.raises(RuntimeError), mgr.sandbox() as sid:
        captured = sid
        raise RuntimeError("agent crashed")
    assert captured not in mgr.list_sandboxes()
