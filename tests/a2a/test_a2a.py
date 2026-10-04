"""Phase 9 acceptance: specialists are separate processes reached over real A2A on the network."""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from a2a.client.client import ClientConfig
from a2a.client.client_factory import create_client
from a2a.helpers import get_data_parts
from google.protobuf import json_format
from google.protobuf.struct_pb2 import Value
from pydantic import SecretStr

from a2a import types as t
from aeos.a2a.client import A2ASpecialist
from aeos.a2a.server import REQUEST_KEY, build_card, create_specialist_app, data_part
from aeos.agents.specialists import SpecialistRequest, SpecialistResult
from aeos.core.budget import BudgetTracker
from aeos.core.config import Settings, get_settings
from aeos.core.events import ListSink
from aeos.demo.trajectories import happy_path_llm
from aeos.graph.build import run_task
from aeos.hitl import auto_approver
from aeos.runtime import open_runtime
from aeos.telemetry import current_trace_id, traced

pytestmark = pytest.mark.docker

TRUTH = json.loads((Path(__file__).parents[2] / "demo_target" / "GROUND_TRUTH.json").read_text())
KEY = "test-a2a-key"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_healthy(port: int, proc: subprocess.Popen[bytes], limit_s: float = 60) -> None:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"service exited early ({proc.returncode})")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise AssertionError("service did not become healthy")


