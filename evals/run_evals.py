"""Run the eval suite and write a versioned results file + markdown table.

uv run python -m evals.run_evals --suite smoke                  # FakeLLM, 6 tasks, CI-safe
uv run python -m evals.run_evals --suite full --llm gemini --trials 3 --parallel 3
uv run python -m evals.run_evals --suite smoke --break retry    # deliberately broken config
uv run python -m evals.run_evals --suite smoke --baseline evals/results/<file>.json
"""

import argparse
import asyncio
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aeos.core.config import get_settings
from aeos.core.events import ListSink
from aeos.core.retry import RetryPolicy
from aeos.core.state import TaskState
from aeos.demo.trajectories import happy_path_llm, wrong_fix_then_fix_llm
from aeos.graph.build import run_task
from aeos.hitl import auto_approver
from aeos.llm.base import LLMProvider
from aeos.llm.factory import get_llm
from aeos.runtime import open_runtime
from evals.env import Env, load_tasks, prepare
from evals.graders import grade_all
from evals.metrics import METRICS, aggregate, compare

RESULTS = Path(__file__).parent / "results"
ROOT = Path(__file__).resolve().parents[1]


def code_version() -> dict[str, str]:
    """Hashes that identify WHAT was evaluated, so two runs can be compared meaningfully."""

    def digest(paths: list[Path]) -> str:
        h = hashlib.sha256()
        for p in sorted(paths):
            h.update(p.read_bytes())
        return h.hexdigest()[:12]

    return {
        "code": digest(list((ROOT / "src").rglob("*.py"))),
        "prompts": digest(
            [
                ROOT / "src/aeos/agents/specialists.py",
                ROOT / "src/aeos/agents/supervisor.py",
                ROOT / "src/aeos/agents/judge.py",
            ]
        ),  # fmt: skip
        "policy": digest([ROOT / "policy/risk_policy.yaml"]),
        "dataset": digest([ROOT / "evals/datasets/tasks.yaml"]),
        "graders": digest([ROOT / "evals/graders.py", ROOT / "evals/metrics.py"]),
    }


