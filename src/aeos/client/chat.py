"""`aeos chat`: type a request, watch it work, approve inline. Slash commands do the rest."""

import argparse
from typing import Any

from rich.console import Console

from aeos.client import commands as C
from aeos.client.api import AeosClient, ApiError, ApiUnreachable
from aeos.client.sanitize import clean, safe, say
from aeos.client.watch import watch_task

HELP = """type a request in plain English to start a task, or use:
  /tasks                      list tasks
  /show [id] [plan|findings|root-cause|verify|diff|stats|pr]
  /watch [id]                 follow a task live (Ctrl+C detaches)
  /approvals                  pending approvals
  /approve <approval-id>      /reject <approval-id> [reason]
  /stats [id]   /events [id]  /diff [id]   /open [id]
  /target change|regression   what the next request runs on
  /actor <name>               who approvals are recorded as
  /help   /quit"""
SECTIONS = {"plan", "findings", "root-cause", "verify", "diff", "stats", "pr", "feedback"}


def _ns(**kw: Any) -> argparse.Namespace:
    base = dict(json=False, step=None, version=None, follow=False, after=0, all=False, watch=False,
                status=None, yes=False, feedback=None, actor=None)  # fmt: skip
    base.update({s.replace("-", "_"): False for s in SECTIONS})
    return argparse.Namespace(**{**base, **kw})


def run_chat(client: AeosClient, console: Console, *, actor: str, kind: str | None = None) -> int:
    try:
        meta = client.meta()
    except (ApiError, ApiUnreachable) as exc:
        console.print(safe(str(exc), "red"))
        return 2
    repo = meta.get("target_repo")
    state: dict[str, Any] = {
        "kind": kind or ("change" if repo else "regression"),
        "actor": actor,
        "last": None,
    }
    say(console, f"AEOS chat · api {client.base_url} · repo {repo or 'none (demo only)'} · "
                 f"target {state['kind']} · you are {actor} · /help for commands")  # fmt: skip
    pending = client.approvals("pending")
    if pending:
        say(console, f"{len(pending)} approval(s) waiting: /approvals")

    def tid(parts: list[str]) -> str | None:
        t = parts[0] if parts and parts[0].startswith("t-") else state["last"]
        if t is None:
            console.print("no task yet: give a task id (see /tasks)")
        return t

    def command(line: str) -> bool:
        words = line[1:].split()
        if not words:
            say(console, "type a command after the slash (try /help)")
            return True
        cmd, *parts = words
        a = state["actor"]
        if cmd in ("quit", "exit", "q"):
            return False
        if cmd == "help":
            console.print(HELP)
        elif cmd == "tasks":
            C.cmd_tasks(_ns(cmd="tasks"), client, console)
        elif cmd == "approvals":
            C.cmd_approvals(_ns(cmd="approvals"), client, console)
        elif cmd in ("approve", "reject") and parts:
            C.cmd_approve(
                _ns(cmd=cmd, approval_id=parts[0], actor=a, feedback=" ".join(parts[1:]) or None),
                client,
                console,
            )
        elif cmd == "target" and parts[:1] in (["change"], ["regression"]):
            state["kind"] = parts[0]
            say(console, f"next requests run as: {parts[0]}")
        elif cmd == "actor" and parts:
            state["actor"] = clean(parts[0])
            say(console, f"approvals are recorded as {state['actor']}")
        elif cmd in ("show", "watch", "stats", "events", "open", "diff"):
            t = tid(parts)
            if t is None:
                return True
            state["last"] = t
            rest = [p for p in parts if p in SECTIONS]
            if cmd == "watch":
                watch_task(
                    client, console, t, actor=a, interactive=True, langfuse_url=C.langfuse(client)
                )
            elif cmd == "diff":
                C.cmd_show(_ns(cmd="show", task_id=t, diff=True), client, console)
            elif cmd == "show":
                flags = {s.replace("-", "_"): True for s in rest}
                C.cmd_show(_ns(cmd="show", task_id=t, **flags), client, console)
            else:
                C.HANDLERS[cmd](_ns(cmd=cmd, task_id=t), client, console)
        else:
            say(console, f"unknown or incomplete command: /{clean(cmd)} (try /help)")
        return True

    while True:
        try:
            line = console.input("aeos> ", markup=False).strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0
        if not line:
            continue
        try:
            if line.startswith("/"):
                if not command(line):
                    return 0
            else:
                state["last"] = client.create_task(line, state["kind"])
                say(console, f"{state['last']}  ({state['kind']})")
                watch_task(client, console, state["last"], actor=state["actor"], interactive=True,
                           langfuse_url=C.langfuse(client))  # fmt: skip
        except (ApiError, ApiUnreachable) as exc:
            console.print(safe(f"error: {exc}", "red"))
        except KeyboardInterrupt:
            say(console, "\ninterrupted")
