"""Golden scripted trajectories for the demo scenario (FakeLLM replay).

Used by CI (deterministic full-graph runs against the REAL tools), offline demo replay
(`aeos run --replay happy`), and smoke evals. Each role gets its own script so concurrent agents
stay deterministic.
"""

import json
from pathlib import Path
from typing import Any

from aeos.llm.base import LLMResponse, ToolCall, Usage
from aeos.llm.fake import FakeLLM, RoutedFakeLLM, ScriptStep

GROUND_TRUTH_PATH = Path("demo_target/GROUND_TRUTH.json")

PLANNER = "ROLE: supervisor-planner"
ANALYTICS = "ROLE: analytics"
RESEARCH = "ROLE: research"
SYNTHESIS = "ROLE: supervisor-synthesis"
CODING = "ROLE: coding"
JUDGE = "ROLE: verifier-judge"
REPLAN = "ROLE: supervisor-replan"
MEMORY = "ROLE: memory-curator"

GOOD_CATALOG = '''"""Product lookup: batched, cache-aside."""


def _key(product_id):
    return f"product:{product_id}"


def get_products(ctx, ids):
    found = {}
    missing = []
    for product_id in ids:
        cached = ctx.cache.get(_key(product_id))
        if cached is None:
            missing.append(product_id)
        else:
            found[product_id] = cached
    if missing:
        marks = ",".join("?" * len(missing))
        rows = ctx.db.query(
            f"SELECT id, name, price_cents FROM products WHERE id IN ({marks})", missing
        )
        for product_id, name, price_cents in rows:
            product = {"id": product_id, "name": name, "price_cents": price_cents}
            ctx.cache.set(_key(product_id), product)
            found[product_id] = product
    return [found[product_id] for product_id in ids]
'''


