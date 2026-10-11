"""Follow one task live: history, then the event stream, with inline approval."""

from typing import Any

from rich.console import Console

from aeos.client import render
from aeos.client.api import AeosClient, ApiError
from aeos.client.prompts import ask_approval
from aeos.client.sanitize import say

TERMINAL = ("completed", "failed", "escalated", "cancelled")
END_EVENTS = {"task_completed", "task_failed", "task_escalated"}


def _after(console: Console, event_type: str, task: dict[str, Any]) -> None:
    """The dashboard re-renders a card when its data changes; print the same card once."""
    if event_type in ("step_done", "step_failed", "replan"):
        console.print(render.plan_view(task))
    elif event_type == "root_cause":
        console.print(render.root_cause_panel(task))
    elif event_type == "verification":
        console.print(render.verification_panel(task))
    elif event_type == "pr_created":
        console.print(render.pr_panel(task))


def _finish(client: AeosClient, console: Console, task_id: str) -> str:
    task = client.task(task_id)
    if task["status"] in TERMINAL:
        for s in (render.findings_panel(task), render.pr_panel(task)):
            if s is not None:
                console.print(s)
        try:
            console.print(render.stats_table(client.stats(task_id)))
        except ApiError:
            pass
    return str(task["status"])


def watch_task(
    client: AeosClient, console: Console, task_id: str, *, actor: str, interactive: bool,
    langfuse_url: str | None = None,
) -> str:  # fmt: skip
    """Returns the last known task status. Ctrl+C detaches; the task keeps running."""
    # history BEFORE the task: a task read after it is at least as new, so a finish or approval
    # request that lands in between is seen in the status and not waited for on the stream
    history = client.events(task_id)
    task = client.task(task_id)
    console.print(render.header(task, langfuse_url))
    for e in history:
        console.print(render.event_line(e))
    if task["status"] in TERMINAL:
        return _finish(client, console, task_id)
    last = history[-1]["id"] if history else 0
    handled: set[str] = set()
    try:
        if task["status"] == "awaiting_approval":  # joined while a decision is already pending
            if not _handle_approval(client, console, task_id, actor, interactive, handled):
                return "awaiting_approval"
        for e in client.stream_events(task_id, last, stop_when=lambda e: e["type"] in END_EVENTS):
            console.print(render.event_line(e))
            _after(console, e["type"], client.task(task_id))
            if e["type"] == "approval_requested" and not _handle_approval(
                client, console, task_id, actor, interactive, handled
            ):
                return "awaiting_approval"
    except KeyboardInterrupt:
        say(console, f"\ndetached; the task keeps running (aeos watch {task_id} to rejoin)")
        try:
            return str(client.task(task_id)["status"])
        except Exception:  # noqa: BLE001
            return "unknown"
    return _finish(client, console, task_id)


def _handle_approval(
    client: AeosClient, console: Console, task_id: str, actor: str, interactive: bool,
    handled: set[str],
) -> bool:  # fmt: skip
    """True = keep following. False = stop here (non-interactive: tell the user what to run)."""
    pending = [a for a in client.approvals("pending", task_id) if a["id"] not in handled]
    handled.update(a["id"] for a in pending)
    for a in pending:
        if interactive:
            ask_approval(client, console, a, actor=actor)
        else:
            console.print(render.approval_panel(a))
            say(
                console,
                f"approval {a['id']} is waiting: aeos approve {a['id']} | aeos reject {a['id']}",
            )
    return interactive or not pending
