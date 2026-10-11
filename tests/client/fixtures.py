"""API payloads shaped exactly like GET /tasks/{id}, /approvals and /stats."""

import copy

PATCH = (
    "diff --git a/package.json b/package.json\n--- a/package.json\n+++ b/package.json\n"
    '@@ -1,3 +1,3 @@\n-    "ws": "^8.14.2",\n+    "ws": "^8.22.0",\n'
)
GATES = {
    "patch_present": True, "diff_size_ok": True, "paths_allowed": True, "no_secrets_in_diff": True,
    "patch_applies": True, "syntax_ok": True, "lockfile_consistent": True,
}  # fmt: skip

TASK = {
    "task_id": "t-1da798cf", "goal": "update the ws dependency and raise a PR", "kind": "change",
    "status": "completed", "status_line": "Task t-1da798cf [completed] plan v1: ✓ s1:research ✓ s2:coding",
    "error": None, "plan_version": 1, "pending_approval_id": None, "root_cause": None,
    "plans": [{
        "version": 1, "reason_for_change": None,
        "steps": [
            {"id": "s1", "owner": "research", "status": "done", "attempts": 1, "depends_on": [],
             "description": "Research the ws dependency"},
            {"id": "s2", "owner": "coding", "status": "done", "attempts": 1, "depends_on": ["s1"],
             "description": "Bump ws"},
            {"id": "v1", "owner": "verifier", "status": "done", "attempts": 1, "depends_on": ["s2"],
             "description": "Verify the patch"},
            {"id": "p1", "owner": "supervisor", "status": "done", "attempts": 1, "depends_on": ["v1"],
             "description": "Publish the PR"},
        ],
    }],
    "tool_calls": [
        {"id": "c1", "step_id": "s1", "agent": "research", "server": "github", "tool": "search_code",
         "arguments": {"pattern": "ws"}, "result_preview": '{"matches": []}', "is_error": False,
         "risk": "read", "origin": "trusted", "duration_ms": 12.5, "ts": 1.0},
        {"id": "c2", "step_id": "s1", "agent": "research", "server": "github", "tool": "get_file",
         "arguments": {"path": "x"}, "result_preview": "boom", "is_error": True,
         "risk": "read", "origin": "untrusted", "duration_ms": 3.0, "ts": 2.0},
    ],
    "observations": [{"id": "o1", "step_id": "s1", "agent": "research", "origin": "untrusted",
                      "summary": "ws is used in src/server.js", "data": {}}],
    "verifications": [{
        "status": "passed", "attempt": 1, "gates": GATES, "issues": [], "suggested_focus": [],
        "judgement": {"concerns": ["noisy diff"]},
        "metrics": {"tests": {"ran": False}, "files": [{"path": "package.json", "status": "modified", "added": 1, "removed": 1}]},
    }],
    "risk_decisions": [], "human_feedback": [],
    "pull_request": {"number": 1, "url": "https://github.com/o/r/pull/1", "title": "chore: bump ws"},
    "artifacts": {
        "patch": PATCH, "report": None, "baseline_ref": None, "rejected_root_causes": [],
        "research": {"summary": "ws declared in package.json", "current_state": "ws ^8.14.2",
                     "relevant_files": [{"path": "package.json", "why": "bump ws"}],
                     "risks": ["low"]},
    },
    "trace_id": "ab" * 16,
}  # fmt: skip

TASK_REPLANNED = copy.deepcopy(TASK)
TASK_REPLANNED["kind"] = "regression"
TASK_REPLANNED["plan_version"] = 2
TASK_REPLANNED["root_cause"] = {
    "file": "checkout/catalog.py",
    "commit_sha": "b166e05cbb" + "0" * 30,
    "confidence": 0.9,
    "summary": "N+1 queries",
    "evidence": ["queries x3"],
}
TASK_REPLANNED["artifacts"]["rejected_root_causes"] = [
    {"file": "checkout/audit.py", "summary": "red herring"}
]
TASK_REPLANNED["plans"].append(
    {
        "version": 2,
        "reason_for_change": "first fix did not restore performance",
        "steps": [
            {
                "id": "s4",
                "owner": "coding",
                "status": "running",
                "attempts": 2,
                "depends_on": [],
                "description": "Fix again",
            }
        ],
    }
)
TASK_REPLANNED["verifications"] = [
    {
        "status": "failed",
        "attempt": 1,
        "gates": {"tests_pass": True, "benchmark_not_regressed": False},
        "issues": [{"type": "perf_regression", "detail": "p95 +300%"}],
        "suggested_focus": ["check caching"],
        "judgement": None,
        "metrics": {
            "benchmark": {
                "baseline": {"p95_ms": 4.5, "db_queries": 520},
                "candidate": {"p95_ms": 18.0, "db_queries": 1900},
                "p95_change_pct": 300.0,
            }
        },
    }
]

APPROVAL = {
    "id": "ap-acb1222808", "task_id": "t-1da798cf", "tool": "create_pull_request", "status": "pending",
    "created_at": "2026-10-07T10:00:00Z", "expires_at": "2026-10-08T10:00:00Z", "actor": None,
    "rationale": "Open a pull request for: update ws",
    "arguments": {"title": "chore: bump ws", "head": "aeos/t-1da798cf-v1", "base": "main", "body": "b"},
    "evidence": {"gates": GATES, "patch": PATCH, "benchmark": None,
                 "root_cause": None, "research": {"summary": "ws declared in package.json"},
                 "files": [{"path": "package.json", "status": "modified", "added": 1, "removed": 1}]},
}  # fmt: skip

STATS = {
    "duration_s": 65.0, "llm": {"calls": 17, "tokens": 79090, "cost_usd": 0.0327},
    "tools": {"calls": 16, "errors": 0, "mean_ms": 120.0}, "replans": 0, "step_retries": 0,
    "verifications": {"total": 1, "failed": 0}, "approvals": {"total_wait_s": 9.0},
    "memory": {"retrieved": 0},
}  # fmt: skip
