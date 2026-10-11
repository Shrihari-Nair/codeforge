"""The terminal against a REAL server (HTTP + SSE), real MCP servers, sandbox and verifier, with
the scripted `happy` LLM: type a request in `aeos chat`, watch, approve at the prompt, get the PR.

Run it with the dashboard stack's API container stopped: both would poll the same task queue."""

import asyncio
import io
import json
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import httpx
import pytest
import uvicorn
from rich.console import Console

from aeos.api.app import create_app
from aeos.cli import main
from aeos.core.config import get_settings
from aeos.demo.trajectories import happy_path_llm
from aeos.graph.checkpoint import open_checkpointer
from aeos.runtime import open_runtime
from aeos.store.events import DbSink

pytestmark = pytest.mark.docker
TRUTH = json.loads((Path(__file__).parents[2] / "demo_target" / "GROUND_TRUTH.json").read_text())


async def get(url: str) -> dict:  # type: ignore[type-arg]
    return await asyncio.to_thread(lambda: httpx.get(url).json())  # type: ignore[no-any-return]


def terminal(lines: list[str]) -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    c = Console(file=buf, width=140, force_terminal=False, color_system=None)
    it = iter(lines)

    def read(*a: object, **k: object) -> str:
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    c.input = read  # type: ignore[method-assign,assignment]
    return c, buf


async def test_chat_request_watch_approve_and_pr(seeded_db: None, remote_template: Path) -> None:
    work = Path(tempfile.mkdtemp()) / "checkout-service.git"
    shutil.copytree(remote_template, work)
    s = get_settings().model_copy(update={"git_remote_path": str(work), "target_enabled": False})
    app = create_app(
        settings=s,
        runtime_factory=lambda: open_runtime(s, llm=happy_path_llm(TRUTH), sink=DbSink()),
        checkpointer_factory=lambda: open_checkpointer(s),
    )
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.1)
    try:
        # a request typed into chat, approved at the inline prompt, then /quit
        c, buf = terminal(["Investigate the checkout latency regression", "a", "/quit"])
        code = await asyncio.to_thread(
            main, ["chat", "--api", base, "--actor", "alice"], _console=c
        )
        out = buf.getvalue()
        assert code == 0, out
        for needle in (
            "plan_created",
            "verification",
            "approval_requested",
            "approve",
            "pr_created",
            "local://",
        ):
            assert needle in out, needle
        assert "approved as alice" in out

        tasks = await get(f"{base}/tasks")
        task_id = tasks[0]["task_id"]
        c2, buf2 = terminal([])
        assert (
            await asyncio.to_thread(main, ["show", task_id, "--json", "--api", base], _console=c2)
            == 0
        )
        assert json.loads(buf2.getvalue()) == await get(f"{base}/tasks/{task_id}")
        assert (await get(f"{base}/tasks/{task_id}"))["status"] == "completed"

        c3, buf3 = terminal([])
        await asyncio.to_thread(main, ["approvals", "--api", base], _console=c3)
        assert task_id not in buf3.getvalue()  # nothing left pending
    finally:
        server.should_exit = True

    c4, buf4 = terminal([])  # the server is gone: a clear message, not a traceback
    await asyncio.sleep(1)
    assert await asyncio.to_thread(main, ["tasks", "--api", base], _console=c4) == 2
    assert "cannot reach the AEOS API" in buf4.getvalue()
