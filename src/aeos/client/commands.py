"""One-shot terminal commands: the CLI face of every dashboard page."""

import argparse
import sys
import time
from typing import Any

from rich.console import Console
from rich.live import Live

from aeos.client import render
from aeos.client.api import AeosClient, ApiError, ApiUnreachable, default_actor
from aeos.client.prompts import show_diff
from aeos.client.sanitize import clean, safe, say
from aeos.client.watch import END_EVENTS, watch_task

EXIT = {"completed": 0, "failed": 1, "escalated": 1, "cancelled": 1, "awaiting_approval": 3}


def interactive(console: Console) -> bool:
    return console.is_terminal and sys.stdin.isatty()


def actor_of(args: argparse.Namespace) -> str:
    return getattr(args, "actor", None) or default_actor()


def langfuse(client: AeosClient) -> str | None:
    try:
        return client.meta().get("langfuse_url")
    except (ApiError, ApiUnreachable):
        return None


def _json(console: Console, data: Any) -> int:
    console.print_json(data=data)
    return 0


# ---------------------------------------------------------------------- reads
def cmd_tasks(a: argparse.Namespace, client: AeosClient, console: Console) -> int:
    def rows() -> list[dict[str, Any]]:
        r = client.tasks()
        return [t for t in r if not a.status or t["status"] == a.status]

    if getattr(a, "json", False):
        return _json(console, rows())
    if a.watch and interactive(console):
        with Live(render.tasks_table(rows()), console=console, refresh_per_second=1) as live:
            try:
                while True:
                    time.sleep(3)
                    live.update(render.tasks_table(rows()))
            except KeyboardInterrupt:
                return 0
    console.print(render.tasks_table(rows()))
    return 0


def cmd_show(a: argparse.Namespace, client: AeosClient, console: Console) -> int:
    task = client.task(a.task_id)
    if a.json:
        return _json(console, task)
    if a.step:
        console.print(render.step_view(task, a.step))
        return 0
    if a.diff and not (a.plan or a.findings or a.verify or a.stats or a.pr or a.root_cause):
        show_diff(console, (task.get("artifacts") or {}).get("patch") or "(no patch yet)")
        return 0
    names = ("plan", "findings", "root-cause", "verify", "diff", "stats", "feedback", "pr")
    picked = {n for n in names if getattr(a, n.replace("-", "_"))}
    stats = None
    if not picked or "stats" in picked:
        try:
            stats = client.stats(a.task_id)
        except ApiError:
            pass  # not started yet
    decided = None if picked else client.approvals(None, a.task_id)
    view = render.render_task(
        task, stats, langfuse(client), picked or None, version=a.version, approvals=decided
    )
    console.print(view)
    return 0


def cmd_events(a: argparse.Namespace, client: AeosClient, console: Console) -> int:
    last = a.after
    for e in client.events(a.task_id, a.after):
        console.print(render.event_line(e))
        last = e["id"]
    if a.follow:
        try:
            for e in client.stream_events(
                a.task_id, last, stop_when=lambda e: e["type"] in END_EVENTS
            ):
                console.print(render.event_line(e))
        except KeyboardInterrupt:
            pass
    return 0


def cmd_stats(a: argparse.Namespace, client: AeosClient, console: Console) -> int:
    s = client.stats(a.task_id)
    if a.json:
        return _json(console, s)
    console.print(render.stats_table(s))
    return 0


def cmd_approvals(a: argparse.Namespace, client: AeosClient, console: Console) -> int:
    rows = client.approvals(None if a.all else "pending")
    if a.json:
        return _json(console, rows)
    if not rows:
        console.print("nothing is waiting for you" if not a.all else "no approvals")
        return 0
    if a.json:
        return _json(console, rows)
    console.print(render.approvals_table(rows))
    return 0


def cmd_status(a: argparse.Namespace, client: AeosClient, console: Console) -> int:
    console.print(safe(client.task(a.task_id)["status_line"]))
    return 0


def cmd_open(a: argparse.Namespace, client: AeosClient, console: Console) -> int:
    task, base = client.task(a.task_id), langfuse(client)
    if base and task.get("trace_id"):
        console.print(safe(f"trace: {base.rstrip('/')}/traces/{task['trace_id']}"))
    if task.get("pull_request"):
        console.print(safe(f"pull request: {task['pull_request']['url']}"))
    elif not task.get("trace_id"):
        console.print("nothing to open yet")
    return 0


# ---------------------------------------------------------------------- writes
def cmd_approve(a: argparse.Namespace, client: AeosClient, console: Console) -> int:
    decision = "approve" if a.cmd == "approve" else "reject"
    if not a.yes:
        card = next((r for r in client.approvals(None) if r["id"] == a.approval_id), None)
        if card:
            console.print(render.approval_panel(card))
    try:
        client.decide(a.approval_id, decision, actor_of(a), a.feedback)
    except ApiError as exc:
        if exc.status in (409, 410):
            console.print(safe(f"not recorded: {exc.detail}", "yellow"))
            return 1
        raise
    say(console, f"{decision}d as {clean(actor_of(a))}; the task resumes on the worker")
    return 0


def cmd_new(a: argparse.Namespace, client: AeosClient, console: Console) -> int:
    if a.yes and not a.actor:
        console.print("--yes needs an explicit --actor so the audit log names a person")
        return 1
    kind = a.kind or ("change" if client.meta().get("target_repo") else "regression")
    task_id = client.create_task(a.goal, kind)
    say(console, f"{task_id}  ({kind})")
    if not (a.watch or a.yes):
        return 0
    if a.yes and kind == "change":
        console.print("this will open a real pull request once the checks pass")
    live = interactive(console) and not a.yes
    while True:
        status = watch_task(client, console, task_id, actor=actor_of(a), interactive=live,
                            langfuse_url=langfuse(client))  # fmt: skip
        pending = (
            client.approvals("pending", task_id)
            if (a.yes and status == "awaiting_approval")
            else []
        )
        if not pending:
            return EXIT.get(status, 0)
        for p in pending:
            client.decide(p["id"], "approve", a.actor, None)


def cmd_watch(a: argparse.Namespace, client: AeosClient, console: Console) -> int:
    status = watch_task(
        client,
        console,
        a.task_id,
        actor=actor_of(a),
        interactive=interactive(console),
        langfuse_url=langfuse(client),
    )
    return EXIT.get(status, 0)


HANDLERS = {
    "tasks": cmd_tasks, "show": cmd_show, "events": cmd_events, "stats": cmd_stats,
    "approvals": cmd_approvals, "status": cmd_status, "open": cmd_open, "approve": cmd_approve,
    "reject": cmd_approve, "new": cmd_new, "watch": cmd_watch,
}  # fmt: skip


def run_command(args: argparse.Namespace, client: AeosClient, console: Console) -> int:
    try:
        if args.cmd == "chat":
            from aeos.client.chat import run_chat

            return run_chat(client, console, actor=actor_of(args), kind=args.kind)
        return HANDLERS[args.cmd](args, client, console)
    except ApiUnreachable as exc:
        console.print(safe(str(exc), "red"))
        return 2
    except ApiError as exc:
        console.print(safe(f"error: {exc.detail}" if exc.status != 404 else "not found", "red"))
        return 1
    except KeyboardInterrupt:
        return 130
