"""Read-only view of eval results (`evals/results/*.json`) for the dashboard.

The files are written by `python -m evals.run_evals`. Nothing here writes or runs evals; it only
lists and serves what is on disk, so a bad file can never take the API down.
"""

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

# <UTC timestamp>_<label>, e.g. 20261003T024031Z_live-gemini-full. Anything else is a 404, which
# also rules out path traversal.
_RUN_ID = re.compile(r"^\d{8}T\d{6}Z_[A-Za-z0-9._-]{1,80}$")
_ROW_FIELDS = (
    "task_id", "template", "status", "success", "latency_s", "cost_usd", "llm_calls", "plans",
    "verifications", "had_failure",
)  # fmt: skip


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def evals_router(results_dir: Path) -> APIRouter:
    router = APIRouter(prefix="/evals", tags=["evals"])

    @router.get("")
    def list_runs() -> list[dict[str, Any]]:
        """Every run, newest first, with its headline numbers."""
        out: list[dict[str, Any]] = []
        for path in sorted(results_dir.glob("*.json"), reverse=True):
            data = _read(path)
            if data is None or not _RUN_ID.match(path.stem):
                continue
            out.append(
                {
                    "id": path.stem,
                    "meta": data.get("meta", {}),
                    "overall": (data.get("aggregate") or {}).get("overall"),
                    "compared_to_baseline": bool(data.get("vs_baseline")),
                }
            )
        return out

    @router.get("/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        if not _RUN_ID.match(run_id):
            raise HTTPException(404, "no such eval run")
        data = _read(results_dir / f"{run_id}.json")
        if data is None:
            raise HTTPException(404, "no such eval run")
        return {
            "id": run_id,
            "meta": data.get("meta", {}),
            "aggregate": data.get("aggregate", {}),
            "vs_baseline": data.get("vs_baseline"),
            "runs": [{k: r.get(k) for k in _ROW_FIELDS} for r in data.get("runs", [])],
        }

    return router
