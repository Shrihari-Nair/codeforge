import io
import json

import httpx
import pytest
from rich.console import Console

from aeos.client.api import AeosClient
from aeos.client.chat import run_chat
from tests.client.fixtures import APPROVAL, STATS, TASK


def sse(*events: dict) -> bytes:  # type: ignore[type-arg]
    return b"".join(
        f"id: {e['id']}\nevent: aeos\ndata: {json.dumps(e)}\n\n".encode() for e in events
    )


def session(lines: list[str], routes: dict | None = None, calls: list | None = None, handler=None):  # type: ignore[no-untyped-def,type-arg]
    table = {"/meta": {"langfuse_url": "http://lf", "target_repo": "o/r"}, "/tasks": [],
             "/tasks/t-new": {**TASK, "task_id": "t-new"}, "/tasks/t-new/events": [],
             "/tasks/t-new/stats": STATS, "/approvals": [APPROVAL], **(routes or {})}  # fmt: skip

    def default(req: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append((req.method, req.url.path, req.content, dict(req.headers)))
        if req.method == "POST" and req.url.path == "/tasks":
            return httpx.Response(202, json={"task_id": "t-new"})
        if req.method == "POST":
            return httpx.Response(200, json={"status": "resuming"})
        return httpx.Response(200, json=table[req.url.path])

    buf = io.StringIO()
    c = Console(file=buf, width=120, force_terminal=False, color_system=None)
    it = iter(lines)

    def read(*a: object, **k: object) -> str:
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None  # end of input, like Ctrl+D

    c.input = read  # type: ignore[method-assign,assignment]
    client = AeosClient("http://aeos", transport=httpx.MockTransport(handler or default))
    return run_chat(client, c, actor="alice"), buf.getvalue()


def test_banner_help_and_quit() -> None:
    code, out = session(["/help", "/quit"])
    assert code == 0 and "o/r" in out and "/approve" in out and "1 approval(s) waiting" in out


def test_plain_text_creates_a_task_with_the_current_kind_and_watches_it() -> None:
    calls: list = []  # type: ignore[type-arg]
    code, out = session(["update ws", "/target regression", "another", "/quit"], calls=calls)
    posts = [json.loads(c[2]) for c in calls if c[0] == "POST" and c[1] == "/tasks"]
    assert posts == [
        {"goal": "update ws", "kind": "change"},
        {"goal": "another", "kind": "regression"},
    ]
    assert code == 0 and "t-new" in out


def test_end_of_input_exits_cleanly_and_blank_lines_are_ignored() -> None:
    assert session(["", "   "])[0] == 0


def test_unknown_command_does_not_crash() -> None:
    assert "unknown or incomplete command" in session(["/nope", "/approve", "/quit"])[1]


def test_show_uses_the_last_task_and_sections() -> None:
    _, out = session(["x", "/show diff", "/quit"])
    assert "diff --git" in out


def test_show_without_any_task_asks_for_an_id() -> None:
    assert "give a task id" in session(["/show", "/quit"])[1]


def test_approve_uses_the_session_actor_and_actor_can_change() -> None:
    calls: list = []  # type: ignore[type-arg]
    session(
        ["/approve ap-acb1222808", "/actor bob", "/reject ap-acb1222808 too big", "/quit"],
        calls=calls,
    )
    posts = [c for c in calls if c[0] == "POST"]
    assert posts[0][3]["x-actor"] == "alice" and posts[1][3]["x-actor"] == "bob"
    assert json.loads(posts[1][2]) == {"feedback": "too big"}


def test_api_errors_mid_session_are_reported_and_the_loop_continues() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/meta":
            return httpx.Response(200, json={"target_repo": "o/r"})
        if req.url.path == "/approvals":
            return httpx.Response(200, json=[])
        raise httpx.ConnectError("down")

    code, out = session(["/tasks", "hello", "/quit"], handler=handler)
    assert code == 0 and out.count("cannot reach the AEOS API") == 2


def test_unreachable_api_at_start_exits_2() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert session(["/quit"], handler=handler)[0] == 2


@pytest.mark.parametrize(
    "cmd", ["/tasks", "/approvals", "/stats", "/events", "/open", "/diff", "/watch"]
)
def test_every_command_runs(cmd: str) -> None:
    routes = {"/tasks/t-new/events": [], "/tasks/t-new": {**TASK, "task_id": "t-new"}}
    code, out = session(["x", cmd, "/quit"], routes)
    assert code == 0 and "unknown" not in out