def turn(*calls: tuple[str, dict[str, Any]], tokens: int = 400) -> ScriptStep:
    tcs = [ToolCall(id=f"call-{i}-{n}", name=n, arguments=a) for i, (n, a) in enumerate(calls)]
    usage = Usage(input_tokens=tokens, output_tokens=tokens // 4)
    return ScriptStep(response=LLMResponse(tool_calls=tcs, usage=usage, model="scripted"))


def load_truth(path: Path = GROUND_TRUTH_PATH) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def planner_script() -> list[ScriptStep]:
    return [
        turn(
            (
                "submit",
                {
                    "rationale": "Investigate in parallel (what changed + which commit), then fix.",
                    "steps": [
                        {"id": "s1", "owner": "analytics",
                         "description": "Find when and why checkout latency regressed after the latest deployment."},
                        {"id": "s2", "owner": "research",
                         "description": "Inspect recent commits and code to find the responsible change."},
                        {"id": "s3", "owner": "coding", "depends_on": ["s1", "s2"],
                         "description": "Implement a fix in the sandbox and verify tests and benchmark."},
                    ],
                },
            )
        )
    ]  # fmt: skip


def analytics_script(truth: dict[str, Any]) -> list[ScriptStep]:
    sha = truth["deploy_range_commits"][-1]
    return [
        turn(("list_deployments", {"limit": 3})),
        turn(("get_metrics", {"start": "2026-03-10T08:00:00Z", "end": "2026-03-10T20:00:00Z",
                              "resolution_minutes": 60})),  # fmt: skip
        turn(("compare_time_windows", {
            "a_start": "2026-03-10T08:00:00Z", "a_end": "2026-03-10T14:00:00Z",
            "b_start": "2026-03-10T14:00:00Z", "b_end": "2026-03-10T20:00:00Z"})),  # fmt: skip
        turn(("search_logs", {"query": "slow query", "level": "WARNING", "limit": 3})),
        turn((
            "finish",
            {
                "summary": "p95 stepped from ~127ms to ~399ms at the v1.2.0 deploy (2026-03-10T14:00Z); "
                "db queries per request rose 2.0 -> 7.5 and cache hits fell to 0.",
                "change_started_at": "2026-03-10T14:00:00Z",
                "deployment_version": "v1.2.0",
                "deployment_commit_sha": sha,
                "metric_changes": [
                    {"metric": "p95_ms", "before": 127, "after": 399},
                    {"metric": "db_queries_per_request", "before": 2.0, "after": 7.5},
                    {"metric": "cache_hits_per_request", "before": 7.5, "after": 0.0},
                ],
                "candidates": [
                    {"description": "per-item product queries / lost caching in the checkout path",
                     "evidence": ["db_queries x3.7", "cache hits -> 0", "repeated products WHERE id = ? spans"],
                     "verdict": "likely_cause"},
                    {"description": "inventory_audit slow query",
                     "evidence": ["same rate before and after the deploy (720/12h each)"],
                     "verdict": "ruled_out"},
                ],
                "red_herrings": ["inventory_audit slow query (unchanged rate)",
                                 "cache backend timeout burst at 08:00 (unrelated, before deploy)"],
            },
        )),
    ]  # fmt: skip


def research_script(truth: dict[str, Any]) -> list[ScriptStep]:
    rc = truth["root_cause"]
    return [
        turn(("get_repository", {})),
        turn(("list_commits", {"path": rc["file"], "limit": 5})),
        turn(("get_diff", {"base": truth["last_good_commit_sha"], "head": rc["commit_sha"],
                           "path": rc["file"]})),  # fmt: skip
        turn((
            "finish",
            {
                "summary": rc.get("summary", "the regressing change"),
                "suspected_commit_sha": rc["commit_sha"],
                "suspected_file": rc["file"],
                "explanation": rc.get("summary", ""),
                "evidence": [f"diff of {rc['file']} in the suspected commit"],
                "confidence": 0.95,
                "last_good_commit_sha": truth["last_good_commit_sha"],
                "ruled_out_commits": [],
            },
        )),
    ]  # fmt: skip


def synthesis_script(truth: dict[str, Any]) -> list[ScriptStep]:
    rc = truth["root_cause"]
    return [
        turn((
            "submit",
            {
                "commit_sha": rc["commit_sha"],
                "file": rc["file"],
                "summary": rc.get("summary", "the regressing change"),
                "confidence": 0.95,
                "evidence": ["compare_time_windows: request profile changed at the deploy", "diff of the suspected commit"],
                "ruled_out": ["inventory_audit slow query (unchanged rate)"],
            },
        ))
    ]  # fmt: skip


def coding_script_good_fix(fix_files: dict[str, str] | None = None) -> list[ScriptStep]:
    """Read each file, write its fixed content, run tests, compare to baseline, finish."""
    files = fix_files or {"checkout/catalog.py": GOOD_CATALOG}
    edits: list[ScriptStep] = []
    for path, content in files.items():
        edits += [
            turn(("sandbox_read_file", {"path": path})),
            turn(("sandbox_write_file", {"path": path, "content": content})),
        ]
    return [
        *edits,
        turn(("sandbox_run_tests", {})),
        turn(("sandbox_compare_to_baseline", {})),
        turn((
            "finish",
            {
                "summary": "Restored the pre-regression implementation of the culprit module.",
                "approach": "revert the regressing change",
                "files_changed": sorted(files),
                "claims_tests_pass": True,
                "claims_benchmark_improved": True,
            },
        )),
    ]  # fmt: skip


def memory_script() -> list[ScriptStep]:
    """What a sensible curator proposes after the happy path (evidence = real trusted tool calls)."""
    return [
        turn((
            "submit",
            {
                "facts": [
                    {"subject": "checkout-service", "predicate": "product_lookup",
                     "value": "get_products must batch misses in one IN (...) query and use the cache",
                     "confidence": 0.9, "evidence": ["call-0-compare_time_windows"]},
                    {"subject": "checkout-service", "predicate": "known_red_herring",
                     "value": "inventory_audit slow-query warnings occur at a constant rate and do not affect p95",
                     "confidence": 0.85, "evidence": ["call-0-compare_time_windows"]},
                ],
                "lessons": ["compare equal windows before/after the deploy before blaming a slow query"],
            },
        ))
    ]  # fmt: skip


def happy_path_llm(
    truth: dict[str, Any] | None = None, fix_files: dict[str, str] | None = None
) -> RoutedFakeLLM:
    truth = truth or load_truth()
    return RoutedFakeLLM(
        {
            PLANNER: FakeLLM(planner_script()),
            ANALYTICS: FakeLLM(analytics_script(truth)),
            RESEARCH: FakeLLM(research_script(truth)),
            SYNTHESIS: FakeLLM(synthesis_script(truth)),
            CODING: FakeLLM(coding_script_good_fix(fix_files)),
            JUDGE: FakeLLM([judge_ok()]),
            MEMORY: FakeLLM(memory_script()),
        }
    )


# ------------------------------------------------------------------ wrong fix -> replan -> fix
def judge_ok(concern: str = "") -> ScriptStep:
    return turn(("submit", {"addresses_root_cause": True, "covers_requirements": True,
                            "concerns": [concern] if concern else [],
                            "rationale": "patch targets the diagnosed cause"}))  # fmt: skip


def analytics_script_misdiagnosis(truth: dict[str, Any]) -> list[ScriptStep]:
    """Plausible but wrong: fixates on the loud slow-query warning (a red herring)."""
    return [
        turn(("list_deployments", {"limit": 3})),
        turn(("search_logs", {"query": "slow query", "level": "WARNING", "limit": 3})),
        turn((
            "finish",
            {
                "summary": "Frequent 'slow query inventory_audit' warnings (~350ms) look like the source of the latency.",
                "change_started_at": "2026-03-10T14:00:00Z",
                "deployment_version": "v1.2.0",
                "deployment_commit_sha": truth["deploy_range_commits"][-1],
                "metric_changes": [{"metric": "p99_ms", "before": 463, "after": 650}],
                "candidates": [{"description": "inventory_audit slow query",
                                "evidence": ["many slow-query warnings"], "verdict": "likely_cause"}],
                "red_herrings": [],
            },
        )),
    ]  # fmt: skip


def research_script_misdiagnosis(truth: dict[str, Any]) -> list[ScriptStep]:
    rh = truth["red_herrings"][0]
    return [
        turn(("get_repository", {})),
        turn(("list_commits", {"path": rh["file"], "limit": 5})),
        turn((
            "finish",
            {
                "summary": "The audit scan in checkout/audit.py is a full-table scan run every 25th request.",
                "suspected_commit_sha": rh["commit_sha"],
                "suspected_file": rh["file"],
                "explanation": "inventory_audit scans the products table without an index.",
                "evidence": ["AUDIT_SCAN_MS cost on every 25th request"],
                "confidence": 0.7,
                "last_good_commit_sha": rh["parent_commit_sha"],
                "ruled_out_commits": [],
            },
        )),
    ]  # fmt: skip


def synthesis_script_misdiagnosis(truth: dict[str, Any]) -> list[ScriptStep]:
    rh = truth["red_herrings"][0]
    return [
        turn((
            "submit",
            {
                "commit_sha": rh["commit_sha"],
                "file": rh["file"],
                "summary": "The periodic inventory audit scan is slowing checkout.",
                "confidence": 0.7,
                "evidence": ["slow query warnings"],
                "ruled_out": [],
            },
        ))
    ]  # fmt: skip


def coding_script_wrong_fix() -> list[ScriptStep]:
    return [
        turn(("sandbox_read_file", {"path": "checkout/audit.py"})),
        turn(("sandbox_edit_file", {"path": "checkout/audit.py", "old": "AUDIT_EVERY = 25",
                                    "new": "AUDIT_EVERY = 1000"})),  # fmt: skip
        turn(("sandbox_run_tests", {})),
        turn((
            "finish",
            {
                "summary": "Reduced how often the inventory audit scan runs (every 1000th request).",
                "approach": "make the slow audit rarer",
                "files_changed": ["checkout/audit.py"],
                "claims_tests_pass": True,
                "claims_benchmark_improved": True,
            },
        )),
    ]  # fmt: skip


def replan_script() -> list[ScriptStep]:
    return [
        turn((
            "submit",
            {
                "reason": "Verifier measured p95 still ~6x the baseline with 7x the DB queries: the audit "
                "scan was a red herring. Re-investigate per-request database queries.",
                "steps": [
                    {"id": "s4", "owner": "research",
                     "description": "Re-investigate why DB queries per request rose ~7x after the deploy "
                     "(look at the checkout hot path, not the audit scan)."},
                    {"id": "s5", "owner": "coding", "depends_on": ["s4"],
                     "description": "Fix the actual cause and benchmark against the baseline."},
                ],
            },
        ))
    ]  # fmt: skip


def wrong_fix_then_fix_llm(
    truth: dict[str, Any] | None = None, fix_files: dict[str, str] | None = None
) -> RoutedFakeLLM:
    truth = truth or load_truth()
    return RoutedFakeLLM(
        {
            PLANNER: FakeLLM(planner_script()),
            ANALYTICS: FakeLLM(analytics_script_misdiagnosis(truth)),
            RESEARCH: FakeLLM(research_script_misdiagnosis(truth) + research_script(truth)),
            SYNTHESIS: FakeLLM(synthesis_script_misdiagnosis(truth) + synthesis_script(truth)),
            CODING: FakeLLM(coding_script_wrong_fix() + coding_script_good_fix(fix_files)),
            REPLAN: FakeLLM(replan_script()),
            JUDGE: FakeLLM([judge_ok(), judge_ok()]),
            MEMORY: FakeLLM(memory_script()),
        }
    )


SCENARIOS = {"happy": happy_path_llm, "wrong_fix": wrong_fix_then_fix_llm}
