# Terminal Client (full dashboard parity) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Everything the localhost dashboard does (submit, watch live, inspect, approve/reject, stats, links) is available from the terminal, through one interactive `aeos chat` session and equivalent one-shot commands.

**Architecture:** A new `aeos.client` package is a thin terminal client of the existing REST + SSE API (`/tasks`, `/approvals`, `/tasks/{id}/events/stream`, `/meta`). The API stays the single source of truth (queue, checkpoints, worker), so the terminal and the browser always show the same state and can be used at the same time. Rendering is pure functions that turn API JSON into `rich` renderables, so every view is unit-testable without a server.

**Tech Stack:** Python 3.12, `rich` (new dependency), `httpx` (already used), argparse (existing `aeos.cli`), pytest. No full-screen TUI framework (see Decisions).

**Spec:** the dashboard in `web/` (`web/app/page.tsx`, `web/app/tasks/[id]/page.tsx`, `web/app/approvals/page.tsx`, `web/components/*`), the API in `src/aeos/api/app.py`, and `docs/ARCHITECTURE.md`.

## Global Constraints

- The client talks **only** to the HTTP API. It never imports the graph, runtime, DB stores or sandbox (so it also works from another machine and inside the `api` container).
- API base URL: env `AEOS_API_URL`, default `http://localhost:8800`; global flag `--api URL` overrides it. Inside the compose `api` container it is `http://localhost:8000`.
- Actor name for approvals: `--actor`, else env `AEOS_ACTOR`, else `$USER`, else `cli-user`. Sent as the `X-Actor` header (same as the dashboard).
- **Untrusted text is never interpreted:** all text that originates from agents, tools, goals, diffs or errors is passed through `sanitize.clean()` (strips ANSI/OSC/control characters) and rendered with `rich.text.Text` (never `markup=True`).
- Non-interactive (piped / not a TTY) runs must never block on a prompt and must produce plain, parseable output.
- Ctrl+C while watching **detaches**; it never cancels the task.
- Existing commands and their tests keep working: `aeos run`, `serve`, `worker`, `agent`, and the direct-DB behaviour of `submit/status/approvals/approve/reject` remain available behind `--direct`.
- No secret may appear in output or tests (`make secrets` must stay clean).
- Run `make lint`, `make typecheck`, `make secrets` after every task. Do **not** run the full test suite while the dashboard stack is in use (some tests clear the task queue); run only the `tests/client` files named in each task.
- Version control is the owner's: this plan has no commit steps.

## Decisions (with the recommended choice)

| Question | Choice | Why |
|---|---|---|
| Client of API vs direct DB | **API** | Same state as the dashboard, works remotely, no DB credentials on the client, reuses SSE resume |
| Rich line UI vs full-screen TUI (Textual) | **Rich line UI** | Streams naturally, copy/paste friendly, works over ssh and in logs, far less code. A Textual TUI can be layered later on the same `api` and `render` modules |
| Keep legacy direct-DB commands | **Yes, behind `--direct`** | Existing tests and the chaos harness rely on them |

## Parity matrix (dashboard → terminal)

| Dashboard feature | Terminal equivalent |
|---|---|
| Tasks list, auto-refresh | `aeos tasks [--watch]`, `/tasks` in chat |
| New task form (target + goal) | `aeos new "goal" --kind change\|regression [--watch]`; plain text in chat; `/target` |
| Task header: id, status, kind, status line, error | `aeos show <id>` header, always shown first |
| Plan graph, plan version tabs, replan reason | `aeos show <id> --plan [--version N]` (columns by dependency depth) |
| Step drill-down: observations (trust origin), tool calls (args, result, risk, duration) | `aeos show <id> --step s1` |
| Research findings (change tasks) | `aeos show <id> --findings` |
| Root cause + rejected earlier diagnoses | `aeos show <id> --root-cause` |
| Verification: gates, benchmark, issues, judge | `aeos show <id> --verify` |
| Patch diff viewer | `aeos show <id> --diff` (coloured, pager for long output) |
| PR link, decided approvals, human feedback | `aeos show <id>` footer sections |
| Stats panel | `aeos stats <id>`, section of `show` |
| Live events timeline (SSE, resumes after reconnect) | `aeos watch <id>`, `aeos events <id> [--follow]` |
| Approval card + Approve/Reject + actor + feedback | inline prompt in `watch`/`chat`, `aeos approve\|reject <approval-id>` |
| Approvals inbox | `aeos approvals`, `/approvals` in chat |
| Langfuse trace link | `aeos open <id>` prints trace + PR URLs; shown in `show` header |
| Remembered actor name | `AEOS_ACTOR` / `--actor`, `/actor` in chat |

## Review Focus

Inputs and conditions the spec implies but no feature task names; each line has a pinned test in the owning task.

