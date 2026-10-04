"""Runtime wiring: real MCP servers (child processes), sandbox manager, specialists, LLMs."""

import asyncio
import functools
import http.server
import logging
import os
import threading
from collections.abc import AsyncIterator, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from aeos.a2a.client import A2ASpecialist, parse_endpoints
from aeos.agents.sandbox_tools import CodingWorkspace, registry_tools
from aeos.agents.specialists import (
    AnalyticsSpecialist,
    ChangeCodingSpecialist,
    ChangeResearchSpecialist,
    CodingSpecialist,
    ResearchSpecialist,
    Specialist,
    SpecialistRequest,
)
from aeos.agents.tools import ToolDef, mcp_tooldefs
from aeos.agents.verifier import GateConfig
from aeos.core.config import Settings, get_settings
from aeos.core.events import EventSink, NullSink
from aeos.core.retry import RetryPolicy
from aeos.core.risk import PolicyEngine, RiskGate
from aeos.llm.base import LLMProvider
from aeos.llm.factory import get_llm
from aeos.llm.traced import TracedLLM
from aeos.mcp_client import McpClient, stdio_server
from aeos.mcp_servers.github.backend import GitBackend, GitError
from aeos.memory.embedder import Embedder, GeminiEmbedder, HashEmbedder
from aeos.memory.manager import MemoryManager
from aeos.memory.store import MemoryStore
from aeos.sandbox import NpmRegistry, RepoRef, SandboxManager
from aeos.target import sync_mirror

log = logging.getLogger("aeos.runtime")

GITHUB_READ = {"get_repository", "search_code", "get_file", "list_commits", "get_diff", "get_blame"}
CODING_READ = {"get_file", "search_code", "get_diff", "list_commits"}
DB_TOOLS = {"get_schema", "query_database", "explain_query"}
WEB_TOOLS = {"search_web", "fetch_page", "extract_information"}


@dataclass
class Runtime:
    settings: Settings
    llm: LLMProvider
    strong_llm: LLMProvider
    sink: EventSink
    gate: RiskGate
    sandbox: SandboxManager
    remote_path: str
    specialists: dict[str, Specialist]
    gate_cfg: GateConfig = field(default_factory=GateConfig)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    memory: MemoryManager | None = None
    clients: dict[str, McpClient] = field(default_factory=dict)
    tools: dict[str, dict[str, ToolDef]] = field(default_factory=dict)  # server -> tools
    # "change" tasks: specialists + GitHub tools bound to the mirror of the real target repo
    change_specialists: dict[str, Specialist] = field(default_factory=dict)
    target_tools: dict[str, ToolDef] = field(default_factory=dict)
    target_remote_path: str | None = None
    target_branch: str | None = None
    target_error: str | None = None

    def refresh_target(self) -> str:
        """Fetch the real repo's default branch into the mirror. Returns its head sha."""
        path, branch, sha = sync_mirror(self.settings)
        self.target_remote_path, self.target_branch = str(path), branch
        return sha

    def resolve_ref(self, ref: str = "main") -> str:
        return GitBackend(Path(self.remote_path)).resolve(ref)

    def baseline_for(
        self, suspect_sha: str, probe: str = "benchmark.py", max_back: int = 40
    ) -> str:
        """Nearest ancestor of the suspect that can actually be benchmarked.

        A baseline that predates the benchmark cannot verify anything (observed live: the model's
        "last good commit" was one that had no benchmark.py).
        """
        backend = GitBackend(Path(self.remote_path))
        for n in range(1, max_back + 1):
            try:
                candidate = backend.resolve(f"{suspect_sha}~{n}")
            except GitError:
                break
            if backend._git("cat-file", "-e", f"{candidate}:{probe}", check=False).returncode == 0:  # noqa: SLF001
                return candidate
        raise ValueError(f"no ancestor of {suspect_sha[:10]} contains {probe}")


def _serve_corpus(corpus_dir: Path) -> tuple[http.server.ThreadingHTTPServer, str]:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

    handler = functools.partial(Quiet, directory=str(corpus_dir))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _github_publish_env(s: Settings) -> dict[str, str]:
    """GitHub publishing config for the github MCP server (the token is deliberately passed
    explicitly: the generic passthrough strips secrets)."""
    if not (s.github_publish and s.github_repo and s.github_token):
        return {}
    env = {
        "AEOS_GITHUB_PUBLISH": "true",
        "AEOS_GITHUB_REPO": s.github_repo,
        "AEOS_GITHUB_TOKEN": s.github_token.get_secret_value(),
        "AEOS_GITHUB_BASE_BRANCH": s.github_base_branch,
        "AEOS_GITHUB_API_URL": s.github_api_url,
    }
    if s.github_git_url:
        env["AEOS_GITHUB_GIT_URL"] = s.github_git_url
    return env


