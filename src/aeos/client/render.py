"""Every dashboard view as a pure function: API JSON in, rich renderable out (nothing prints).

All text that an agent, tool or repo can influence goes through `safe`, so it is shown inertly.
"""

import json
from typing import Any

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from aeos.client.sanitize import clean, safe

GOOD = {"completed", "done", "passed", "approved"}
BAD = {"failed", "escalated", "rejected", "expired"}
WAIT = {"awaiting_approval", "pending", "paused"}
ICON = {"done": "✓", "running": "…", "failed": "✗", "pending": "·", "skipped": "-"}
ALL_SECTIONS = ("plan", "findings", "root-cause", "verify", "diff", "pr", "feedback", "stats")


def _style(status: str) -> str:
    return (
        "green"
        if status in GOOD
        else "red"
        if status in BAD
        else "yellow"
        if status in WAIT
        else "cyan"
    )


def _badge(status: str) -> Text:
    return Text(f"[{clean(status).replace('_', ' ')}]", style=_style(status))


def _kind(task: dict[str, Any]) -> str:
    return "change request" if task.get("kind") == "change" else "regression"


def _panel(title: "str | Text", *body: RenderableType) -> Panel:
    # Text, not str: a str title is parsed as markup and swallows "[pending]"
    return Panel(Group(*body), title=Text(title) if isinstance(title, str) else title,
                 title_align="left", border_style="dim")  # fmt: skip


def _json(v: Any, limit: int = 160) -> str:
    s = json.dumps(v, default=str)
    return s if len(s) <= limit else s[:limit] + "…"


def tasks_table(rows: list[dict[str, Any]]) -> Table:
    t = Table(box=None, pad_edge=False)
    for name in ("TASK", "TARGET", "STATUS", "TRIES", "CREATED"):
        t.add_column(name, no_wrap=True)  # fixed-width columns; only GOAL may wrap
    t.add_column("GOAL", min_width=20)
    for r in rows:
        target = "change" if r.get("kind") == "change" else "regression"
        t.add_row(safe(r["task_id"]), target, _badge(r["status"]), str(r.get("attempts", "")),
                  safe(r.get("created_at", ""))[:16], safe(r.get("goal", ""))[:70])  # fmt: skip
    return t


def header(task: dict[str, Any], langfuse_url: str | None = None) -> Group:
    top = Text.assemble(Text(clean(task["task_id"]), style="bold"), "  ", _badge(task["status"]),
                        "  ", Text(_kind(task), style="dim"))  # fmt: skip
    parts: list[RenderableType] = [top, safe(task.get("goal"))]
    if task.get("status_line"):
        parts.append(safe(task["status_line"], "dim"))
    if task.get("error"):
        parts.append(safe(f"error: {task['error']}", "red"))
    if langfuse_url and task.get("trace_id"):
        parts.append(safe(f"trace: {langfuse_url.rstrip('/')}/traces/{task['trace_id']}", "blue"))
    return Group(*parts)


def _depths(steps: list[dict[str, Any]]) -> dict[str, int]:
    by_id = {s["id"]: s for s in steps}
    depth: dict[str, int] = {}

    def d(sid: str, seen: frozenset[str] = frozenset()) -> int:
        if sid in depth:
            return depth[sid]
        if sid in seen or sid not in by_id:
            return 0
        deps = by_id[sid].get("depends_on", [])
        depth[sid] = 1 + max((d(x, seen | {sid}) for x in deps if x in by_id), default=-1)
        return depth[sid]

    for s in steps:
        d(s["id"])
    return depth


def plan_view(task: dict[str, Any], version: int | None = None) -> RenderableType:
    plans = task.get("plans") or []
    if not plans:
        return _panel("PLAN", Text("planning…", style="dim"))
    plan = next(
        (p for p in plans if p["version"] == (version or task.get("plan_version"))), plans[-1]
    )
    depth = _depths(plan["steps"])
    cols: dict[int, list[Panel]] = {}
    for s in plan["steps"]:
        status = s["status"]
        note = f" · attempt {s['attempts']}" if s.get("attempts", 0) > 1 else ""
        line = f"{ICON.get(status, '?')} {s['owner']} · {status}{note}"
        body = [Text(line, style=_style(status)), safe(s["description"])]
        if s.get("depends_on"):
            body.append(safe("after " + ", ".join(s["depends_on"]), "dim"))
        cols.setdefault(depth[s["id"]], []).append(
            Panel(Group(*body), title=safe(s["id"]), width=34)
        )
    grid = Table.grid(padding=(0, 1))
    for _ in cols:
        grid.add_column()
    grid.add_row(*(Group(*cols[k]) for k in sorted(cols)))
    versions = " ".join(f"v{p['version']}" for p in plans)
    head = [safe(f"versions: {versions} (showing v{plan['version']})", "dim")]
    if plan.get("reason_for_change"):
        head.append(safe(f"Replanned because: {plan['reason_for_change']}", "yellow"))
    return _panel(f"PLAN v{plan['version']}", *head, grid)