1. **Terminal-escape injection:** agent/tool output containing ANSI/OSC sequences or `[red]`-style markup must be shown as inert text (Task 1, Task 2).
2. **Piped / non-TTY stdin and stdout:** no prompts, no live redraws, exit code reflects task outcome (Task 3, Task 4).
3. **API down or wrong URL:** one clear message ("cannot reach the AEOS API at … is `docker compose up` running?"), exit code 2, no traceback (Task 1, Task 4).
4. **Approval already decided elsewhere (browser) or expired:** HTTP 409/410 from the API is reported in words and the view refreshes instead of crashing (Task 3).
5. **SSE drop mid-run and very long diffs or narrow terminals:** the stream resumes from the last event id without duplicates; long diffs go through a pager; panels do not crash at width 40 (Task 1, Task 2, Task 3).

---

### Task 1: Foundations: dependency, sanitizer, API client with SSE

**Files:**
- Modify: `pyproject.toml` (add `"rich>=13.7"` to `dependencies`), then `uv lock`
- Create: `src/aeos/client/__init__.py` (empty)
- Create: `src/aeos/client/sanitize.py`
- Create: `src/aeos/client/api.py`
- Test: `tests/client/__init__.py` (empty), `tests/client/test_sanitize.py`, `tests/client/test_api.py`

**Interfaces:**
- Produces: `sanitize.clean(text: str) -> str`; `sanitize.safe(text: str, style: str | None = None) -> rich.text.Text`
- Produces: `api.AeosClient(base_url: str, *, timeout: float = 15.0, transport: httpx.BaseTransport | None = None)` with `meta() -> dict`, `tasks() -> list[dict]`, `task(id) -> dict`, `stats(id) -> dict`, `events(id, after=0) -> list[dict]`, `stream_events(id, after=0, *, max_retries=5) -> Iterator[dict]`, `approvals(status="pending", task_id=None) -> list[dict]`, `create_task(goal, kind=None) -> str`, `decide(approval_id, decision, actor, feedback=None) -> dict`
- Produces: `api.ApiError(status: int, detail: str)`, `api.ApiUnreachable(url: str)`, `api.default_api_url() -> str`, `api.default_actor() -> str`

- [ ] **Step 1: Write the failing sanitizer tests**

```python
# tests/client/test_sanitize.py
from aeos.client.sanitize import clean, safe


def test_ansi_and_osc_sequences_are_removed() -> None:
    evil = "ok\x1b[31mRED\x1b[0m \x1b]0;pwned title\x07done \x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\"
    assert clean(evil) == "okRED done link"


def test_control_characters_are_removed_but_newline_and_tab_kept() -> None:
    assert clean("a\x00b\x07c\x08d\te\nf\r\ng") == "abcd\te\nf\ng"


def test_rich_markup_is_not_interpreted() -> None:
    t = safe("[bold red]hello[/] [link=http://evil]x[/link]")
    assert t.plain == "[bold red]hello[/] [link=http://evil]x[/link]"
    assert not t.spans


def test_none_and_non_strings_become_text() -> None:
    assert clean(None) == "" and clean(42) == "42"  # type: ignore[arg-type]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/client/test_sanitize.py -q`
Expected: FAIL `ModuleNotFoundError: aeos.client`

- [ ] **Step 3: Implement the sanitizer**

```python
# src/aeos/client/sanitize.py
"""Everything an agent, tool or repo can influence is untrusted terminal input.

`clean` removes escape sequences and control characters; `safe` wraps the result in a rich `Text`
so `[markup]` inside it is never interpreted.
"""

import re

from rich.text import Text

_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ESC_OTHER = re.compile(r"\x1b[@-Z\\-_]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # keeps \t (09) and \n (0a)


def clean(text: object) -> str:
    s = "" if text is None else str(text)
    s = _OSC.sub("", s)
    s = _CSI.sub("", s)
    s = _ESC_OTHER.sub("", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL.sub("", s)


def safe(text: object, style: str | None = None) -> Text:
    return Text(clean(text), style=style or "")
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/client/test_sanitize.py -q`
Expected: `4 passed`

- [ ] **Step 5: Write the failing API client tests**