def select(tasks: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.suite == "smoke":
        tasks = [t for t in tasks if t["environment"]["seed"] == 1]
    elif args.suite != "full":
        tasks = [t for t in tasks if t["id"] in args.suite.split(",")]
    if args.templates:
        want = set(args.templates.split(","))
        tasks = [t for t in tasks if t["environment"]["template"] in want]
    if args.limit:
        tasks = tasks[: args.limit]
    return tasks


def make_strong_llm(args: argparse.Namespace) -> LLMProvider | None:
    """Planner/synthesis/replan/judge model (None = reuse the main one, as in FakeLLM runs)."""
    if args.llm == "gemini":
        return get_llm(get_settings().model_copy(update={"llm_provider": "gemini"}), strong=True)
    return None


def make_llm(args: argparse.Namespace, env: Env) -> LLMProvider:
    if args.llm == "gemini":
        s = get_settings().model_copy(update={"llm_provider": "gemini"})
        return get_llm(s)
    fixes = env.template.fix_files
    if args.scenario == "wrong_fix":
        return wrong_fix_then_fix_llm(env.truth, fixes)
    return happy_path_llm(env.truth, fixes)


async def run_one(
    task: dict[str, Any], trial: int, args: argparse.Namespace, sem: asyncio.Semaphore
) -> dict[str, Any]:
    async with sem:
        env = await prepare(task)
        started = time.time()
        sink = ListSink()
        retry = (
            RetryPolicy(max_verification_failures=1) if args.break_ == "retry" else RetryPolicy()
        )
        settings = env.settings(memory_enabled=args.memory == "on", telemetry_enabled=False)
        state: TaskState | None = None
        error: str | None = None
        try:
            async with open_runtime(
                settings, llm=make_llm(args, env), sink=sink, retry=retry
            ) as rt:
                state = await asyncio.wait_for(
                    run_task(rt, task["task"], approver=auto_approver), timeout=args.task_timeout
                )
                judge = get_llm(settings.model_copy(update={"llm_provider": "gemini"}), strong=True) \
                    if args.llm == "gemini" else None  # fmt: skip
                secrets = [v.get_secret_value() for v in (settings.gemini_api_key,) if v]
                grades = await grade_all(state, env, rt.sandbox, judge=judge, secrets=secrets)
        except Exception as exc:  # a crashed run is a failed run, recorded, never hidden
            error = f"{type(exc).__name__}: {exc}"
        finally:
            env.cleanup()
        latency = time.time() - started
    if state is None or error:
        return {"task_id": task["id"], "template": env.template.id, "seed": task["environment"]["seed"],
                "trial": trial, "status": "crashed", "success": False, "error": error, "latency_s": latency,
                "had_failure": False, "agent_steps": 0, "llm_calls": 0, "cost_usd": 0.0,
                "grades": _zero_grades()}  # fmt: skip
    success = (
        state.status == "completed" and state.artifacts.pull_request is not None
        and grades["outcome_tests"].passed and grades["outcome_benchmark"].passed
        and grades["root_cause_match"].passed and grades["trace_policy"].passed
    )  # fmt: skip
    fix_success = (
        state.status == "completed" and state.artifacts.pull_request is not None
        and grades["outcome_tests"].passed and grades["outcome_benchmark"].passed
        and grades["trace_policy"].passed
    )  # fmt: skip
    steps = [s for p in state.plans for s in p.steps]
    had_failure = (
        any(v.status == "failed" for v in state.verifications)
        or any(s.attempts > 1 for s in steps if s.owner != "supervisor")
        or state.status == "escalated"
    )
    return {
        "task_id": task["id"], "template": env.template.id, "seed": task["environment"]["seed"],
        "trial": trial, "status": state.status, "success": success, "fix_success": fix_success,
        "error": state.error,
        "latency_s": round(latency, 2), "had_failure": had_failure,
        "agent_steps": state.budget.steps_used, "llm_calls": state.budget.llm_calls,
        "tool_calls": state.budget.tool_calls, "cost_usd": round(state.budget.cost_usd, 5),
        "tokens": state.budget.tokens_used, "plans": len(state.plans),
        "verifications": [v.status for v in state.verifications],
        "tools_used": sorted({t.tool for t in state.tool_calls}),
        "root_cause": state.artifacts.root_cause.model_dump() if state.artifacts.root_cause else None,
        "grades": {k: g.model_dump() for k, g in grades.items()},
    }  # fmt: skip


def _zero_grades() -> dict[str, Any]:
    names = ["outcome_tests", "outcome_benchmark", "root_cause_match", "trace_policy", "tool_selection",
             "planning", "llm_rubric"]  # fmt: skip
    return {n: {"name": n, "passed": False, "score": 0.0, "detail": "run crashed"} for n in names}


def markdown(result: dict[str, Any], deltas: list[dict[str, Any]] | None) -> str:
    meta, agg = result["meta"], result["aggregate"]
    lines = [
        f"# Eval results: {meta['label']}", "",
        f"- run: `{meta['run_id']}`  llm: `{meta['llm']}`  scenario: `{meta['scenario']}`  break: `{meta['break']}`  "
        f"memory: `{meta['memory']}`",
        f"- tasks: {agg['overall']['tasks']}  trials/task: {meta['trials']}  runs: {agg['trials']}",
        "- versions: " + ", ".join(f"{k} `{v}`" for k, v in meta["version"].items()), "",
        "## Overall", "", "| metric | value (mean ± stderr) |", "|---|---|",
    ]  # fmt: skip
    for key, label in METRICS:
        m = agg["overall"][key]
        value = (
            f"{m['mean']:.3f} ± {m['stderr']:.3f} (n={m['n']})" if m["n"] else "n/a (none occurred)"
        )
        lines.append(f"| {label} | {value} |")
    lines += ["", "## By template", "", "| template | success | root cause | recovery | steps | cost | latency |",
              "|---|---|---|---|---|---|---|"]  # fmt: skip
    for tpl, m in agg["by_template"].items():
        rec = f"{m['recovery_rate']['mean']:.2f}" if m["recovery_rate"]["n"] else "n/a"
        lines.append(f"| {tpl} | {m['success_rate']['mean']:.2f} | {m['root_cause_accuracy']['mean']:.2f} | "
                     f"{rec} | {m['avg_agent_steps']['mean']:.1f} | "
                     f"${m['avg_cost_usd']['mean']:.3f} | {m['avg_latency_s']['mean']:.0f}s |")  # fmt: skip
    if deltas:
        lines += [
            "",
            "## vs previous run",
            "",
            "| metric | previous | current | delta | |",
            "|---|---|---|---|---|",
        ]
        for d in deltas:
            flag = "REGRESSED" if d["regressed"] else ""
            lines.append(
                f"| {d['label']} | {d['previous']:.3f} | {d['current']:.3f} | {d['delta']:+.3f} | {flag} |"
            )
    failures = [r for r in result["runs"] if not r["success"]]
    if failures:
        lines += ["", "## Failures", ""]
        for r in failures:
            why = r.get("error") or "; ".join(
                f"{k}: {v['detail']}" for k, v in r["grades"].items() if not v["passed"]
            )
            lines.append(f"- `{r['task_id']}` (trial {r['trial']}, {r['status']}): {why[:200]}")
    return "\n".join(lines) + "\n"


async def main_async(args: argparse.Namespace) -> int:
    tasks = select(load_tasks(), args)
    if not tasks:
        print("no tasks selected", file=sys.stderr)
        return 2
    if args.memory == "on":
        args.parallel = 1  # memory is shared across tasks: order must be deterministic
        import psycopg

        from aeos.memory.store import DDL as MEM_DDL

        with psycopg.connect(get_settings().database_url) as conn:
            conn.execute("DROP TABLE IF EXISTS aeos.episodes, aeos.facts")
            conn.execute(MEM_DDL)
    sem = asyncio.Semaphore(args.parallel)
    jobs = [run_one(t, trial, args, sem) for trial in range(args.trials) for t in tasks]
    print(f"running {len(jobs)} runs ({len(tasks)} tasks x {args.trials} trials), parallel={args.parallel}, "
          f"llm={args.llm}", flush=True)  # fmt: skip
    results: list[dict[str, Any]] = []
    for coro in asyncio.as_completed(jobs):
        r = await coro
        results.append(r)
        mark = "PASS" if r["success"] else "FAIL"
        print(f"  [{len(results)}/{len(jobs)}] {mark} {r['task_id']} ({r['status']}, {r['latency_s']}s, "
              f"{r.get('llm_calls', 0)} llm calls)", flush=True)  # fmt: skip
    results.sort(key=lambda r: (r["task_id"], r["trial"]))
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    label = args.label or f"{args.suite}-{args.llm}"
    result = {
        "meta": {"run_id": run_id, "label": label, "llm": args.llm, "scenario": args.scenario,
                 "break": args.break_, "memory": args.memory, "trials": args.trials,
                 "version": code_version(), "model": get_settings().gemini_model if args.llm == "gemini" else "scripted"},
        "aggregate": aggregate(results),
        "runs": results,
    }  # fmt: skip
    deltas = None
    if args.baseline:
        previous = json.loads(await asyncio.to_thread(Path(args.baseline).read_text))
        deltas = compare(result["aggregate"], previous["aggregate"])
        result["vs_baseline"] = {"file": args.baseline, "deltas": deltas}
    RESULTS.mkdir(exist_ok=True)
    stem = f"{run_id}_{label}".replace(" ", "-")
    (RESULTS / f"{stem}.json").write_text(json.dumps(result, indent=2, default=str))
    md = markdown(result, deltas)
    (RESULTS / f"{stem}.md").write_text(md)
    print("\n" + md)
    print(f"wrote evals/results/{stem}.json")
    ok = all(not d["regressed"] for d in deltas) if deltas else True
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--suite", default="smoke", help="smoke | full | comma-separated task ids")
    p.add_argument("--llm", choices=["fake", "gemini"], default="fake")
    p.add_argument(
        "--scenario", choices=["happy", "wrong_fix"], default="happy", help="fake-LLM trajectory"
    )
    p.add_argument("--break", dest="break_", choices=["none", "retry"], default="none",
                   help="deliberately degrade the config (proves the evals have signal)")  # fmt: skip
    p.add_argument("--memory", choices=["on", "off"], default="off")
    p.add_argument("--templates")
    p.add_argument("--limit", type=int)
    p.add_argument("--trials", type=int, default=1)
    p.add_argument("--parallel", type=int, default=3)
    p.add_argument("--task-timeout", type=float, default=900)
    p.add_argument("--label")
    p.add_argument("--baseline", help="previous results .json to compare against")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
