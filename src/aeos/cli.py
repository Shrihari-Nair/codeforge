"""`aeos` command line.

aeos run [task]            run one task inline, streaming events (prompts for approval)
aeos serve                 API server with an embedded worker
aeos worker                standalone worker (claims queued tasks, survives crashes)
aeos submit <goal>         enqueue a task for a worker
aeos status <task_id>      show a task's status line
aeos approvals             list pending approvals
aeos approve|reject <id>   decide (records the actor; the task resumes on a worker)

Terminal client of the running API (same features as the dashboard):
aeos chat                  interactive session: type a request, watch, approve inline
aeos tasks | new | show | watch | events | stats | open
(status/approvals/approve/reject use the API when --api or AEOS_API_URL is set)
"""

import argparse
import asyncio
import os
import sys
import uuid
from typing import Any

from aeos.core.config import get_settings
from aeos.core.events import CompositeSink, ConsoleSink, EventSink
from aeos.core.logging import configure_logging
from aeos.core.state import TaskState
from aeos.demo.trajectories import SCENARIOS
from aeos.graph.build import run_task
from aeos.graph.checkpoint import open_checkpointer
from aeos.hitl import DecisionError, apply_decision, auto_approver
from aeos.llm.base import LLMError, LLMProvider
from aeos.llm.factory import get_llm
from aeos.runtime import open_runtime
from aeos.store import approvals, tasks
from aeos.store.approvals import ApprovalRequest
from aeos.store.events import DbSink

DEMO_TASK = (
    "Investigate why checkout latency increased after the latest deployment. Determine the root "
    "cause, propose a fix, implement it, test it against the previous version, and prepare a PR. "
    "Don't merge anything without my approval."
)


def _llm(args: argparse.Namespace) -> LLMProvider:
    settings = get_settings()
    replay = getattr(args, "replay", None) or os.environ.get("AEOS_REPLAY")
    if replay:
        return SCENARIOS[replay]()
    return get_llm(settings.model_copy(update={"llm_provider": args.llm or settings.llm_provider}))


def _summarize(req: ApprovalRequest) -> str:
    ev = req.evidence
    bench = ev.get("benchmark") or {}
    gates = ", ".join(f"{'✓' if ok else '✗'}{g}" for g, ok in (ev.get("gates") or {}).items())
    lines = [
        "",
        f"⏸  APPROVAL NEEDED [{req.id}]: {req.tool}",
        f"   title : {req.arguments.get('title')}",
        f"   branch: {req.arguments.get('head')} -> {req.arguments.get('base')}",
        f"   gates : {gates}",
    ]
    if bench:
        b, c = bench["baseline"], bench["candidate"]
        lines.append(
            f"   p95   : {b['p95_ms']} ms -> {c['p95_ms']} ms ({bench['p95_change_pct']:+}%)"
        )
    lines.append("   files : " + ", ".join(f["path"] for f in (ev.get("files") or [])))
    return "\n".join(lines)


async def _console_approver(req: ApprovalRequest) -> dict[str, Any]:
    print(_summarize(req))
    answer = await asyncio.to_thread(
        input, "   Approve opening this PR? [y/N or type feedback to reject] "
    )
    if answer.strip().lower() in ("y", "yes"):
        return {"decision": "approved", "actor": "cli-user", "feedback": ""}
    feedback = "" if answer.strip().lower() in ("", "n", "no") else answer.strip()
    return {"decision": "rejected", "actor": "cli-user", "feedback": feedback}


# ---------------------------------------------------------------------- run
async def _run(args: argparse.Namespace) -> TaskState:
    settings = get_settings()
    approver = auto_approver if args.yes else _console_approver
    sink: EventSink = ConsoleSink(verbose=args.verbose)
    async with (
        open_runtime(settings, llm=_llm(args), sink=sink) as rt,
        open_checkpointer(settings) as saver,
    ):
        return await run_task(
            rt, args.task, task_id=args.task_id, checkpointer=saver, approver=approver,
            kind=args.kind,
        )  # fmt: skip