```python
# tests/client/test_api.py
import json

import httpx
import pytest

from aeos.client.api import AeosClient, ApiError, ApiUnreachable


def client(handler) -> AeosClient:  # type: ignore[no-untyped-def]
    return AeosClient("http://aeos", transport=httpx.MockTransport(handler))


def test_create_task_sends_goal_and_kind() -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(202, json={"task_id": "t-1"})

    assert client(handler).create_task("bump ws", "change") == "t-1"
    assert seen["body"] == {"goal": "bump ws", "kind": "change"}


def test_decide_sends_actor_header_and_feedback() -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(path=req.url.path, actor=req.headers["x-actor"], body=json.loads(req.content))
        return httpx.Response(200, json={"status": "resuming"})

    client(handler).decide("ap-1", "reject", "alice", "too big")
    assert seen == {"path": "/approvals/ap-1/reject", "actor": "alice", "body": {"feedback": "too big"}}


@pytest.mark.parametrize("status,detail", [(409, "already decided"), (410, "expired"), (404, "no task")])
def test_http_errors_become_api_error_with_detail(status: int, detail: str) -> None:
    c = client(lambda r: httpx.Response(status, json={"detail": detail}))
    with pytest.raises(ApiError) as ei:
        c.task("t-x")
    assert ei.value.status == status and detail in str(ei.value)


def test_unreachable_api_is_a_dedicated_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ApiUnreachable) as ei:
        client(handler).meta()
    assert "http://aeos" in str(ei.value)


def sse(*events: dict) -> bytes:  # type: ignore[type-arg]
    return b"".join(
        f"id: {e['id']}\nevent: aeos\ndata: {json.dumps(e)}\n\n".encode() for e in events
    ) + b": keep-alive\n\n"


def test_stream_parses_events_and_skips_keepalives() -> None:
    evs = [{"id": 1, "type": "a"}, {"id": 2, "type": "b"}]
    c = client(lambda r: httpx.Response(200, content=sse(*evs)))
    got = []
    for e in c.stream_events("t-1", max_retries=0):
        got.append(e["id"])
    assert got == [1, 2]


def test_stream_resumes_from_last_id_after_a_dropped_connection() -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url.params.get("after")))
        if len(calls) == 1:
            return httpx.Response(200, content=sse({"id": 1, "type": "a"}, {"id": 2, "type": "b"}),
                                  headers={"x-drop": "1"})
        return httpx.Response(200, content=sse({"id": 3, "type": "c"}))

    c = client(handler)
    it = c.stream_events("t-1", max_retries=1, stop_when=lambda e: e["id"] == 3)
    assert [e["id"] for e in it] == [1, 2, 3]
    assert calls == ["0", "2"]  # resumed after event 2, no duplicates
```

- [ ] **Step 6: Run to verify it fails**

Run: `uv run pytest tests/client/test_api.py -q`
Expected: FAIL `ModuleNotFoundError: aeos.client.api`

- [ ] **Step 7: Implement the client**

```python
# src/aeos/client/api.py
"""Synchronous HTTP client for the AEOS API (REST + Server-Sent Events)."""

import getpass
import json
import os
from collections.abc import Callable, Iterator
from typing import Any

import httpx


class ApiError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status, self.detail = status, detail


class ApiUnreachable(Exception):
    def __init__(self, url: str) -> None:
        super().__init__(
            f"cannot reach the AEOS API at {url}. Is `docker compose up -d` running? "
            "(set AEOS_API_URL or pass --api to point elsewhere)"
        )
        self.url = url


def default_api_url() -> str:
    return os.environ.get("AEOS_API_URL", "http://localhost:8800")


def default_actor() -> str:
    return os.environ.get("AEOS_ACTOR") or os.environ.get("USER") or _safe_user()


def _safe_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "cli-user"


class AeosClient:
    def __init__(
        self, base_url: str, *, timeout: float = 15.0, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)
        self._transport = transport

    # ------------------------------------------------------------------ plumbing
    def _request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = self._http.request(method, path, **kw)
        except httpx.TransportError as exc:
            raise ApiUnreachable(self.base_url) from exc
        if r.status_code >= 400:
            try:
                detail = str(r.json().get("detail", r.reason_phrase))
            except Exception:  # noqa: BLE001
                detail = r.text[:200] or r.reason_phrase
            raise ApiError(r.status_code, detail)
        return r.json()

    # ------------------------------------------------------------------ reads
    def meta(self) -> dict[str, Any]:
        return self._request("GET", "/meta")  # type: ignore[no-any-return]

    def tasks(self) -> list[dict[str, Any]]:
        return self._request("GET", "/tasks")  # type: ignore[no-any-return]

    def task(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/tasks/{task_id}")  # type: ignore[no-any-return]

    def stats(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/tasks/{task_id}/stats")  # type: ignore[no-any-return]

    def events(self, task_id: str, after: int = 0) -> list[dict[str, Any]]:
        return self._request("GET", f"/tasks/{task_id}/events", params={"after": after})  # type: ignore[no-any-return]

    def approvals(self, status: str | None = "pending", task_id: str | None = None) -> list[dict[str, Any]]:
        params = {"status": status or "", **({"task_id": task_id} if task_id else {})}
        return self._request("GET", "/approvals", params=params)  # type: ignore[no-any-return]

    # ------------------------------------------------------------------ writes
    def create_task(self, goal: str, kind: str | None = None) -> str:
        body: dict[str, Any] = {"goal": goal, **({"kind": kind} if kind else {})}
        return str(self._request("POST", "/tasks", json=body)["task_id"])

    def decide(self, approval_id: str, decision: str, actor: str, feedback: str | None = None) -> dict[str, Any]:
        assert decision in ("approve", "reject")
        return self._request(  # type: ignore[no-any-return]
            "POST", f"/approvals/{approval_id}/{decision}",
            headers={"X-Actor": actor}, json={"feedback": feedback or None},
        )

    # ------------------------------------------------------------------ SSE
    def stream_events(
        self,
        task_id: str,
        after: int = 0,
        *,
        max_retries: int = 5,
        stop_when: Callable[[dict[str, Any]], bool] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield events after `after`, following live. Reconnects from the last id on drops."""
        last, retries = after, 0
        while True:
            try:
                with self._http.stream(
                    "GET", f"/tasks/{task_id}/events/stream", params={"after": last},
                    timeout=httpx.Timeout(15.0, read=None),
                ) as r:
                    if r.status_code >= 400:
                        raise ApiError(r.status_code, r.read().decode(errors="replace")[:200])
                    retries = 0
                    for line in r.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        ev = json.loads(line[5:].strip())
                        if ev["id"] <= last:
                            continue
                        last = ev["id"]
                        yield ev
                        if stop_when and stop_when(ev):
                            return
            except httpx.TransportError as exc:
                retries += 1
                if retries > max_retries:
                    raise ApiUnreachable(self.base_url) from exc
                continue
            if max_retries == 0 or (stop_when is None and retries >= max_retries):
                return
            retries += 1
            if retries > max_retries:
                return
```

