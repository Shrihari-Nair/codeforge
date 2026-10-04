"""Phase 7 acceptance: kill -9 a real worker process at every node boundary and mid-node.

Each run: worker A is SIGKILLed at a named point (AEOS_CRASH_AT); worker B takes over after the
lease expires and must finish the task from the last checkpoint with NO duplicate side effects.
Run with:  uv run pytest -m chaos tests/chaos
"""

import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from aeos.core.config import get_settings
from aeos.sandbox import SandboxManager
from aeos.store import approvals, tasks

pytestmark = [pytest.mark.chaos, pytest.mark.docker]

NODES = ["plan", "dispatch", "synthesize", "verify", "publish_prepare", "await_decision",
         "publish_pr", "finish"]  # fmt: skip
TOOLS = ["list_deployments:after", "sandbox_write_file:after", "sandbox_compare_to_baseline:before",
         "create_branch:after", "commit_patch:after", "create_pull_request:before",
         "create_pull_request:after"]  # fmt: skip
POINTS = [f"node:{n}:{w}" for n in NODES for w in ("before", "after")] + [
    f"tool:{t}" for t in TOOLS
]

GOAL = "Investigate the checkout latency regression"


@pytest.fixture
def env(seeded_db: None, remote_template: Path) -> dict[str, str]:
    work = Path(tempfile.mkdtemp()) / "checkout-service.git"
    shutil.copytree(remote_template, work)
    tasks.ensure_schema()
    approvals.ensure_schema()
    with psycopg.connect(get_settings().database_url) as conn:  # an isolated queue: a leftover
        conn.execute("DELETE FROM aeos.tasks WHERE status IN ('queued', 'running')")  # task would
    return {**os.environ, "AEOS_GIT_REMOTE_PATH": str(work), "AEOS_LLM_PROVIDER": "fake"}