# ---------------------------------------------------------------------- worker / serve
async def _worker(args: argparse.Namespace) -> None:
    from aeos.worker import Worker

    settings = get_settings()
    sink: EventSink = CompositeSink(DbSink(), *([ConsoleSink()] if args.console else []))
    stop = asyncio.Event()
    async with (
        open_runtime(settings, llm=_llm(args), sink=sink) as rt,
        open_checkpointer(settings) as saver,
    ):
        worker = Worker(
            rt, saver, lease_s=args.lease, poll_s=args.poll, sweep_s=args.sweep,
            reap_grace_s=args.reap_grace, approver=auto_approver if args.yes else None,
        )  # fmt: skip
        if args.once:
            await asyncio.to_thread(tasks.ensure_schema)
            await asyncio.to_thread(approvals.ensure_schema)
            while await worker.run_once():
                pass
            return
        print(f"worker {worker.worker_id} ready", file=sys.stderr, flush=True)
        await worker.run_forever(stop)


def _serve(args: argparse.Namespace) -> None:
    import uvicorn

    from aeos.api.app import create_app

    settings = get_settings()
    app = create_app(
        settings=settings,
        runtime_factory=lambda: open_runtime(settings, llm=_llm(args), sink=DbSink()),
        embedded_worker=not args.no_worker,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


# ---------------------------------------------------------------------- store-backed commands
def _submit(args: argparse.Namespace) -> int:
    tasks.ensure_schema()
    task_id = f"t-{uuid.uuid4().hex[:8]}"
    tasks.enqueue(task_id, args.goal)
    print(task_id)
    return 0


def _status(args: argparse.Namespace) -> int:
    row = tasks.get(args.task_id)
    if row is None:
        print(f"no task {args.task_id}", file=sys.stderr)
        return 1

    async def line() -> str:
        from aeos.graph.build import _snapshot, build_graph

        settings = get_settings()
        async with open_checkpointer(settings) as saver:
            graph = build_graph(None, saver)  # type: ignore[arg-type]  # read-only use
            snap = await graph.aget_state({"configurable": {"thread_id": args.task_id}})
            if not snap.values:
                return f"Task {row.task_id} [{row.status}] (not started)"
            state, _ = await _snapshot(graph, args.task_id)
            return state.status_line()

    print(asyncio.run(line()))
    return 0


def _list_approvals(_: argparse.Namespace) -> int:
    approvals.ensure_schema()
    for r in approvals.list_requests(status="pending"):
        print(f"{r.id}  task={r.task_id}  {r.tool}  expires {r.expires_at:%Y-%m-%d %H:%M}")
    return 0


def _decide(args: argparse.Namespace, decision: str) -> int:
    try:
        req, _ = asyncio.run(
            apply_decision(args.approval_id, decision, args.actor, args.feedback)  # type: ignore[arg-type]
        )
    except DecisionError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1
    print(f"{req.id}: {req.status} by {req.actor}; task {req.task_id} will resume on a worker")
    return 0


CLIENT_CMDS = {"tasks", "new", "show", "watch", "events", "stats", "open", "chat"}
LEGACY_CMDS = {"status", "approvals", "approve", "reject"}  # DB-backed unless --api / AEOS_API_URL


def _client_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--api", help="AEOS API URL (default: $AEOS_API_URL or http://localhost:8800)")
    p.add_argument("--actor", help="your name for the audit log (default: $AEOS_ACTOR or $USER)")


def _client_commands(sub: Any) -> None:
    def add(name: str, help_: str, *flags: str) -> argparse.ArgumentParser:
        p: argparse.ArgumentParser = sub.add_parser(name, help=help_)
        _client_flags(p)
        for f in flags:
            p.add_argument(f, action="store_true")
        return p

    t = add("tasks", "list tasks", "--watch", "--json")
    t.add_argument("--status")
    n = add("new", "submit a request", "--watch", "--yes")
    n.add_argument("goal")
    n.add_argument("--kind", choices=["change", "regression"])
    s = add("show", "show a task", "--plan", "--findings", "--root-cause", "--verify", "--diff",
            "--stats", "--feedback", "--pr", "--json")  # fmt: skip
    s.add_argument("task_id")
    s.add_argument("--step")
    s.add_argument("--version", type=int)
    for name, help_ in (
        ("watch", "follow a task live"),
        ("stats", "cost and usage"),
        ("open", "trace and PR links"),
    ):
        add(name, help_, *(("--json",) if name == "stats" else ())).add_argument("task_id")
    e = add("events", "task events", "--follow")
    e.add_argument("task_id")
    e.add_argument("--after", type=int, default=0)
    c = add("chat", "interactive session")
    c.add_argument("--kind", choices=["change", "regression"])


def main(argv: list[str] | None = None, *, _client: Any = None, _console: Any = None) -> int:
    parser = argparse.ArgumentParser(prog="aeos", description="Autonomous Engineering OS")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def llm_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--llm", choices=["gemini"], help="live provider (default: from settings)")
        p.add_argument("--replay", choices=sorted(SCENARIOS), help="scripted scenario (no API)")

    run = sub.add_parser("run", help="run a task inline")
    run.add_argument("task", nargs="?", default=DEMO_TASK)
    llm_flags(run)
    run.add_argument("--task-id")
    run.add_argument("--yes", action="store_true", help="auto-approve the PR (demos only)")
    run.add_argument("-v", "--verbose", action="store_true")

    worker = sub.add_parser("worker", help="standalone worker")
    llm_flags(worker)
    worker.add_argument("--once", action="store_true", help="drain the queue and exit")
    worker.add_argument("--yes", action="store_true", help="auto-approve (chaos tests only)")
    worker.add_argument("--lease", type=float, default=30.0)
    worker.add_argument("--poll", type=float, default=1.0)
    worker.add_argument("--sweep", type=float, default=10.0, help="housekeeping interval")
    worker.add_argument("--reap-grace", type=float, default=120.0)
    worker.add_argument("--console", action="store_true", help="print events")

    serve = sub.add_parser("serve", help="API server (+ embedded worker)")
    llm_flags(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--no-worker", action="store_true")

    agent = sub.add_parser("agent", help="run one specialist as an A2A service")
    agent.add_argument("owner", choices=["analytics", "research", "coding"])
    agent.add_argument("--host", default="127.0.0.1")
    agent.add_argument("--port", type=int, required=True)
    agent.add_argument("--public-url")
    llm_flags(agent)

    submit = sub.add_parser("submit", help="enqueue a task (direct, no API)")
    submit.add_argument("goal", nargs="?", default=DEMO_TASK)
    status = sub.add_parser("status", help="show a task")
    status.add_argument("task_id")
    _client_flags(status)
    appr = sub.add_parser("approvals", help="list pending approvals")
    appr.add_argument("--all", action="store_true")
    appr.add_argument("--json", action="store_true")
    _client_flags(appr)
    for name in ("approve", "reject"):
        d = sub.add_parser(name, help=f"{name} a pending request")
        d.add_argument("approval_id")
        d.add_argument("--actor", default=None)
        d.add_argument("--api")
        d.add_argument("--feedback")
        d.add_argument("--yes", action="store_true", help="skip showing the approval card")
    _client_commands(sub)
    run.add_argument("--kind", choices=["change", "regression"], default="regression")

    args = parser.parse_args(argv)
    api_only_flags = getattr(args, "all", False) or getattr(args, "json", False)
    if args.cmd in CLIENT_CMDS or (
        args.cmd in LEGACY_CMDS
        and (getattr(args, "api", None) or os.environ.get("AEOS_API_URL") or api_only_flags)
    ):
        from rich.console import Console

        from aeos.client.api import AeosClient, default_api_url
        from aeos.client.commands import run_command

        client = _client or AeosClient(getattr(args, "api", None) or default_api_url())
        return run_command(
            args, client, _console or Console(markup=False, highlight=False, emoji=False)
        )
    if args.cmd in ("approve", "reject"):
        args.actor = args.actor or os.environ.get("USER", "cli-user")
    configure_logging("INFO" if getattr(args, "verbose", False) else "ERROR")
    from aeos.telemetry.setup import init_telemetry

    init_telemetry(f"aeos-{args.cmd}" + (f"-{args.owner}" if args.cmd == "agent" else ""))
    try:
        match args.cmd:
            case "run":
                state = asyncio.run(_run(args))
                print("\n" + (state.artifacts.report or state.status_line()))
                return 0 if state.status == "completed" else 1
            case "worker":
                asyncio.run(_worker(args))
            case "serve":
                _serve(args)
            case "agent":
                from aeos.a2a.service import serve as serve_agent

                asyncio.run(
                    serve_agent(
                        args.owner,
                        host=args.host,
                        port=args.port,
                        llm=_llm(args),
                        public_url=args.public_url,
                    )
                )
            case "submit":
                return _submit(args)
            case "status":
                return _status(args)
            case "approvals":
                return _list_approvals(args)
            case "approve" | "reject":
                return _decide(args, "approved" if args.cmd == "approve" else "rejected")
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
