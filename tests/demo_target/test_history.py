import subprocess
import sys
from pathlib import Path

from demo_target.build_repo import build
from demo_target.history import BAD_COMMIT_KEY, COMMITS


def test_history_has_about_twenty_commits(commits: list[dict[str, str]]) -> None:
    assert len(commits) == len(COMMITS) == 20


def test_rebuild_gives_identical_shas(tmp_path: Path, commits: list[dict[str, str]]) -> None:
    again = build(tmp_path / "again")
    assert [c["sha"] for c in again] == [c["sha"] for c in commits]


def test_bad_commit_is_the_catalog_refactor(commits: list[dict[str, str]]) -> None:
    bad = next(c for c in commits if c["key"] == BAD_COMMIT_KEY)
    assert bad["subject"] == "refactor(catalog): simplify product lookup"


def test_service_tests_pass_at_head_and_at_bad_commit(tree, sha) -> None:  # type: ignore[no-untyped-def]
    # The regression must be invisible to the unit tests.
    for key in ("version", BAD_COMMIT_KEY):
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=tree(key),
            capture_output=True,
            text=True,
        )
        assert out.returncode == 0, out.stdout + out.stderr
