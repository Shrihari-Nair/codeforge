"""Regressions found by the final review of the terminal client."""

import io
import json

import httpx
import pytest
from rich.console import Console

from aeos.cli import main
from aeos.client import render
from aeos.client.api import AeosClient, ApiUnreachable
from aeos.client.chat import run_chat
from aeos.client.prompts import ask_approval
from aeos.client.watch import watch_task
from tests.client.fixtures import APPROVAL, STATS, TASK


def term() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, width=120, force_terminal=False, color_system=None), buf


def api(handler) -> AeosClient:  # type: ignore[no-untyped-def]
    return AeosClient("http://aeos", transport=httpx.MockTransport(handler))


def test_markup_in_an_api_error_detail_cannot_crash_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, buf = term()
    monkeypatch.setattr(c, "input", lambda *a, **k: "a")
    client = api(
        lambda r: httpx.Response(409, json={"detail": "already approved by [/x][link=http://e]y"})
    )
    assert ask_approval(client, c, APPROVAL, actor="a") == "stale"
    assert "[/x][link=http://e]y" in buf.getvalue()


def test_watch_does_not_hang_when_the_task_finishes_between_the_two_reads() -> None:
    seen = {"events": False}

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p.endswith("/stream"):
            pytest.fail("must not wait for a stream: the task already finished")
        if p.endswith("/events"):
            seen["events"] = True
            e = {
                "id": 1,
                "ts": "2026-10-07T10:00:00Z",
                "type": "task_completed",
                "agent": "s",
                "message": "done",
            }
            return httpx.Response(200, json=[e])
        if p.endswith("/stats"):
            return httpx.Response(200, json=STATS)
        # finished only after the history was read: reading the task BEFORE the history would miss it
        return httpx.Response(200, json=TASK if seen["events"] else {**TASK, "status": "running"})

    c, _ = term()
    assert watch_task(api(handler), c, "t-1da798cf", actor="a", interactive=False) == "completed"


def test_approval_panel_title_keeps_the_status_brackets() -> None:
    c, buf = term()
    c.print(render.approval_panel(APPROVAL))
    assert "[pending]" in buf.getvalue()


def test_ctrl_c_during_a_command_exits_130_without_a_traceback() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt

    c, _ = term()
    assert main(["tasks"], _client=api(handler), _console=c) == 130


def test_a_non_json_answer_means_the_wrong_service() -> None:
    client = api(lambda r: httpx.Response(200, text="<html>dashboard</html>"))
    with pytest.raises(ApiUnreachable) as ei:
        client.meta()
    assert "AEOS API" in str(ei.value)


def test_approvals_json_is_always_json() -> None:
    c, buf = term()
    client = api(lambda r: httpx.Response(200, json=[]))
    assert main(["approvals", "--json", "--api", "http://aeos"], _client=client, _console=c) == 0
    assert json.loads(buf.getvalue()) == []


def chat(lines: list[str], handler=None):  # type: ignore[no-untyped-def]
    c, buf = term()
    it = iter(lines)

    def read(*a: object, **k: object) -> str:
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    c.input = read  # type: ignore[method-assign,assignment]
    h = handler or (
        lambda r: httpx.Response(200, json={"target_repo": "o/r"} if r.url.path == "/meta" else [])
    )
    return run_chat(api(h), c, actor="alice"), buf.getvalue()


def test_chat_survives_a_bare_slash_and_markup_in_arguments() -> None:
    code, out = chat(["/", "/   ", "/actor [/x]", "/[/x]", "/quit"])
    assert code == 0 and "[/x]" in out


def test_chat_survives_ctrl_c_inside_a_command() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/meta":
            return httpx.Response(200, json={"target_repo": None})
        if req.url.path == "/approvals":
            return httpx.Response(200, json=[])
        raise KeyboardInterrupt

    code, out = chat(["/tasks", "/quit"], handler)
    assert code == 0 and "interrupted" in out


def test_show_prints_decided_approvals() -> None:
    decided = {**APPROVAL, "status": "approved", "actor": "alice"}
    c, buf = term()
    c.print(render.decided_panel([decided, APPROVAL]))
    assert "approved by alice" in buf.getvalue() and "pending" not in buf.getvalue()