@dataclass
class Toolsets:
    clients: dict[str, McpClient]
    tools: dict[str, dict[str, ToolDef]]

    def get(self, name: str) -> dict[str, ToolDef]:
        return self.tools.get(name, {})


_INCLUDE: dict[str, set[str]] = {"database": DB_TOOLS, "web": WEB_TOOLS}


async def open_toolsets(stack: AsyncExitStack, s: Settings, names: Iterable[str]) -> Toolsets:
    """Start the named MCP servers (child processes) and wrap their tools. Used by the in-process
    runtime AND by every A2A specialist service, which owns its own MCP connections."""
    remote = os.path.abspath(s.git_remote_path)  # noqa: ASYNC240 (cheap, no I/O)
    corpus = Path(os.path.abspath(s.web_corpus_dir))  # noqa: ASYNC240
    envs: dict[str, dict[str, str]] = {
        "observability": {"AEOS_DEMO_SCHEMA": s.demo_schema},
        "database": {"AEOS_DEMO_SCHEMA": s.demo_schema},
        "github": {"AEOS_GIT_REMOTE_PATH": remote, **_github_publish_env(s)},
    }
    clients: dict[str, McpClient] = {}
    tools: dict[str, dict[str, ToolDef]] = {}
    for name in names:
        env = envs.get(name, {})
        if name == "web":
            httpd, corpus_url = _serve_corpus(corpus)
            stack.callback(httpd.shutdown)
            env = {
                "AEOS_WEB_CORPUS_URL": corpus_url,
                "AEOS_WEB_CORPUS_DIR": str(corpus),
                "AEOS_WEB_ALLOWED_HOSTS": "127.0.0.1",
            }
        clients[name] = await stack.enter_async_context(
            McpClient(stdio_server(name, env=env), name=name)
        )
        tools[name] = await mcp_tooldefs(clients[name], name, include=_INCLUDE.get(name))
    return Toolsets(clients, tools)


def make_local_specialist(
    owner: str, llm: LLMProvider, gate: RiskGate, ts: Toolsets, sandbox: SandboxManager, remote: str
) -> Specialist:
    gh = ts.get("github")
    if owner == "analytics":
        return AnalyticsSpecialist(llm, gate, {**ts.get("observability"), **ts.get("database")})
    if owner == "research":
        return ResearchSpecialist(
            llm, gate, {**{k: v for k, v in gh.items() if k in GITHUB_READ}, **ts.get("web")}
        )
    if owner == "coding":

        def workspace(req: SpecialistRequest) -> CodingWorkspace:
            base = RepoRef(remote=remote, ref=GitBackend(Path(remote)).resolve("main"))
            baseline = RepoRef(remote=remote, ref=req.context["baseline_ref"])
            return CodingWorkspace(sandbox, base, baseline, req.task_id)

        return CodingSpecialist(
            llm, gate, {k: v for k, v in gh.items() if k in CODING_READ}, workspace
        )
    raise ValueError(f"unknown specialist {owner!r}")


def make_change_specialist(
    owner: str, llm: LLMProvider, gate: RiskGate, ts: Toolsets, sandbox: SandboxManager,
    remote: str, branch: str, npm: NpmRegistry,
) -> Specialist:  # fmt: skip
    gh = ts.get("github")
    if owner == "research":
        return ChangeResearchSpecialist(
            llm, gate, {**{k: v for k, v in gh.items() if k in GITHUB_READ}, **registry_tools(npm)}
        )
    if owner == "coding":

        def workspace(req: SpecialistRequest) -> CodingWorkspace:
            ref = RepoRef(remote=remote, ref=GitBackend(Path(remote)).resolve(branch))
            return CodingWorkspace(sandbox, ref, ref, req.task_id, profile="change", npm=npm)

        return ChangeCodingSpecialist(
            llm, gate, {k: v for k, v in gh.items() if k in CODING_READ}, workspace
        )
    raise ValueError(f"unknown change specialist {owner!r}")


def _traced(llm: LLMProvider) -> LLMProvider:
    return llm if isinstance(llm, TracedLLM) else TracedLLM(llm)


SPECIALIST_OWNERS = ("analytics", "research", "coding")
_TOOLSETS_FOR = {
    "analytics": ("observability", "database"),
    "research": ("github", "web"),
    "coding": ("github",),
}


@asynccontextmanager
async def open_specialist(
    owner: str,
    settings: Settings | None = None,
    *,
    llm: LLMProvider | None = None,
    gate: RiskGate | None = None,
) -> AsyncIterator[Specialist]:
    """One specialist with its own MCP connections: what an A2A service process runs."""
    s = settings or get_settings()
    async with AsyncExitStack() as stack:
        ts = await open_toolsets(stack, s, _TOOLSETS_FOR[owner])
        sandbox = SandboxManager() if owner == "coding" else None
        yield make_local_specialist(
            owner, _traced(llm or get_llm(s)), gate or PolicyEngine(), ts,
            sandbox,  # type: ignore[arg-type]
            os.path.abspath(s.git_remote_path),  # noqa: ASYNC240
        )  # fmt: skip