def step_view(task: dict[str, Any], step_id: str) -> RenderableType:
    parts: list[RenderableType] = []
    for o in (x for x in task.get("observations", []) if x["step_id"] == step_id):
        parts.append(
            Text.assemble(safe(o["agent"], "bold"), f"  {o.get('origin', 'trusted')}", style="dim")
        )
        parts.append(safe(o["summary"]))
    calls = [c for c in task.get("tool_calls", []) if c["step_id"] == step_id]
    for c in calls:
        head = Text.assemble(
            (f"{clean(c['server'])}.{clean(c['tool'])}", "red" if c.get("is_error") else "bold"),
            f"  {c.get('risk', '')} · {round(c.get('duration_ms', 0))} ms · {c.get('origin', '')}",
        )  # fmt: skip
        parts += [head, safe("  args: " + _json(c.get("arguments", {})), "dim"),
                  safe("  → " + clean(c.get("result_preview", ""))[:600], "dim")]  # fmt: skip
    return _panel(
        f"STEP {clean(step_id)} ({len(calls)} tool calls)",
        *(parts or [Text("nothing recorded", style="dim")]),
    )


def findings_panel(task: dict[str, Any]) -> RenderableType | None:
    r = (task.get("artifacts") or {}).get("research")
    if not r or task.get("kind") != "change":
        return None
    parts: list[RenderableType] = [safe(r.get("summary")), safe(r.get("current_state"), "dim")]
    parts += [
        Text.assemble((clean(f["path"]), "bold"), f" — {clean(f['why'])}")
        for f in r.get("relevant_files", [])
    ]
    parts += [safe(f"Risk: {x}", "dim") for x in r.get("risks", [])]
    return _panel("RESEARCH FINDINGS", *parts)


def root_cause_panel(task: dict[str, Any]) -> RenderableType | None:
    rc = task.get("root_cause")
    if not rc:
        return None
    parts: list[RenderableType] = [
        Text.assemble(
            (clean(rc["file"]), "bold"),
            f" @ {clean(rc['commit_sha'])[:10]} (confidence {rc['confidence']})",
        ),
        safe(rc["summary"]),
    ]  # fmt: skip
    parts += [safe(f"Rejected earlier diagnosis: {r['file']} — {r['summary']}", "dim")
              for r in (task.get("artifacts") or {}).get("rejected_root_causes", [])]  # fmt: skip
    return _panel("ROOT CAUSE", *parts)


def gates_text(gates: dict[str, bool]) -> Text:
    t = Text()
    for k, ok in gates.items():
        t.append(f"{'✓' if ok else '✗'} {clean(k)}  ", style="green" if ok else "red")
    return t


def _bench(b: dict[str, Any]) -> Text:
    base, cand = b["baseline"], b["candidate"]
    return Text(f"p95 {base['p95_ms']} → {cand['p95_ms']} ms ({b.get('p95_change_pct', 0):+}%)   "
                f"db queries {base['db_queries']} → {cand['db_queries']}")  # fmt: skip


def verification_panel(task: dict[str, Any]) -> RenderableType | None:
    vs = task.get("verifications") or []
    if not vs:
        return None
    v = vs[-1]
    parts: list[RenderableType] = [
        Text.assemble(_badge(v["status"]), "  ", gates_text(v.get("gates", {})))
    ]
    m = v.get("metrics", {})
    if m.get("benchmark"):
        parts.append(_bench(m["benchmark"]))
    if (m.get("tests") or {}).get("ran") is False:
        parts.append(safe("no automated test suite was run for this repository", "dim"))
    parts += [safe(f"{i['type']}: {i['detail']}", "red") for i in v.get("issues", [])]
    parts += [
        safe(f"judge concern: {c}", "yellow")
        for c in (v.get("judgement") or {}).get("concerns", [])
    ]
    return _panel(f"VERIFICATION (attempt {v['attempt']})", *parts)


def diff_view(patch: str) -> RenderableType:
    return Syntax(clean(patch), "diff", theme="ansi_dark", word_wrap=True)


def pr_panel(task: dict[str, Any]) -> RenderableType | None:
    pr = task.get("pull_request")
    if not pr:
        return None
    return _panel("PULL REQUEST", safe(f"#{pr['number']} {pr['title']}"), safe(pr["url"], "blue"))


def feedback_panel(task: dict[str, Any]) -> RenderableType | None:
    fb = task.get("human_feedback") or []
    return (
        _panel("HUMAN FEEDBACK", *(safe(f"{f.get('actor')}: {f.get('feedback')}") for f in fb))
        if fb
        else None
    )