def spawn(env: dict[str, str], crash_at: str | None = None) -> subprocess.Popen[bytes]:
    e = {**env}
    if crash_at:
        e["AEOS_CRASH_AT"] = crash_at
    return subprocess.Popen(
        [sys.executable, "-m", "aeos.cli", "worker", "--replay", "happy", "--yes", "--lease", "3",
         "--poll", "0.3", "--sweep", "2", "--reap-grace", "3"],
        env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip


def wait_status(task_id: str, wanted: set[str], limit_s: float) -> tasks.TaskRow:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        row = tasks.get(task_id)
        if row and row.status in wanted:
            return row
        time.sleep(0.5)
    raise AssertionError(f"task {task_id} stuck at {tasks.get(task_id)}")


def stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def scalar(sql: str, *params: Any) -> Any:
    with psycopg.connect(get_settings().database_url) as conn:
        row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def assert_exactly_once(task_id: str, env: dict[str, str]) -> None:
    row = tasks.get(task_id)
    assert row is not None and row.status == "completed", row
    # one PR, one branch, one extra commit on it: no duplicated side effects
    assert scalar("SELECT count(*) FROM vcs.pull_requests WHERE head_branch LIKE %s",
                  f"aeos/{task_id}%") == 1  # fmt: skip
    remote = env["AEOS_GIT_REMOTE_PATH"]
    branches = subprocess.run(["git", "-C", remote, "branch", "--list", f"aeos/{task_id}*"],
                              capture_output=True, text=True, check=True).stdout.split()  # fmt: skip
    assert branches == [f"aeos/{task_id}-v1"]
    main_count = int(subprocess.run(["git", "-C", remote, "rev-list", "--count", "main"],
                                    capture_output=True, text=True, check=True).stdout)  # fmt: skip
    branch_count = int(subprocess.run(["git", "-C", remote, "rev-list", "--count", branches[0]],
                                      capture_output=True, text=True, check=True).stdout)  # fmt: skip
    assert branch_count == main_count + 1
    # one approval request, decided once; audit shows each effect once
    assert len(approvals.list_requests(task_id=task_id)) == 1
    events = [e["event"] for e in approvals.audit_log(task_id)]
    assert events.count("approval_requested") == 1
    assert events.count("approval_approved") == 1
    assert events.count("pr_created") == 1
    # no sandbox left behind for this task (the crashed worker's container was reaped)
    mgr = SandboxManager()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        leftovers = [c.name for c in mgr.client.containers.list(all=True, filters={
            "label": f"aeos.task_id={task_id}"})]  # fmt: skip
        if not leftovers:
            break
        time.sleep(1)
    assert leftovers == []


@pytest.mark.parametrize("point", POINTS)
def test_kill_9_at_every_point_still_completes_exactly_once(
    env: dict[str, str], point: str
) -> None:
    task_id = f"t-{uuid.uuid4().hex[:8]}"
    tasks.ensure_schema()
    approvals.ensure_schema()
    tasks.enqueue(task_id, GOAL)

    a = spawn(env, crash_at=point)
    try:
        code = a.wait(timeout=180)
    finally:
        stop(a)
    assert code == -signal.SIGKILL, f"worker did not die at {point} (exit {code})"
    mid = tasks.get(task_id)
    assert mid is not None and mid.status in (
        "running",
        "queued",
    )  # killed mid-flight, not finished

    b = spawn(env)
    try:
        row = wait_status(task_id, {"completed", "failed", "escalated"}, limit_s=240)
        assert row.status == "completed", row.last_error
        assert row.attempts >= 2  # it really was recovered by a second claim
        assert_exactly_once(task_id, env)  # while B is alive: it reaps the crashed worker's sandbox
    finally:
        stop(b)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_kill_9_loop_eventually_completes_exactly_once(
    env: dict[str, str], seed: int
) -> None:
    rng = random.Random(seed)
    task_id = f"t-{uuid.uuid4().hex[:8]}"
    tasks.ensure_schema()
    approvals.ensure_schema()
    tasks.enqueue(task_id, GOAL)
    for _ in range(3):  # three random assassinations
        p = spawn(env)
        time.sleep(rng.uniform(3, 14))
        p.send_signal(signal.SIGKILL)
        p.wait(timeout=10)
        if (row := tasks.get(task_id)) and row.status == "completed":
            break
    final = spawn(env)
    try:
        row = wait_status(task_id, {"completed", "failed", "escalated"}, limit_s=300)
        assert row.status == "completed", row.last_error
        assert_exactly_once(task_id, env)
    finally:
        stop(final)


def test_crash_while_awaiting_approval_then_restart_then_approve(env: dict[str, str]) -> None:
    """The task pauses for a human (no auto-approve). Kill the worker. Restart. Approve. Done."""
    task_id = f"t-{uuid.uuid4().hex[:8]}"
    tasks.ensure_schema()
    approvals.ensure_schema()
    tasks.enqueue(task_id, GOAL)
    no_yes = [sys.executable, "-m", "aeos.cli", "worker", "--replay", "happy", "--lease", "3",
              "--poll", "0.3", "--sweep", "2", "--reap-grace", "3"]  # fmt: skip
    a = subprocess.Popen(no_yes, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_status(task_id, {"paused"}, limit_s=200)
    finally:
        a.send_signal(signal.SIGKILL)
        a.wait(timeout=10)
    pending = approvals.list_requests(status="pending", task_id=task_id)
    assert len(pending) == 1  # the approval survived the crash

    b = subprocess.Popen(no_yes, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(2)  # B is up; the task is still paused (nothing to do without a human)
        assert tasks.get(task_id).status == "paused"  # type: ignore[union-attr]
        import asyncio

        from aeos.hitl import apply_decision

        asyncio.run(apply_decision(pending[0].id, "approved", "alice", "ship it"))
        row = wait_status(task_id, {"completed", "failed", "escalated"}, limit_s=120)
        assert row.status == "completed"
        assert_exactly_once(task_id, env)
    finally:
        stop(b)
    assert json.dumps(approvals.audit_log(task_id), default=str)  # audit trail readable
