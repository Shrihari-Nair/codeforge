import pytest

from aeos.sandbox import SandboxManager

pytestmark = pytest.mark.docker


def test_orphan_is_reaped_after_simulated_crash(mgr: SandboxManager) -> None:
    sid = mgr.create_sandbox(task_id="crashed-task")
    # The owning process "crashes": a brand-new manager has no memory of the sandbox.
    fresh = SandboxManager()
    others = set(fresh.list_sandboxes()) - {sid}
    removed = fresh.reap(keep=others)
    assert removed == [sid]
    assert sid not in fresh.list_sandboxes()


def test_reap_respects_keep_and_ttl(mgr: SandboxManager) -> None:
    with mgr.sandbox() as sid:
        assert mgr.reap(ttl_s=3600, keep={sid}) == []  # young and kept
        others = set(mgr.list_sandboxes()) - {sid}
        assert mgr.reap(ttl_s=0, keep=others | {sid}) == [sid] or sid not in mgr.list_sandboxes()