def stats_table(s: dict[str, Any]) -> Table:
    llm, tools = s.get("llm", {}), s.get("tools", {})
    dur, ver = s.get("duration_s"), s.get("verifications", {})
    rows = [
        ("Duration", f"{dur:.1f} s" if dur is not None else "—"),
        ("LLM calls", f"{llm.get('calls', 0)} ({llm.get('tokens', 0):,} tokens)"),
        ("Cost", f"${llm.get('cost_usd', 0):.4f}"),
        ("Tool calls", f"{tools.get('calls', 0)} ({tools.get('errors', 0)} errors)"),
        ("Replans / retries", f"{s.get('replans', 0)} / {s.get('step_retries', 0)}"),
        ("Verifications", f"{ver.get('total', 0)} ({ver.get('failed', 0)} failed)"),
        ("Approval wait", f"{s.get('approvals', {}).get('total_wait_s', 0):.0f} s"),
        ("Memory recalled", str(s.get("memory", {}).get("retrieved", 0))),
    ]  # fmt: skip
    t = Table.grid(padding=(0, 2))
    for k, v in rows:
        t.add_row(Text(k, style="dim"), v)
    return t


def event_line(e: dict[str, Any]) -> Text:
    msg = clean(e.get("message", ""))
    msg = msg if len(msg) <= 300 else msg[:300] + "…"
    return Text.assemble(
        clean(str(e.get("ts", ""))[11:19]), "  ", (clean(e.get("agent", "")), "bold"),
        "  ", clean(e.get("type", "")), ("  " + msg, "dim"),
    )  # fmt: skip


def approval_panel(a: dict[str, Any], *, with_diff: bool = False) -> RenderableType:
    args, ev = a.get("arguments", {}), a.get("evidence", {})
    parts: list[RenderableType] = [
        Text.assemble((clean(a["tool"]), "bold"), f": {clean(args.get('title'))}"),
        safe(f"{args.get('head')} → {args.get('base')}", "dim"),
        safe(a.get("rationale"), "dim"),
        gates_text(ev.get("gates") or {}),
    ]  # fmt: skip
    if ev.get("benchmark"):
        parts.append(_bench(ev["benchmark"]))
    rc, research = ev.get("root_cause"), ev.get("research")
    if rc:
        parts.append(safe(f"Root cause: {rc['file']} — {rc['summary']}"))
    elif research:
        parts.append(safe(f"Findings: {research.get('summary')}"))
    parts += [
        safe(f"  {f['path']} +{f['added']} −{f['removed']}", "dim") for f in ev.get("files") or []
    ]
    if with_diff and ev.get("patch"):
        parts.append(diff_view(ev["patch"]))
    expires = clean(a.get("expires_at", ""))[:16]
    title = f"APPROVAL {clean(a['id'])} [{clean(a['status'])}] expires {expires}"
    return _panel(title, *parts)


def decided_panel(rows: list[dict[str, Any]]) -> RenderableType | None:
    done = [a for a in rows if a.get("status") != "pending"]
    if not done:
        return None
    lines = (f"{a['id']} {a['status']} by {a.get('actor') or '—'}  {a['tool']}" for a in done)
    return _panel("DECIDED APPROVALS", *(safe(x) for x in lines))


def approvals_table(rows: list[dict[str, Any]]) -> Table:
    t = Table("APPROVAL", "TASK", "STATUS", "TITLE", "EXPIRES", box=None, pad_edge=False)
    for a in rows:
        title = safe((a.get("arguments") or {}).get("title"))[:60]
        t.add_row(
            safe(a["id"]),
            safe(a["task_id"]),
            _badge(a["status"]),
            title,
            safe(a.get("expires_at", ""))[:16],
        )
    return t


def render_task(
    task: dict[str, Any],
    stats: dict[str, Any] | None = None,
    langfuse_url: str | None = None,
    sections: set[str] | None = None,
    *,
    version: int | None = None,
    approvals: list[dict[str, Any]] | None = None,
) -> RenderableType:
    want = set(ALL_SECTIONS) if sections is None else sections
    out: list[RenderableType] = [header(task, langfuse_url)]
    if "plan" in want:
        out.append(plan_view(task, version))
    if "findings" in want:
        out.append(findings_panel(task))  # type: ignore[arg-type]
    if "root-cause" in want:
        out.append(root_cause_panel(task))  # type: ignore[arg-type]
    if "verify" in want:
        out.append(verification_panel(task))  # type: ignore[arg-type]
    patch = (task.get("artifacts") or {}).get("patch")
    if "diff" in want and patch:
        lines = clean(patch).splitlines()
        shown = "\n".join(lines) if sections is not None else "\n".join(lines[:40])
        more = len(lines) - 40
        note = safe(f"… {more} more lines, use: aeos show {task['task_id']} --diff", "dim")
        out.append(
            _panel("PATCH", diff_view(shown), *([note] if sections is None and more > 0 else []))
        )
    if "pr" in want:
        out.append(pr_panel(task))  # type: ignore[arg-type]
    if "feedback" in want:
        out.append(feedback_panel(task))  # type: ignore[arg-type]
    if approvals and sections is None:
        out.append(decided_panel(approvals))  # type: ignore[arg-type]
    if "stats" in want and stats:
        out.append(_panel("STATS", stats_table(stats)))
    return Group(*(x for x in out if x is not None))
