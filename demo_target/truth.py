"""Ground truth per template, MEASURED (never typed in): build the repo, benchmark the last good
commit and the regressed head, apply the reference fix, and record what actually happens."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from demo_target.build_repo import build_template, export_tree, template_paths
from demo_target.templates import TEMPLATES, Template

TRUTH_DIR = Path(__file__).resolve().parent / "truth"


def _benchmark(tree: Path) -> dict[str, Any]:
    out = subprocess.run([sys.executable, "benchmark.py", "--json"], cwd=tree, capture_output=True,
                         text=True, check=True)  # fmt: skip
    result: dict[str, Any] = json.loads(out.stdout)
    return result


def _tests_pass(tree: Path) -> bool:
    out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=tree,
                         capture_output=True, text=True)  # fmt: skip
    return out.returncode == 0


def compute_truth(t: Template) -> dict[str, Any]:
    info = build_template(t.id)
    sha = {c["key"]: c for c in info}
    repo, _, _ = template_paths(t.id)
    bad, good, head = sha[t.bad_key], sha[t.last_good_key], info[-1]
    with tempfile.TemporaryDirectory() as tmp:
        base_tree = export_tree(repo, good["sha"], Path(tmp) / "good")
        head_tree = export_tree(repo, head["sha"], Path(tmp) / "head")
        baseline, regressed = _benchmark(base_tree), _benchmark(head_tree)
        tests_head = _tests_pass(head_tree)
        for path, content in t.fix_files.items():  # the reference fix
            (head_tree / path).write_text(content)
        fixed = _benchmark(head_tree)
        tests_fixed = _tests_pass(head_tree)
    ratio = regressed["p95_ms"] / baseline["p95_ms"]
    assert ratio >= t.min_p95_ratio, f"{t.id}: regression only {ratio:.2f}x (< {t.min_p95_ratio})"
    assert tests_head, f"{t.id}: the regression must be invisible to the unit tests"
    if t.fix_files:
        assert tests_fixed and fixed["p95_ms"] <= baseline["p95_ms"] * 1.10, (
            f"{t.id}: reference fix is not a fix"
        )
    deploy_sha = {k: sha[k]["sha"] for k in sha}
    deploy_range = [
        c["sha"]
        for c in info
        if bad["date"] <= c["date"] <= head["date"] and c["date"] >= good["date"]
    ]
    return {
        "scenario_id": t.id,
        "service": "checkout-service",
        "title": t.title,
        "root_cause": {
            "commit_sha": bad["sha"],
            "commit_subject": bad["subject"],
            "file": t.root_cause_file,
            "summary": t.summary,
            "category": t.category,
            "introduced_in_deploy": "v1.2.0",
        },
        "last_good_commit_sha": good["sha"],
        "head_commit_sha": head["sha"],
        "deploy_range_commits": deploy_range,
        "red_herrings": [
            {
                "description": t.red_herring,
                "applies": t.red_herring_applies,
                "file": "checkout/audit.py",
                "commit_sha": sha["audit"]["sha"],
                "parent_commit_sha": sha["benchmark"]["sha"],
            }
        ],
        "acceptable_fixes": [{"id": i, "description": d} for i, d in t.acceptable_fixes],
        "reference_fix_files": sorted(t.fix_files),
        "expected_tools": list(t.expected_tools),
        "benchmark": {
            "command": "python benchmark.py --json",
            "baseline": baseline,
            "regressed": regressed,
            "reference_fix": fixed if t.fix_files else None,
            "p95_ratio_regressed_vs_baseline": round(ratio, 3),
            "pass_rule": "fixed p95_ms <= baseline p95_ms * 1.10",
        },
        "tests_must_pass": "python -m pytest -q",
        "deploy_commits": {k: deploy_sha[k] for k in deploy_sha if k in {d[1] for d in t.deploys}},
    }


def write_all() -> None:
    TRUTH_DIR.mkdir(exist_ok=True)
    for t in TEMPLATES.values():
        truth = compute_truth(t)
        (TRUTH_DIR / f"{t.id}.json").write_text(json.dumps(truth, indent=2) + "\n")
        b = truth["benchmark"]
        print(f"{t.id:26} p95 {b['baseline']['p95_ms']:>7} -> {b['regressed']['p95_ms']:>7}"
              f" ({b['p95_ratio_regressed_vs_baseline']}x)  fix -> {b['reference_fix']['p95_ms'] if b['reference_fix'] else '-'}")  # fmt: skip


if __name__ == "__main__":
    write_all()
