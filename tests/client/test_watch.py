import io
import json

import httpx
import pytest
from rich.console import Console

from aeos.client.api import AeosClient
from aeos.client.prompts import ask_approval
from aeos.client.watch import watch_task
from tests.client.fixtures import APPROVAL, STATS, TASK


def console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, width=100, force_terminal=False, color_system=None), buf


def sse(*events: dict) -> bytes:  # type: ignore[type-arg]
    return b"".join(
        f"id: {e['id']}\nevent: aeos\ndata: {json.dumps(e)}\n\n".encode() for e in events
    )


def ev(i: int, type_: str, msg: str = "", **data: object) -> dict:  # type: ignore[type-arg]
    return {"id": i, "task_id": "t-1da798cf", "ts": "2026-10-07T10:00:00Z", "type": type_,
            "agent": "supervisor", "message": msg or type_, "data": data}  # fmt: skip


def api(handler) -> AeosClient:  # type: ignore[no-untyped-def]
    return AeosClient("http://aeos", transport=httpx.MockTransport(handler))


def server(task: dict, live: list[dict], history: list[dict] | None = None, approvals=None):  # type: ignore[no-untyped-def,type-arg]
    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p.endswith("/events/stream"):
            return httpx.Response(200, content=sse(*live))
        if p.endswith("/events"):
            return httpx.Response(200, json=history or [])
        if p.endswith("/stats"):
            return httpx.Response(200, json=STATS)
        if p == "/approvals":
            return httpx.Response(200, json=approvals or [])
        return httpx.Response(200, json=task)

    return handler


def test_watch_streams_events_and_returns_final_status() -> None:
    live = [ev(1, "plan_created", "plan v1"), ev(2, "task_completed", "task completed")]
    state = {"task": {**TASK, "status": "running"}}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/stream"):
            state["task"] = TASK  # the run finishes while we follow it
        return server(state["task"], live)(req)

    c, buf = console()
    status = watch_task(api(handler), c, "t-1da798cf", actor="alice", interactive=False)
    assert status == "completed"
    assert "plan_created" in buf.getvalue() and "task_completed" in buf.getvalue()


def test_finished_task_shows_history_without_streaming() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert not req.url.path.endswith("/stream")
        return server(TASK, [], [ev(1, "plan_created")])(req)

    c, buf = console()
    assert watch_task(api(handler), c, "t-1da798cf", actor="a", interactive=False) == "completed"
    assert "plan_created" in buf.getvalue()


def test_non_interactive_watch_stops_at_an_approval_with_instructions() -> None:
    waiting = {**TASK, "status": "awaiting_approval", "pending_approval_id": "ap-1"}
    live = [ev(1, "approval_requested", "approval ap-1 needed", approval_id="ap-1")]
    c, buf = console()
    handler = server(waiting, live, approvals=[APPROVAL])
    status = watch_task(api(handler), c, "t-1da798cf", actor="alice", interactive=False)
    assert status == "awaiting_approval" and "aeos approve ap-acb1222808" in buf.getvalue()


def test_interactive_watch_asks_then_keeps_following(monkeypatch: pytest.MonkeyPatch) -> None:
    waiting = {**TASK, "status": "awaiting_approval"}
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            seen.append(req.url.path)
            return httpx.Response(200, json={"status": "resuming"})
        live = [ev(1, "approval_requested"), ev(2, "task_completed")]
        return server(waiting, live, approvals=[APPROVAL])(req)

    c, _ = console()
    monkeypatch.setattr(c, "input", lambda *a, **k: "a")
    watch_task(api(handler), c, "t-1da798cf", actor="alice", interactive=True)
    assert seen == ["/approvals/ap-acb1222808/approve"]


@pytest.mark.parametrize("answer,decision", [("a", "approve"), ("r", "reject")])
def test_prompt_sends_the_decision_with_actor(
    answer: str, decision: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}  # type: ignore[type-arg]

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(path=req.url.path, actor=req.headers["x-actor"], body=json.loads(req.content))
        return httpx.Response(200, json={"status": "resuming"})

    c, _ = console()
    answers = iter([answer, "needs a smaller diff"] if answer == "r" else [answer])
    monkeypatch.setattr(c, "input", lambda *a, **k: next(answers))
    result = ask_approval(api(handler), c, APPROVAL, actor="alice")
    assert result in ("approved", "rejected") and seen["path"].endswith(decision)
    assert seen["actor"] == "alice"
    if answer == "r":
        assert seen["body"]["feedback"] == "needs a smaller diff"


@pytest.mark.parametrize("status,word", [(409, "already decided"), (410, "expired")])
def test_prompt_handles_decisions_made_elsewhere_or_expired(
    status: int, word: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, buf = console()
    monkeypatch.setattr(c, "input", lambda *a, **k: "a")
    client = api(lambda r: httpx.Response(status, json={"detail": word}))
    assert ask_approval(client, c, APPROVAL, actor="alice") == "stale"
    assert word in buf.getvalue()


def test_prompt_can_show_the_diff_then_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = console()
    answers = iter(["d", "s"])
    monkeypatch.setattr(c, "input", lambda *a, **k: next(answers))
    assert ask_approval(api(lambda r: httpx.Response(500)), c, APPROVAL, actor="alice") == "skipped"
    assert "diff --git" in buf.getvalue()


def test_end_of_input_skips_instead_of_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = console()

    def eof(*a: object, **k: object) -> str:
        raise EOFError

    monkeypatch.setattr(c, "input", eof)
    assert ask_approval(api(lambda r: httpx.Response(500)), c, APPROVAL, actor="a") == "skipped"


def test_ctrl_c_detaches_without_cancelling() -> None:
    running = {**TASK, "status": "running"}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/stream"):
            raise KeyboardInterrupt
        return server(running, [])(req)

    c, buf = console()
    assert watch_task(api(handler), c, "t-1da798cf", actor="a", interactive=True) == "running"
    assert "detached" in buf.getvalue()
