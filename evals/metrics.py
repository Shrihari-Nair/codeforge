"""Aggregate per-run results into the metrics the roadmap asks for (mean +/- stderr over trials)."""

import math
from collections import defaultdict
from statistics import mean, pstdev
from typing import Any

METRICS = [
    ("success_rate", "Task success (outcome + root cause + policy)"),
    ("fix_rate", "Verified fix delivered (outcome + policy, root-cause commit not required)"),
    ("root_cause_accuracy", "Root cause correct"),
    ("tool_selection", "Tool-selection recall"),
    ("planning_accuracy", "Plan v1 sound and not revised"),
    ("recovery_rate", "Recovered after a failed attempt"),
    ("human_intervention_rate", "Needed a human (escalated)"),
    ("rubric_score", "LLM/heuristic rubric score"),
    ("avg_agent_steps", "Agent steps per task"),
    ("avg_llm_calls", "LLM calls per task"),
    ("avg_cost_usd", "Cost per task (USD)"),
    ("avg_latency_s", "Latency per task (s)"),
]


def _ms(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "stderr": 0.0, "n": 0}
    se = pstdev(values) / math.sqrt(len(values)) if len(values) > 1 else 0.0
    return {"mean": round(mean(values), 4), "stderr": round(se, 4), "n": len(values)}


def per_run_values(r: dict[str, Any]) -> dict[str, float | None]:
    g = r["grades"]
    failed_before = r["had_failure"]
    return {
        "success_rate": 1.0 if r["success"] else 0.0,
        "fix_rate": 1.0 if r.get("fix_success", r["success"]) else 0.0,
        "root_cause_accuracy": g["root_cause_match"]["score"],
        "tool_selection": g["tool_selection"]["score"],
        "planning_accuracy": g["planning"]["score"],
        "recovery_rate": (1.0 if r["success"] else 0.0) if failed_before else None,
        "human_intervention_rate": 1.0 if r["status"] == "escalated" else 0.0,
        "rubric_score": g["llm_rubric"]["score"],
        "avg_agent_steps": float(r["agent_steps"]),
        "avg_llm_calls": float(r["llm_calls"]),
        "avg_cost_usd": r["cost_usd"],
        "avg_latency_s": r["latency_s"],
    }


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Overall + per-template metrics. Trials of the same task are averaged first, so each task
    counts once and the stderr reflects task-level variation."""
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        by_task[r["task_id"]].append(r)

    def collapse(task_runs: list[dict[str, Any]]) -> dict[str, float | None]:
        vals = [per_run_values(r) for r in task_runs]
        out: dict[str, float | None] = {}
        for key, _ in METRICS:
            xs = [v[key] for v in vals if v[key] is not None]
            out[key] = mean(xs) if xs else None  # type: ignore[type-var]
        return out

    per_task = {tid: collapse(runs) for tid, runs in by_task.items()}
    template_of = {tid: runs[0]["template"] for tid, runs in by_task.items()}

    def summarise(task_ids: list[str]) -> dict[str, Any]:
        out: dict[str, Any] = {"tasks": len(task_ids)}
        for key, _ in METRICS:
            xs = [per_task[t][key] for t in task_ids if per_task[t][key] is not None]
            out[key] = _ms([float(x) for x in xs])  # type: ignore[arg-type]
        return out

    templates = sorted(set(template_of.values()))
    return {
        "overall": summarise(list(per_task)),
        "by_template": {
            t: summarise([i for i, tt in template_of.items() if tt == t]) for t in templates
        },
        "trials": len(results),
    }


def compare(
    current: dict[str, Any], previous: dict[str, Any], tolerance: float = 0.05
) -> list[dict[str, Any]]:
    """Per-metric delta vs a previous run; flags regressions beyond `tolerance` (higher-is-better
    metrics) or growth beyond 25% (cost/latency/steps)."""
    higher_better = {"success_rate", "fix_rate", "root_cause_accuracy", "tool_selection", "planning_accuracy",
                     "recovery_rate", "rubric_score"}  # fmt: skip
    rows = []
    for key, label in METRICS:
        if key not in previous["overall"] or key not in current["overall"]:
            continue  # a newer metric than the older results file: nothing to compare against
        cur, prev = current["overall"][key]["mean"], previous["overall"][key]["mean"]
        delta = cur - prev
        if key in higher_better:
            regressed = delta < -tolerance
        elif key == "human_intervention_rate":
            regressed = delta > tolerance
        else:
            regressed = prev > 0 and cur > prev * 1.25
        rows.append({"metric": key, "label": label, "previous": prev, "current": cur,
                     "delta": round(delta, 4), "regressed": regressed})  # fmt: skip
    return rows
