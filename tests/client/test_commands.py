import io
import json

import httpx
import pytest
from rich.console import Console

from aeos.cli import main
from aeos.client.api import AeosClient
from tests.client.fixtures import APPROVAL, STATS, TASK


def console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, width=120, force_terminal=False, color_system=None), buf


ROW = {"task_id": "t-1da798cf", "goal": "update ws", "status": "completed", "attempts": 1,
       "kind": "change", "created_at": "2026-10-07T10:00:00Z", "last_error": None}  # fmt: skip


def run(argv: list[str], routes: dict[str, object] | None = None, calls: list | None = None):  # type: ignore[no-untyped-def,type-arg]
    table: dict[str, object] = {
        "/meta": {"langfuse_url": "http://lf", "target_repo": "o/r"},
        "/tasks": [ROW], "/tasks/t-1da798cf": TASK, "/tasks/t-1da798cf/stats": STATS,
        "/tasks/t-1da798cf/events": [], "/approvals": [APPROVAL],
        **(routes or {}),
    }  # fmt: skip

    def handler(req: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append((req.method, req.url.path, req.content, dict(req.headers)))
        if req.method == "POST" and req.url.path == "/tasks":
            return httpx.Response(202, json={"task_id": "t-new"})
        if req.method == "POST":
            return httpx.Response(200, json={"status": "resuming"})
        if req.url.path not in table:
            return httpx.Response(404, json={"detail": "no task"})
        return httpx.Response(200, json=table[req.url.path])

    c, buf = console()
    client = AeosClient("http://aeos", transport=httpx.MockTransport(handler))
    code = main(argv, _client=client, _console=c)
    return code, buf.getvalue()


def test_tasks_lists_rows() -> None:
    code, out = run(["tasks"])
    assert code == 0 and "t-1da798cf" in out and "completed" in out


def test_show_full_and_sections_and_json() -> None:
    code, out = run(["show", "t-1da798cf"])
    assert code == 0 and "PLAN" in out and "VERIFICATION" in out and "STATS" in out
    _, diff = run(["show", "t-1da798cf", "--diff"])
    assert "diff --git" in diff and "PLAN" not in diff
    _, js = run(["show", "t-1da798cf", "--json"])
    assert json.loads(js)["task_id"] == "t-1da798cf"
    _, step = run(["show", "t-1da798cf", "--step", "s1"])
    assert "github.search_code" in step


def test_new_posts_kind_and_defaults_to_change_when_a_target_repo_exists() -> None:
    calls: list = []  # type: ignore[type-arg]
    assert run(["new", "bump ws"], calls=calls)[0] == 0
    post = next(c for c in calls if c[0] == "POST")
    assert json.loads(post[2]) == {"goal": "bump ws", "kind": "change"}
    calls.clear()
    run(["new", "bump ws", "--kind", "regression"], calls=calls)
    assert json.loads(next(c for c in calls if c[0] == "POST")[2])["kind"] == "regression"


def test_new_defaults_to_regression_without_a_target_repo() -> None:
    calls: list = []  # type: ignore[type-arg]
    run(["new", "x"], {"/meta": {"target_repo": None}}, calls)
    assert json.loads(next(c for c in calls if c[0] == "POST")[2])["kind"] == "regression"


def test_new_yes_requires_an_explicit_actor() -> None:
    code, out = run(["new", "x", "--yes"])
    assert code == 1 and "--actor" in out


def test_approve_posts_with_actor_and_reject_with_feedback() -> None:
    calls: list = []  # type: ignore[type-arg]
    assert (
        run(["approve", "ap-acb1222808", "--actor", "alice", "--api", "http://aeos"], calls=calls)[
            0
        ]
        == 0
    )
    post = next(c for c in calls if c[0] == "POST")
    assert post[1] == "/approvals/ap-acb1222808/approve" and post[3]["x-actor"] == "alice"
    calls.clear()
    run(
        [
            "reject",
            "ap-acb1222808",
            "--actor",
            "bob",
            "--feedback",
            "too big",
            "--api",
            "http://aeos",
        ],
        calls=calls,
    )
    post = next(c for c in calls if c[0] == "POST")
    assert post[1].endswith("/reject") and json.loads(post[2]) == {"feedback": "too big"}


def test_approvals_empty_and_listed() -> None:
    assert "ap-acb1222808" in run(["approvals", "--api", "http://aeos"])[1]
    assert "nothing is waiting" in run(["approvals", "--api", "http://aeos"], {"/approvals": []})[1]


def test_open_prints_trace_and_pr_urls() -> None:
    _, out = run(["open", "t-1da798cf"])
    assert "http://lf/traces/" in out and "https://github.com/o/r/pull/1" in out


def test_stats_and_status_and_events() -> None:
    assert "$0.0327" in run(["stats", "t-1da798cf"])[1]
    assert "t-1da798cf" in run(["status", "t-1da798cf", "--api", "http://aeos"])[1]
    assert run(["events", "t-1da798cf"])[0] == 0


def test_missing_task_is_a_clean_error_and_unreachable_api_exits_2() -> None:
    code, out = run(["show", "t-nope"])
    assert code == 1 and "not found" in out

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    c, buf = console()
    client = AeosClient("http://aeos", transport=httpx.MockTransport(boom))
    assert main(["tasks"], _client=client, _console=c) == 2
    assert "cannot reach the AEOS API" in buf.getvalue() and "Traceback" not in buf.getvalue()


def test_run_accepts_kind_and_legacy_commands_stay_direct_without_an_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aeos import cli

    monkeypatch.delenv("AEOS_API_URL", raising=False)
    seen = []
    monkeypatch.setattr(cli, "_list_approvals", lambda a: seen.append("direct") or 0)
    assert main(["approvals"]) == 0 and seen == ["direct"]