- [ ] **Step 8: Run to verify it passes**

Run: `uv run pytest tests/client/test_api.py tests/client/test_sanitize.py -q`
Expected: `all passed`. (If the resume test's reconnect accounting is off, adjust the loop's exit rule only; the test defines the required behaviour: resume with `after=<last id>`, no duplicates, stop at `stop_when`.)

- [ ] **Step 9: Lint, types, secrets**

Run: `uv run ruff check src/aeos/client tests/client && uv run ruff format src/aeos/client tests/client && uv run mypy src && uv run python scripts/check_no_secrets.py`
Expected: all clean

---

### Task 2: Renderers: every dashboard view as a pure function

**Files:**
- Create: `src/aeos/client/render.py`
- Test: `tests/client/test_render.py`, fixture `tests/client/fixtures.py`

**Interfaces:**
- Consumes: `sanitize.safe`, `sanitize.clean` (Task 1); task/approval/stats JSON exactly as returned by `GET /tasks/{id}`, `/approvals`, `/stats`
- Produces (all return `rich.console.RenderableType`; none print):
  `tasks_table(rows)`, `header(task, langfuse_url=None)`, `plan_view(task, version=None)`, `step_view(task, step_id)`, `findings_panel(task)`, `root_cause_panel(task)`, `verification_panel(task)`, `diff_view(patch)`, `pr_panel(task)`, `feedback_panel(task)`, `stats_table(stats)`, `event_line(event)`, `approval_panel(approval, *, with_diff=False)`, `approvals_table(rows)`, `render_task(task, stats=None, langfuse_url=None, sections=None)`

- [ ] **Step 1: Write the fixtures and failing tests**

`tests/client/fixtures.py` holds `TASK` (a completed change task: 2-step plan v1 + verifier + supervisor, 3 tool calls, observation, research with `relevant_files` and `risks`, one passed verification with 7 gates, patch, PR `{"number": 1, "url": "https://github.com/o/r/pull/1", "title": "chore: bump ws"}`, trace_id `"ab"*16`), `TASK_REPLANNED` (two plan versions, second with `reason_for_change`, plus `rejected_root_causes`), `APPROVAL` (arguments title/head/base, evidence with gates, patch, files), `STATS`.

