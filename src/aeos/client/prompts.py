"""The approval prompt: the terminal twin of the dashboard's approval card."""

from typing import Any

from rich.console import Console

from aeos.client import render
from aeos.client.api import AeosClient, ApiError
from aeos.client.sanitize import clean, say

HELP = "[a]pprove  [r]eject  [d]iff  [f]indings  [s]kip > "


def show_diff(console: Console, patch: str) -> None:
    if console.is_terminal:
        with console.pager(styles=True):
            console.print(render.diff_view(patch))
    else:
        console.print(render.diff_view(patch))


def ask_approval(
    client: AeosClient, console: Console, approval: dict[str, Any], *, actor: str
) -> str:
    """Returns approved | rejected | skipped | stale (decided elsewhere or expired)."""
    console.print(render.approval_panel(approval))
    ev = approval.get("evidence", {})
    while True:
        try:
            ans = console.input(HELP, markup=False).strip().lower()[:1]
        except (EOFError, KeyboardInterrupt):
            return "skipped"
        if ans == "d":
            show_diff(console, ev.get("patch") or "(no patch)")
        elif ans == "f":
            console.print(render.approval_panel({**approval, "evidence": {**ev, "patch": None}}))
        elif ans == "s":
            say(
                console,
                f"left pending: aeos approve {approval['id']}  |  aeos reject {approval['id']}",
            )
            return "skipped"
        elif ans in ("a", "r"):
            decision, feedback = ("approve", None) if ans == "a" else ("reject", None)
            if ans == "r":
                try:
                    feedback = (
                        console.input("reason (what should change?) > ", markup=False).strip()
                        or None
                    )
                except (EOFError, KeyboardInterrupt):
                    return "skipped"
            try:
                client.decide(approval["id"], decision, actor, feedback)
            except ApiError as exc:
                if exc.status in (409, 410):
                    msg = (
                        "this approval was already decided elsewhere"
                        if exc.status == 409
                        else "this approval expired; the task is escalated"
                    )
                    say(console, f"{msg} ({clean(exc.detail)})")
                    return "stale"
                say(console, f"could not record the decision: {clean(exc.detail)}")
                continue
            word = "approved" if ans == "a" else "rejected"
            say(console, f"{word} as {clean(actor)}; the task resumes on the worker")
            return word
