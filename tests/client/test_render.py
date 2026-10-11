import io

from rich.console import Console

from aeos.client import render
from tests.client.fixtures import APPROVAL, STATS, TASK, TASK_REPLANNED


def text(renderable, width: int = 100) -> str:  # type: ignore[no-untyped-def]
    buf = io.StringIO()
    Console(file=buf, width=width, force_terminal=False, color_system=None).print(renderable)
    return buf.getvalue()


def test_header_shows_id_status_kind_goal_and_trace_link() -> None:
    out = text(render.header(TASK, langfuse_url="http://localhost:3000"))
    assert "t-1da798cf" in out and "completed" in out and "change request" in out
    assert "http://localhost:3000/traces/" + "ab" * 16 in out


def test_plan_groups_steps_by_dependency_depth_and_marks_status() -> None:
    out = text(render.plan_view(TASK), width=200)
    assert (
        out.index("research")
        < out.index("coding")
        < out.index("verifier")
        < out.index("supervisor")
    )
    assert "✓" in out and "after s1" in out


def test_plan_version_selection_and_replan_reason() -> None:
    out = text(render.plan_view(TASK_REPLANNED, version=2))
    assert "v2" in out and "Replanned because" in out
    assert "Replanned because" not in text(render.plan_view(TASK_REPLANNED, version=1))


def test_step_view_lists_observations_with_trust_and_tool_calls() -> None:
    out = text(render.step_view(TASK, "s1"))
    assert "github.search_code" in out and "untrusted" in out and "ms" in out
    assert "boom" in out


def test_verification_shows_gates_issues_and_benchmark() -> None:
    out = text(render.verification_panel(TASK_REPLANNED))
    assert "✗" in out and "✓" in out and "perf_regression" in out and "4.5" in out and "18.0" in out
    assert "no automated test suite" in text(render.verification_panel(TASK))


def test_diff_view_colours_added_and_removed_lines() -> None:
    d = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new\n"
    buf = io.StringIO()
    Console(file=buf, width=80, force_terminal=True, color_system="standard").print(
        render.diff_view(d)
    )
    assert "\x1b[" in buf.getvalue() and "old" in buf.getvalue() and "new" in buf.getvalue()


def test_approval_panel_has_gates_title_branch_and_optional_diff() -> None:
    short = text(render.approval_panel(APPROVAL))
    assert "ap-" in short and "chore: bump ws" in short and "aeos/t-" in short
    assert "patch_present" in short and "diff --git" not in short
    assert "diff --git" in text(render.approval_panel(APPROVAL, with_diff=True))


def test_stats_table_has_cost_tokens_and_tool_counts() -> None:
    out = text(render.stats_table(STATS))
    assert "$0.0327" in out and "79,090" in out


def test_untrusted_text_is_inert() -> None:
    evil = {**TASK, "goal": "\x1b[2Jwipe [bold red]x[/] \x1b]0;t\x07"}
    out = text(render.header(evil))
    assert "\x1b" not in out and "[bold red]x[/]" in out


def test_narrow_terminal_does_not_crash() -> None:
    for fn in (render.header, render.plan_view, render.verification_panel, render.stats_table):
        assert text(fn(STATS if fn is render.stats_table else TASK), width=40)  # type: ignore[operator]


def test_render_task_sections_and_empty_task() -> None:
    full = text(render.render_task(TASK, STATS, "http://x"))
    for needle in ("PLAN", "RESEARCH FINDINGS", "VERIFICATION", "PATCH", "PULL REQUEST", "STATS"):
        assert needle in full
    only = text(render.render_task(TASK, STATS, sections={"stats"}))
    assert "STATS" in only and "PLAN" not in only
    assert text(
        render.render_task({"task_id": "t-9", "goal": "g", "status": "queued", "kind": "change"})
    )


def test_tasks_and_approvals_tables() -> None:
    row = {"task_id": "t-1", "goal": "g", "status": "paused", "attempts": 1, "kind": "change",
           "created_at": "2026-10-07T10:00:00Z", "last_error": None}  # fmt: skip
    assert "t-1" in text(render.tasks_table([row]))
    assert "ap-acb1222808" in text(render.approvals_table([APPROVAL]))


def test_event_line() -> None:
    e = {
        "id": 1,
        "ts": "2026-10-07T10:00:05Z",
        "type": "plan_created",
        "agent": "supervisor",
        "message": "plan v1",
    }
    line = text(render.event_line(e))
    assert "supervisor" in line and "plan_created" in line and "plan v1" in line