```python
# tests/client/test_render.py
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
    out = text(render.plan_view(TASK))
    assert out.index("research") < out.index("coding") < out.index("verifier") < out.index("supervisor")
    assert "✓" in out and "after s1" in out


def test_plan_version_selection_and_replan_reason() -> None:
    out = text(render.plan_view(TASK_REPLANNED, version=2))
    assert "v2" in out and "Replanned because" in out
    assert "Replanned because" not in text(render.plan_view(TASK_REPLANNED, version=1))


def test_step_view_lists_observations_with_trust_and_tool_calls() -> None:
    out = text(render.step_view(TASK, "s1"))
    assert "github.search_code" in out and "untrusted" in out.lower() or "trusted" in out.lower()
    assert "ms" in out


def test_verification_shows_gates_issues_and_benchmark() -> None:
    out = text(render.verification_panel(TASK_REPLANNED))
    assert "✗" in out and "✓" in out


def test_diff_view_colours_added_and_removed_lines() -> None:
    d = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new\n"
    buf = io.StringIO()
    Console(file=buf, width=80, force_terminal=True, color_system="standard").print(render.diff_view(d))
    assert "\x1b[" in buf.getvalue() and "old" in buf.getvalue() and "new" in buf.getvalue()


def test_approval_panel_has_gates_title_branch_and_optional_diff() -> None:
    short = text(render.approval_panel(APPROVAL))
    assert "ap-" in short and "chore: bump ws" in short and "aeos/t-" in short and "patch_present" in short
    assert "diff --git" not in short
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
        arg = STATS if fn is render.stats_table else TASK
        assert text(fn(arg), width=40)


def test_render_task_sections_and_empty_task() -> None:
    full = text(render.render_task(TASK, STATS, "http://x"))
    for needle in ("PLAN", "RESEARCH FINDINGS", "VERIFICATION", "PATCH", "PULL REQUEST", "STATS"):
        assert needle in full
    only = text(render.render_task(TASK, STATS, sections={"stats"}))
    assert "STATS" in only and "PLAN" not in only
    assert text(render.render_task({"task_id": "t-9", "goal": "g", "status": "queued", "kind": "change"}))
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/client/test_render.py -q`
Expected: FAIL `ModuleNotFoundError: aeos.client.render`

- [ ] **Step 3: Implement `render.py`**

Rules the implementation must follow (each is covered by a test above):
- Status colouring: `completed|done|passed|approved` green; `failed|escalated|rejected|expired` red; `running|queued|planning` cyan; `awaiting_approval|pending` yellow. Icons `✓ ✗ … · ⏸` as in `TaskState.status_line`.
- `plan_view`: compute depth like `web/components/PlanGraph.tsx` (`depth = 0` with no deps, else `1 + max(depth of deps)`; guard cycles), render one `rich.table.Table.grid` column per depth, each step a `Panel` showing `owner · status[ · attempt N]`, the description, and `after sX, sY`. Title shows `v{version}` and, when `reason_for_change` is set, a line `Replanned because: …`. Available versions listed as `versions: v1 v2 (showing v2)`.
- `step_view`: observations (agent, `trusted|untrusted`, summary) then tool calls (`server.tool`, risk, `duration_ms`, arguments as compact JSON, result preview truncated to 600 chars, errors in red).
- `verification_panel`: last verification; gates as `✓/✗ name`; issues `type: detail`; benchmark `p95 a → b (±c%)`, `db queries a → b`; judge concerns; footer `no automated test suite was run` when `metrics.tests.ran is False`.
- `diff_view`: `rich.syntax.Syntax(clean(patch), "diff", theme="ansi_dark", word_wrap=True)`.
- `event_line(event)`: `HH:MM:SS  agent  type  message` using `safe`; long messages truncated at 300 chars with `…`.
- `approval_panel`: id, status, expiry, tool, title, `head → base`, rationale, gates, benchmark, root cause or research summary, file list (`path +a −r`), and the diff only when `with_diff`.
- `render_task`: sections in order header, status line/error, approvals pending note, plan, findings (change tasks), root cause (regression), verification, patch (first 40 lines plus `… N more lines, use --diff`), PR, decided approvals, feedback, stats. `sections` (set of names) filters. Missing keys are skipped, never an exception.
- Every untrusted string goes through `sanitize.safe`; no f-string builds `markup=True` content.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/client/test_render.py -q`
Expected: all passed

- [ ] **Step 5: Lint, types, secrets** (same command as Task 1 Step 9)

---

### Task 3: Watch flow and the approval prompt

**Files:**
- Create: `src/aeos/client/watch.py`
- Create: `src/aeos/client/prompts.py`
- Test: `tests/client/test_watch.py`

**Interfaces:**
- Consumes: `AeosClient` (Task 1), `render.*` (Task 2)
- Produces: `watch.watch_task(client, console, task_id, *, actor, interactive, from_start=False) -> str` (returns the final task status); `prompts.ask_approval(client, console, approval, *, actor) -> str` returns `"approved" | "rejected" | "skipped" | "stale"`

Behaviour:
1. `watch_task` prints the header, then replays/streams events with `render.event_line`. After each `step_done`, `verification`, `root_cause`, `approval_requested`, `pr_created`, `task_completed|task_failed|task_escalated` event it re-fetches `client.task(id)` and prints the relevant section once (plan after each step, verification card, PR panel).
2. On `approval_requested`, if `interactive`, call `prompts.ask_approval`; else print `approval ap-… is waiting: aeos approve ap-… | aeos reject ap-…` and keep following until the task finishes or the user detaches.
3. Stop when the task status is `completed|failed|escalated|cancelled`, or when it is `awaiting_approval` and not interactive (returns `awaiting_approval`).
4. Ctrl+C: print `detached; the task keeps running (aeos watch <id> to rejoin)` and return the last known status.

`ask_approval` loop (single keypress via `console.input`, case-insensitive): `a` approve · `r` reject (asks `reason> `) · `d` show full diff in `console.pager()` · `f` show research findings / root cause · `s` skip (leave pending). `a` and `r` call `client.decide(..., actor, feedback)`. `ApiError` 409 → print `already decided elsewhere` and return `"stale"`; 410 → `this approval expired; the task is escalated` and return `"stale"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/client/test_watch.py
import io
import json

