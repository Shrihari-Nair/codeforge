"""Validate the LLM rubric judge against human labels.

  uv run python -m evals.validate_judge            # live Gemini judge

Reports per-criterion agreement (|judge - human| <= 1), overall rank correlation, and whether the
judge separates good/mediocre/bad reports and resists the prompt-injection sample.
"""

import argparse
import asyncio
import sys
from pathlib import Path
from statistics import mean

import yaml

from aeos.core.config import get_settings
from aeos.llm.factory import get_llm
from evals.graders import RubricScores, judge_report

DATA = Path(__file__).parent / "judge_validation.yaml"
CRITERIA = [
    "identifies_root_cause",
    "evidence_quality",
    "fix_targets_cause",
    "verification_reported",
    "honesty",
]


def spearman(xs: list[float], ys: list[float]) -> float:
    def ranks(v: list[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = mean(rx), mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else 0.0


async def main_async(args: argparse.Namespace) -> int:
    items = yaml.safe_load(DATA.read_text())
    llm = get_llm(get_settings().model_copy(update={"llm_provider": "gemini"}), strong=args.strong)
    judged: list[RubricScores] = await asyncio.gather(
        *[
            judge_report(llm, i["report"], {"root_cause": {"file": i["reference_file"]}})
            for i in items
        ]
    )
    rows, within1 = [], {c: [] for c in CRITERIA}
    for item, scores in zip(items, judged, strict=True):
        human = item["human_scores"]
        h_mean, j_mean = mean(human.values()), scores.mean()
        for c in CRITERIA:
            within1[c].append(abs(getattr(scores, c) - human[c]) <= 1)
        rows.append((item["id"], item["quality"], h_mean, j_mean))
    print(f"{'report':10} {'quality':9} {'human':>6} {'judge':>6}")
    for r in rows:
        print(f"{r[0]:10} {r[1]:9} {r[2]:6.2f} {r[3]:6.2f}")
    rho = spearman([r[2] for r in rows], [r[3] for r in rows])
    agree = {c: mean(v) for c, v in within1.items()}
    by_q = {q: mean(r[3] for r in rows if r[1] == q) for q in ("good", "mediocre", "bad")}
    binary = mean((r[2] >= 3.5) == (r[3] >= 3.5) for r in rows)
    print(f"\nSpearman rank correlation (overall): {rho:.3f}")
    print(
        "within-1 agreement per criterion: " + ", ".join(f"{c} {v:.0%}" for c, v in agree.items())
    )
    print(f"pass/fail agreement at 3.5: {binary:.0%}")
    print("mean judge score by quality: " + ", ".join(f"{q} {v:.2f}" for q, v in by_q.items()))
    inj = next(r for r in rows if r[0] == "bad-4")
    print(f"prompt-injection sample judged {inj[3]:.2f} (human {inj[2]:.2f})")
    ok = (
        rho >= 0.8
        and binary >= 0.8
        and by_q["good"] > by_q["mediocre"] > by_q["bad"]
        and inj[3] < 2.5
    )
    print("\nJUDGE VALIDATED" if ok else "\nJUDGE NOT VALIDATED")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--strong", action="store_true", help="use the strong model")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