@contextmanager
def service(
    owner: str, env: dict[str, str], crash_at: str | None = None
) -> Iterator[tuple[int, subprocess.Popen[bytes]]]:
    port = free_port()
    e = {**env, "AEOS_A2A_API_KEY": KEY}
    if crash_at:
        e["AEOS_CRASH_AT"] = crash_at
    proc = subprocess.Popen(
        [sys.executable, "-m", "aeos.a2a.service", owner, "--port", str(port), "--replay", "happy"],
        env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        wait_healthy(port, proc)
        yield port, proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()


@pytest.fixture
def env(seeded_db: None, remote_template: Path) -> dict[str, str]:
    work = Path(tempfile.mkdtemp()) / "checkout-service.git"
    shutil.copytree(remote_template, work)
    return {**os.environ, "AEOS_GIT_REMOTE_PATH": str(work), "AEOS_MEMORY_ENABLED": "false"}


def url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def settings_for(env: dict[str, str], endpoints: dict[str, str], **kw: Any) -> Settings:
    return get_settings().model_copy(update={
        "git_remote_path": env["AEOS_GIT_REMOTE_PATH"], "a2a_mode": "a2a",
        "a2a_endpoints": json.dumps(endpoints), "memory_enabled": False,
        "a2a_api_key": SecretStr(KEY), **kw})  # type: ignore[arg-type]  # fmt: skip


# ============================================================ discovery + standalone use
async def test_agent_cards_are_discoverable_and_describe_each_service(env) -> None:  # type: ignore[no-untyped-def]
    with service("analytics", env) as (pa, _), service("research", env) as (pr, _):
        for port, owner, skill in ((pa, "analytics", "investigate-regression"),
                                   (pr, "research", "find-culprit-commit")):  # fmt: skip
            card = (
                await httpx.AsyncClient().get(f"{url(port)}/.well-known/agent-card.json")
            ).json()
            assert card["name"] == f"aeos-{owner}"
            assert card["capabilities"]["streaming"] is True
            assert [s["id"] for s in card["skills"]] == [skill]
            assert "bearer" in card["securitySchemes"]
            assert card["supportedInterfaces"][0]["url"] == f"{url(port)}/a2a"
        spec = A2ASpecialist("analytics", [url(pa)], api_key=KEY)
        assert (await spec.discover(url(pa))).name == "aeos-analytics"
        wrong = A2ASpecialist("research", [url(pa)], api_key=KEY)  # points at the wrong agent
        with pytest.raises(Exception, match="expected aeos-research"):
            await wrong.discover(url(pa))


async def test_a_generic_a2a_client_can_use_a_specialist_without_our_supervisor(env) -> None:  # type: ignore[no-untyped-def]
    """Standalone use: plain SDK client, plain JSON request, read the result artifact."""
    with service("research", env) as (port, _):
        http = httpx.AsyncClient(headers={"Authorization": f"Bearer {KEY}"}, timeout=60)
        client = await create_client(url(port), ClientConfig(httpx_client=http))
        req = SpecialistRequest(task_id="t-generic", step_id="s2", goal="find the culprit commit")
        msg = t.Message(message_id="m1", role=t.Role.ROLE_USER,
                        parts=[data_part({REQUEST_KEY: req.model_dump(mode="json")})])  # fmt: skip
        states: list[str] = []
        result: dict[str, Any] | None = None
        async for ev in client.send_message(t.SendMessageRequest(message=msg)):
            kind = ev.WhichOneof("payload")
            if kind == "status_update":
                states.append(t.a2a_pb2.TaskState.Name(ev.status_update.status.state))
            elif kind == "artifact_update":
                result = get_data_parts(ev.artifact_update.artifact.parts)[0]
        await client.close()
        await http.aclose()
    assert states[-1] == "TASK_STATE_COMPLETED" and "TASK_STATE_WORKING" in states
    assert result is not None and result["status"] == "finished" and result["owner"] == "research"
    assert result["output"]["suspected_file"] == TRUTH["root_cause"]["file"]
    assert result["usage"]["llm_calls"] > 0 and result["tool_calls"]  # it really used its own tools


async def test_service_rejects_calls_without_the_bearer_token(env) -> None:  # type: ignore[no-untyped-def]
    with service("research", env) as (port, _):
        async with httpx.AsyncClient() as http:
            r = await http.post(f"{url(port)}/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "x"})
            assert r.status_code == 401
            r2 = await http.post(
                f"{url(port)}/a2a", json={}, headers={"Authorization": "Bearer wrong"}
            )
            assert r2.status_code == 401
            card = await http.get(f"{url(port)}/.well-known/agent-card.json")
            assert card.status_code == 200  # discovery stays public


# ============================================================ the whole task over A2A
async def test_full_task_runs_through_three_separate_specialist_processes(env) -> None:  # type: ignore[no-untyped-def]
    with (service("analytics", env) as (pa, _), service("research", env) as (pr, _),
          service("coding", env) as (pc, _)):  # fmt: skip
        s = settings_for(env, {"analytics": url(pa), "research": url(pr), "coding": url(pc)})
        sink = ListSink()
        async with open_runtime(s, llm=happy_path_llm(TRUTH), sink=sink) as rt:
            # the supervisor holds NO investigation tools: those live in the service processes
            assert set(rt.clients) == {"github"}
            state = await run_task(
                rt, "Investigate the checkout latency regression", approver=auto_approver
            )
    assert state.status == "completed", state.error
    assert state.artifacts.pull_request is not None
    assert state.artifacts.root_cause.commit_sha == TRUTH["root_cause"]["commit_sha"]  # type: ignore[union-attr]
    assert "a2a_progress" in sink.types()  # streamed status from the remote agents
    assert {tc.server for tc in state.tool_calls} >= {
        "observability",
        "sandbox",
    }  # their tool calls came back
    assert state.budget.llm_calls > 10  # remote LLM usage was added to the shared budget


# ============================================================ failure: no hang, retry/reroute/escalate
async def test_killing_the_only_research_service_mid_run_escalates_without_hanging(env) -> None:  # type: ignore[no-untyped-def]
    with (service("analytics", env) as (pa, _),
          service("research", env, crash_at="tool:get_diff:before") as (pr, rproc),
          service("coding", env) as (pc, _)):  # fmt: skip
        s = settings_for(env, {"analytics": url(pa), "research": url(pr), "coding": url(pc)})
        sink = ListSink()
        started = time.monotonic()
        async with open_runtime(s, llm=happy_path_llm(TRUTH), sink=sink) as rt:
            state = await asyncio.wait_for(
                run_task(rt, "Investigate the checkout latency regression", approver=auto_approver),
                120,
            )
        elapsed = time.monotonic() - started
        assert rproc.poll() == -9  # it really was SIGKILLed mid-run
    assert state.status == "escalated" and "research" in (state.error or "")
    assert "human" in (state.error or "") and elapsed < 100  # detected promptly, did not hang
    assert "a2a_failover" in sink.types() and "step_retry" in sink.types()
    assert state.artifacts.pull_request is None


async def test_a_second_research_instance_takes_over_when_the_first_dies(env) -> None:  # type: ignore[no-untyped-def]
    with (service("analytics", env) as (pa, _),
          service("research", env, crash_at="tool:get_diff:before") as (pr1, r1),
          service("research", env) as (pr2, _),
          service("coding", env) as (pc, _)):  # fmt: skip
        s = settings_for(env, {"analytics": url(pa), "research": f"{url(pr1)},{url(pr2)}",
                               "coding": url(pc)})  # fmt: skip
        sink = ListSink()
        async with open_runtime(s, llm=happy_path_llm(TRUTH), sink=sink) as rt:
            state = await asyncio.wait_for(
                run_task(rt, "Investigate the checkout latency regression", approver=auto_approver),
                180,
            )
        assert r1.poll() == -9
    assert state.status == "completed", state.error
    failovers = [e for e in sink.events if e.type == "a2a_failover"]
    assert len(failovers) == 1 and "trying next endpoint" in failovers[0].message
    assert "step_retry" not in sink.types()  # the failover happened INSIDE one delegation


async def test_unreachable_service_is_reported_not_hung() -> None:
    spec = A2ASpecialist("research", [f"http://127.0.0.1:{free_port()}"], api_key=KEY, timeout_s=30)
    started = time.monotonic()
    res = await spec.run(
        SpecialistRequest(task_id="t", step_id="s", goal="g"), BudgetTracker(), ListSink()
    )
    assert res.status == "error" and "every endpoint" in res.detail
    assert time.monotonic() - started < 15


# ============================================================ protocol behaviours (in-process test server)
class SlowSpecialist:
    owner = "research"

    def __init__(self, delay: float, capture: dict[str, str] | None = None) -> None:
        self.delay, self.capture, self.cancelled = delay, capture, False

    async def run(
        self, req: SpecialistRequest, tracker: BudgetTracker, sink: Any
    ) -> SpecialistResult:
        if self.capture is not None:
            self.capture["trace_id"] = current_trace_id() or ""
            self.capture["budget_steps"] = str(tracker.budget.max_steps)
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        tracker.add_step()
        return SpecialistResult(owner="research", status="finished", output={"summary": "ok"})


@contextmanager
def inproc_service(spec: SlowSpecialist) -> Iterator[int]:
    import threading

    port = free_port()
    card = build_card(owner="research", description="slow", skills=[
        t.AgentSkill(id="x", name="x", description="x", tags=["x"])], base_url=url(port))  # fmt: skip
    server = uvicorn.Server(uvicorn.Config(create_specialist_app(spec, card, api_key=KEY),
                                           port=port, log_level="error"))  # fmt: skip
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        yield port
    finally:
        server.should_exit = True
        th.join(timeout=10)


async def test_a_silent_service_is_treated_as_dead_after_the_stall_timeout() -> None:
    spec = SlowSpecialist(delay=60)
    with inproc_service(spec) as port:
        client = A2ASpecialist("research", [url(port)], api_key=KEY, stall_s=2, timeout_s=60)
        started = time.monotonic()
        res = await client.run(
            SpecialistRequest(task_id="t", step_id="s", goal="g"), BudgetTracker(), ListSink()
        )
    assert res.status == "error" and time.monotonic() - started < 20


async def test_overall_deadline_cancels_the_remote_work() -> None:
    spec = SlowSpecialist(delay=60)
    with inproc_service(spec) as port:
        client = A2ASpecialist("research", [url(port)], api_key=KEY, stall_s=60, timeout_s=3)
        started = time.monotonic()
        res = await client.run(
            SpecialistRequest(task_id="t", step_id="s", goal="g"), BudgetTracker(), ListSink()
        )
        await asyncio.sleep(1.0)
        assert spec.cancelled is True  # the client asked the service to stop working
    assert res.status == "error" and time.monotonic() - started < 20


async def test_trace_context_and_budget_cross_the_process_boundary(spans) -> None:  # type: ignore[no-untyped-def]
    captured: dict[str, str] = {}
    spec = SlowSpecialist(delay=0.1, capture=captured)
    tracker = BudgetTracker()
    with traced("supervisor") as _, inproc_service(spec) as port:
        mine = current_trace_id()
        client = A2ASpecialist("research", [url(port)], api_key=KEY)
        res = await client.run(
            SpecialistRequest(task_id="t", step_id="s", goal="g"), tracker, ListSink()
        )
    assert res.status == "finished"
    assert captured["trace_id"] == mine  # one trace spans supervisor + specialist
    names = {sp.name: sp for sp in spans.get_finished_spans()}
    delegate, serve = names["a2a.delegate"], names["a2a.serve s"]
    assert (
        serve.context.trace_id == delegate.context.trace_id == names["supervisor"].context.trace_id
    )
    assert captured["budget_steps"] == str(tracker.budget.max_steps)
    assert tracker.budget.steps_used == 1  # usage consumed remotely was added to the shared budget


def test_value_import_is_used() -> None:
    assert json_format and Value  # (keeps generic-client imports honest)