import httpx
import pytest
from rich.console import Console

from aeos.client.api import AeosClient
from aeos.client.prompts import ask_approval
from aeos.client.watch import watch_task
from tests.client.fixtures import APPROVAL, TASK


def console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    return Console(file=buf, width=100, force_terminal=False, color_system=None), buf


def sse(*events: dict) -> bytes:  # type: ignore[type-arg]
    return b"".join(f"id: {e['id']}\nevent: aeos\ndata: {json.dumps(e)}\n\n".encode() for e in events)


EVENTS = [
    {"id": 1, "task_id": "t-1da798cf", "ts": "2026-10-07T10:00:00Z", "type": "plan_created", "agent": "supervisor", "message": "plan v1", "data": {}},
    {"id": 2, "task_id": "t-1da798cf", "ts": "2026-10-07T10:00:05Z", "type": "task_completed", "agent": "supervisor", "message": "task completed", "data": {}},
]


def api(handler) -> AeosClient:  # type: ignore[no-untyped-def]
    return AeosClient("http://aeos", transport=httpx.MockTransport(handler))


def test_watch_streams_events_and_returns_final_status() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/events/stream"):
            return httpx.Response(200, content=sse(*EVENTS))
        if req.url.path.endswith("/stats"):
            return httpx.Response(200, json={"llm": {"calls": 1, "tokens": 2, "cost_usd": 0.01}, "tools": {"calls": 0, "errors": 0}, "approvals": {}, "verifications": {}, "memory": {}})
        return httpx.Response(200, json=TASK)

    c, buf = console()
    status = watch_task(api(handler), c, "t-1da798cf", actor="alice", interactive=False)
    assert status == "completed"
    assert "plan_created" in buf.getvalue() and "task_completed" in buf.getvalue()


def test_non_interactive_watch_stops_at_an_approval_with_instructions() -> None:
    waiting = {**TASK, "status": "awaiting_approval", "pending_approval_id": "ap-1"}
    ev = [{**EVENTS[0], "type": "approval_requested", "message": "approval ap-1 needed", "data": {"approval_id": "ap-1"}, "id": 1}]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/events/stream"):
            return httpx.Response(200, content=sse(*ev))
        if req.url.path == "/approvals":
            return httpx.Response(200, json=[APPROVAL])
        return httpx.Response(200, json=waiting)

    c, buf = console()
    status = watch_task(api(handler), c, "t-1da798cf", actor="alice", interactive=False)
    assert status == "awaiting_approval" and "aeos approve ap-" in buf.getvalue()


