import json
import subprocess
import sys
from pathlib import Path

import pytest
from demo_target.build_repo import REPO_DIR, build, export_tree


@pytest.fixture(scope="session")
def commits() -> list[dict[str, str]]:
    return build()


@pytest.fixture(scope="session")
def sha(commits: list[dict[str, str]]) -> dict[str, str]:
    return {c["key"]: c["sha"] for c in commits}


@pytest.fixture(scope="session")
def tree(tmp_path_factory: pytest.TempPathFactory, sha: dict[str, str]):  # type: ignore[no-untyped-def]
    def _tree(key: str) -> Path:
        return export_tree(REPO_DIR, sha[key], tmp_path_factory.mktemp(key))

    return _tree


def run_benchmark(tree_dir: Path) -> dict[str, float]:
    out = subprocess.run(
        [sys.executable, "benchmark.py", "--json"],
        cwd=tree_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    result: dict[str, float] = json.loads(out.stdout)
    return result
