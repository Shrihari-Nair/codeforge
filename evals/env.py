"""Isolated environment for one eval run: own telemetry schema, own git remote, ground truth."""

import asyncio
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import yaml
from demo_target.build_repo import build_template, template_paths
from demo_target.datagen import generate
from demo_target.seed import load
from demo_target.templates import TEMPLATES, Template
from demo_target.truth import TRUTH_DIR

from aeos.core.config import Settings, get_settings

DATASET = Path(__file__).parent / "datasets" / "tasks.yaml"


def load_tasks(path: Path = DATASET) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = yaml.safe_load(path.read_text())
    return tasks


@dataclass
class Env:
    task: dict[str, Any]
    template: Template
    truth: dict[str, Any]
    schema: str
    remote_path: str
    work_dir: Path

    def settings(self, base: Settings | None = None, **overrides: Any) -> Settings:
        return (base or get_settings()).model_copy(
            update={"git_remote_path": self.remote_path, "demo_schema": self.schema, **overrides}
        )

    def cleanup(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)


def schema_name(template: str, seed: int) -> str:
    return f"ev_{template}_s{seed}"


def _ensure_schema_seeded(db_url: str, template: Template, seed: int, schema: str) -> None:
    info = build_template(template.id)
    with psycopg.connect(db_url) as conn:
        exists = conn.execute("SELECT to_regclass(%s)", (f"{schema}.metrics",)).fetchone()
        n = 0
        if exists and exists[0]:
            row = conn.execute(f"SELECT count(*) FROM {schema}.metrics").fetchone()  # noqa: S608
            n = row[0] if row else 0
        if n != 1440:  # seeded exactly once per (template, seed); reused by every later run
            load(conn, generate(info, seed=seed, template=template), schema)


def load_truth(template_id: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((TRUTH_DIR / f"{template_id}.json").read_text())
    return data


async def prepare(task: dict[str, Any], base: Settings | None = None) -> Env:
    base = base or get_settings()
    tid, seed = task["environment"]["template"], int(task["environment"]["seed"])
    template = TEMPLATES[tid]
    schema = schema_name(tid, seed)
    await asyncio.to_thread(_ensure_schema_seeded, base.database_url, template, seed, schema)
    _, remote_src, _ = template_paths(tid)
    work = Path(tempfile.mkdtemp(prefix=f"aeos-eval-{tid}-"))
    await asyncio.to_thread(shutil.copytree, remote_src, work / "remote.git")
    return Env(task, template, load_truth(tid), schema, str(work / "remote.git"), work)