@pytest.mark.parametrize("answer,decision", [("a", "approve"), ("r", "reject")])
def test_prompt_sends_the_decision_with_actor(answer: str, decision: str, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(path=req.url.path, actor=req.headers["x-actor"], body=json.loads(req.content))
        return httpx.Response(200, json={"status": "resuming"})

    c, _ = console()
    answers = iter([answer, "needs a smaller diff"] if answer == "r" else [answer])
    monkeypatch.setattr(c, "input", lambda *a, **k: next(answers))
    result = ask_approval(api(handler), c, APPROVAL, actor="alice")
    assert result in ("approved", "rejected") and seen["path"].endswith(decision) and seen["actor"] == "alice"
    if answer == "r":
        assert seen["body"]["feedback"] == "needs a smaller diff"


@pytest.mark.parametrize("status,word", [(409, "already decided"), (410, "expired")])
def test_prompt_handles_decisions_made_elsewhere_or_expired(status: int, word: str, monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = console()
    monkeypatch.setattr(c, "input", lambda *a, **k: "a")
    client = api(lambda r: httpx.Response(status, json={"detail": word}))
    assert ask_approval(client, c, APPROVAL, actor="alice") == "stale"
    assert word in buf.getvalue()


def test_prompt_can_show_the_diff_then_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    c, buf = console()
    answers = iter(["d", "s"])
    monkeypatch.setattr(c, "input", lambda *a, **k: next(answers))
    assert ask_approval(api(lambda r: httpx.Response(500)), c, APPROVAL, actor="alice") == "skipped"
    assert "diff --git" in buf.getvalue()


def test_ctrl_c_detaches_without_cancelling(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/events/stream"):
            raise KeyboardInterrupt
        return httpx.Response(200, json={**TASK, "status": "running"})

    c, buf = console()
    status = watch_task(api(handler), c, "t-1da798cf", actor="alice", interactive=True)
    assert status == "running" and "detached" in buf.getvalue()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/client/test_watch.py -q`
Expected: FAIL (modules missing)

- [ ] **Step 3: Implement `prompts.py` and `watch.py`** per the behaviour list above (use `console.pager(styles=True)` for the diff; the test console is not a terminal, so the pager path prints directly when `not console.is_terminal`).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/client/test_watch.py tests/client/test_api.py tests/client/test_render.py -q`
Expected: all passed

- [ ] **Step 5: Lint, types, secrets** (same as Task 1 Step 9)

---

### Task 4: One-shot commands and CLI wiring

**Files:**
- Create: `src/aeos/client/commands.py`
- Modify: `src/aeos/cli.py` (register subcommands; `--api`, `--actor`; `--kind` on `run`; `--direct` on legacy commands)
- Test: `tests/client/test_commands.py`; extend `tests/cli/test_cli.py`

**Interfaces:**
- Consumes: `AeosClient`, `render`, `watch_task`, `ask_approval`
- Produces: `commands.run_command(args, client, console) -> int` (dispatch table; exit codes below)

Subcommands (all accept the global `--api`, `--actor`, `--json`):

| Command | Behaviour | Exit code |
|---|---|---|
| `aeos tasks [--watch] [--status S]` | table; `--watch` refreshes every 3 s (not on non-TTY) | 0 |
| `aeos new "goal" [--kind change\|regression] [--watch] [--yes]` | create, print id, optionally watch. Default kind: `change` when `/meta` reports a `target_repo`, else `regression`. `--yes` approves without prompting, but only when `--actor` is given explicitly (so the audit log names a person); for `change` tasks it first prints "this will open a real pull request" | 0 completed, 1 failed/escalated, 3 left awaiting approval |
| `aeos show <id> [--plan] [--version N] [--step ID] [--findings] [--root-cause] [--verify] [--diff] [--stats]` | full view or just the named sections; `--json` prints the raw task JSON | 0 |
| `aeos watch <id>` | `watch_task`, interactive when TTY | as `new` |
| `aeos events <id> [--follow] [--after N]` | history, optionally follow | 0 |
| `aeos stats <id>` | stats table | 0 |
| `aeos approvals [--all]` | pending (or all) approvals across tasks | 0 |
| `aeos approve <approval-id> [--feedback T]` / `aeos reject ...` | decide; shows the card first unless `--yes` | 0 / 1 on stale |
| `aeos open <id>` | prints Langfuse trace URL and PR URL (OSC-8 hyperlinks on TTY) | 0 |
| `aeos status <id>` | one-line status (keeps working through the API) | 0 |

Errors: `ApiUnreachable` → print the message, exit 2. `ApiError 404` → `no such task`, exit 1. Never print a traceback for these.

Legacy compatibility: `aeos submit|status|approvals|approve|reject --direct` run the existing DB-backed code unchanged (the chaos harness and `tests/cli` use it). `aeos run` gains `--kind`.

- [ ] **Step 1: Write failing tests** covering each row of the table with `httpx.MockTransport` (JSON in, captured console out, exit code asserted): `tasks` prints a row per task; `new --kind change` posts `kind`; `new` without `--kind` asks `/meta` and picks `change` when `target_repo` is set; `show --diff` prints only the diff; `show --json` is valid JSON equal to the API payload; `approve` posts with the `--actor`; unreachable API → exit 2 and the hint; 404 → exit 1; `open` prints both URLs; non-TTY `new --watch` never calls `console.input`. Add to `tests/cli/test_cli.py`: `aeos run --kind change` parses; `--direct` still routes `status` to the DB implementation.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/client/test_commands.py tests/cli -q`
Expected: FAIL

- [ ] **Step 3: Implement `commands.py` and the CLI changes.** `cli.main` builds one `Console()` and one `AeosClient(args.api or default_api_url())` only for client commands (so `serve`/`worker` never need `rich` output or HTTP).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/client tests/cli -q`
Expected: all passed

- [ ] **Step 5: Lint, types, secrets** (same as Task 1 Step 9)

---

### Task 5: `aeos chat`: the interactive session

**Files:**
- Create: `src/aeos/client/chat.py`
- Modify: `src/aeos/cli.py` (register `chat`)
- Test: `tests/client/test_chat.py`

**Interfaces:**
- Consumes: everything above
- Produces: `chat.run_chat(client, console, *, actor, kind=None) -> int`

Session behaviour:
- Banner: API URL, target repo from `/meta`, current kind, actor, and `/help` hint. A one-line status of pending approvals ("2 approvals waiting") if any.
- A line that does **not** start with `/` is a new request: create the task with the current kind, then `watch_task(interactive=True)` inline (live events, plan after each step, approval prompt, PR link). Returns to the prompt when the task finishes or the user detaches with Ctrl+C.
- Slash commands (each is the interactive form of a Task 4 command): `/tasks`, `/show <id> [sections]`, `/watch <id>`, `/approvals`, `/approve <ap-id>`, `/reject <ap-id>`, `/stats <id>`, `/events <id>`, `/diff <id>`, `/open <id>`, `/target change|regression`, `/actor <name>`, `/help`, `/quit` (and EOF / Ctrl+D). `<id>` may be omitted to mean the last task of the session. Unknown command → one-line hint, never an exception.
- Empty input is ignored. API errors are printed and the session continues.

- [ ] **Step 1: Write failing tests** driving `run_chat` with scripted input via `monkeypatch.setattr(console, "input", ...)` and a `MockTransport` API: a plain sentence creates a task with the current kind and starts watching; `/target regression` changes the kind of the next request; `/help` lists every command; unknown command is survivable; `/quit` and EOF exit 0; `/show` without an id uses the last task; an `ApiUnreachable` mid-session is reported and the loop continues; `/approvals` + `/approve ap-1` posts a decision with the session actor and a changed `/actor`.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/client/test_chat.py -q`
Expected: FAIL

- [ ] **Step 3: Implement `chat.py`** and register `aeos chat [--kind K]` in `cli.py`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/client -q`
Expected: all passed

- [ ] **Step 5: Lint, types, secrets** (same as Task 1 Step 9)

---

### Task 6: End-to-end proof, wrapper script, docs

**Files:**
- Create: `tests/client/test_e2e_terminal.py` (real uvicorn server like `tests/graph/test_hitl.py`'s `running_app`, replay LLM `happy`)
- Create: `scripts/aeos` (executable shell wrapper)
- Modify: `docker-compose.yml` (api env `AEOS_API_URL: http://localhost:8000`), `README.md`, `docs/ARCHITECTURE.md`

- [ ] **Step 1: Write the end-to-end test.** Start the API (embedded worker, replay `happy`, the existing `settings` fixture pattern from `tests/graph/test_hitl.py`), then run the real `main([...])` entry point with `AEOS_API_URL` pointing at it: `new "Investigate the checkout latency regression" --kind regression --watch --yes-for-test-actor alice` driven by scripted answers (`a` at the approval prompt). Assert: exit code 0, the transcript contains `plan_created`, `verification`, `approval_requested`, `pr_created`, and the PR URL `local://`; `aeos show <id> --json` equals `GET /tasks/<id>`; `aeos approvals` is empty afterwards; a second invocation with the API stopped prints the unreachable hint and exits 2.
- [ ] **Step 2: Run it** (stop the dashboard stack first or use a different DB; this test creates tasks):
  Run: `uv run pytest tests/client/test_e2e_terminal.py -q`
  Expected: pass
- [ ] **Step 3: Wrapper.** `scripts/aeos`: `exec docker compose exec -it api aeos "$@"` when `docker compose ps api` is running, else `exec uv run aeos "$@"`. Mark executable. Document: `./scripts/aeos chat`.
- [ ] **Step 4: Docs.** README: a "Terminal" section with the parity table and a 6-line demo transcript; ARCHITECTURE: one paragraph "Clients: dashboard and terminal share the API".
- [ ] **Step 5: Manual smoke against the live stack** (owner present): `./scripts/aeos chat`, request "update the ws dependency to the latest version and raise a PR", watch events, press `d` to view the diff, `a` to approve, confirm the PR link prints. Record the result in `docs/PROGRESS.md`.
- [ ] **Step 6: Final checks:** `uv run ruff check . && uv run mypy src && uv run python scripts/check_no_secrets.py` and `uv run pytest tests/client tests/cli -q`. Expected: all clean/passed.

---

## Self-review

- **Spec coverage:** every row of the parity matrix maps to a command in Tasks 2–5; submit/new, tasks, show (plan, version, step, findings, root cause, verification, diff, PR, feedback), stats, events/watch (SSE with resume), approvals inbox, approve/reject with actor and feedback, trace link, actor persistence, target selection. The only dashboard item without a direct equivalent is the clickable plan graph, replaced by `show --plan` and `--step`.
- **Placeholders:** Tasks 2–5 implementation steps describe required behaviour precisely and are pinned by the listed tests; the tests are the contract, and the client/sanitizer code in Task 1 is complete. The implementer writes `render.py`, `watch.py`, `prompts.py`, `commands.py`, `chat.py` against those tests.
- **Type consistency:** `AeosClient` method names and return types are used identically in Tasks 3–5; `watch_task` and `ask_approval` signatures in Task 3 are the ones consumed by Tasks 4–5.
- **Review Focus coverage:** items 1–5 are pinned in Task 1 (sanitizer, unreachable, SSE resume), Task 2 (inert text, narrow width), Task 3 (409/410, Ctrl+C), Task 4 (non-TTY, exit codes).