@asynccontextmanager
async def open_runtime(
    settings: Settings | None = None,
    *,
    llm: LLMProvider | None = None,
    strong_llm: LLMProvider | None = None,
    sink: EventSink | None = None,
    gate: RiskGate | None = None,
    gate_cfg: GateConfig | None = None,
    retry: RetryPolicy | None = None,
    embedder: Embedder | None = None,
    memory: bool | None = None,
    specialists: dict[str, Specialist] | None = None,
    npm: NpmRegistry | None = None,
) -> AsyncIterator[Runtime]:
    s = settings or get_settings()
    llm = _traced(llm or get_llm(s))
    strong = _traced(strong_llm) if strong_llm is not None else llm
    sink = sink or NullSink()
    gate = gate or PolicyEngine()
    remote = os.path.abspath(s.git_remote_path)  # noqa: ASYNC240 (cheap, no I/O)
    a2a = s.a2a_mode == "a2a" and specialists is None

    async with AsyncExitStack() as stack:
        # In A2A mode the supervisor only needs the repo tools (to publish); each specialist
        # service owns its own tool connections.
        names = ("github",) if a2a else ("observability", "database", "github", "web")
        # The demo repo's pull requests stay local; only the real target repo is ever published.
        ts = await open_toolsets(stack, s.model_copy(update={"github_publish": False}), names)
        sandbox = SandboxManager()
        mem: MemoryManager | None = None
        if s.memory_enabled if memory is None else memory:
            emb = embedder
            if emb is None:
                live = s.llm_provider == "gemini" and s.gemini_api_key is not None
                emb = (
                    GeminiEmbedder(s.gemini_api_key.get_secret_value())
                    if live and s.gemini_api_key
                    else HashEmbedder()
                )
            store = MemoryStore(emb)
            await store.ensure_schema()
            secrets = [v.get_secret_value() for v in (s.gemini_api_key, s.langfuse_secret_key) if v]
            mem = MemoryManager(store, token_budget=s.memory_token_budget, secret_values=secrets)

        specs: dict[str, Specialist]
        if specialists is not None:
            specs = specialists
        elif a2a:
            endpoints = parse_endpoints(s.a2a_endpoints)
            key = s.a2a_api_key.get_secret_value() if s.a2a_api_key else None
            specs = {
                owner: A2ASpecialist(
                    owner,
                    endpoints[owner],
                    api_key=key,
                    timeout_s=s.a2a_timeout_s,
                    stall_s=s.a2a_stall_s,
                )  # fmt: skip
                for owner in SPECIALIST_OWNERS
            }
        else:
            specs = {
                o: make_local_specialist(o, llm, gate, ts, sandbox, remote)
                for o in SPECIALIST_OWNERS
            }
        rt = Runtime(
            settings=s, llm=llm, strong_llm=strong, sink=sink, gate=gate, sandbox=sandbox,
            remote_path=remote, specialists=specs, gate_cfg=gate_cfg or GateConfig(),
            retry=retry or RetryPolicy(), memory=mem,
            clients=ts.clients,
            tools={n: ts.get(n) for n in ("observability", "database", "github", "web")},
        )  # fmt: skip
        if s.target_ready and specialists is None:
            await _open_target(stack, rt, s, llm, npm)
        yield rt


async def _open_target(
    stack: AsyncExitStack, rt: Runtime, s: Settings, llm: LLMProvider, npm: NpmRegistry | None
) -> None:
    """Wire "change" tasks: sync the real repo's mirror and start a github MCP server on it that
    is allowed to publish (approved PRs only) to the real GitHub repository."""
    try:
        sha = await asyncio.to_thread(rt.refresh_target)
        log.info("target repo %s mirrored at %s", s.github_repo, sha[:10])
    except Exception as exc:  # the API must still start; change tasks report the problem
        rt.target_error = f"{type(exc).__name__}: {exc}"
        log.warning("target repo unavailable: %s", rt.target_error)
        return
    assert rt.target_remote_path and rt.target_branch
    ts_cfg = s.model_copy(
        update={
            "git_remote_path": rt.target_remote_path,
            "github_base_branch": "@default",
            "github_publish": True,
        }
    )
    ts = await open_toolsets(stack, ts_cfg, ("github",))
    npm = npm or NpmRegistry()
    rt.target_tools = ts.get("github")
    rt.change_specialists = {
        o: make_change_specialist(
            o, llm, rt.gate, ts, rt.sandbox, rt.target_remote_path, rt.target_branch, npm
        )
        for o in ("research", "coding")
    }
