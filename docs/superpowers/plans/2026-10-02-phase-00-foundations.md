# Phase 0: Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A reproducible dev environment: uv project with lint/type/test tooling, a healthy local infra stack, config, JSON logging with run context, the LLM provider interface with `FakeLLM`, and green CI.

**Architecture:** `src/aeos/` package. `core/` holds config + logging (strict-typed). `llm/base.py` defines a provider-agnostic async interface with Pydantic message types; `llm/fake.py` is a scripted implementation used by all later tests. Infra runs via Docker Compose (Postgres+pgvector, Redis, OTel collector; Langfuse stack under an `observability` profile).

**Tech Stack:** Python 3.12, uv, pydantic v2, pydantic-settings, pytest + pytest-asyncio, ruff, mypy, Docker Compose, GitHub Actions.

**Spec:** `ROADMAP.md` Section 0, Section 4 "Phase 0", Section 3 tech stack. Master plan: `2026-10-02-aeos-master-plan.md`.

## Global Constraints

- Python 3.12+, `uv` for env/deps; `ruff` + `mypy --strict` on `core` (and `llm`) + `pytest`.
- Pydantic Settings, env-based config, `.env.example` committed.
- JSON structured logs carrying `task_id` / `run_id` / `agent`.
- No provider-specific LLM calls outside `src/aeos/llm/`.
- CI: lint, type-check, unit tests, **no live LLM**. Live tests use `@pytest.mark.live`, excluded by default.
- Conventional commits, one logical change each.
- Ask before adding any major dependency beyond: pydantic, pydantic-settings, pytest, pytest-asyncio, ruff, mypy.

## Review Focus

- Missing/invalid env vars (e.g. bad `DATABASE_URL`, unknown `LOG_LEVEL`): settings raise a clear validation error naming the field (Task 2 test).
- Secrets (`SecretStr` API keys) never appear in logs or `repr(settings)` (Task 2 + 3 tests).
- Logging with no run context set, and context leaking across concurrent asyncio tasks: defaults to null, isolated per task (Task 3 tests).
- `FakeLLM` called more times than scripted, or with an unexpected prompt: fails loudly with a descriptive error, never returns a silent default (Task 4 tests).
- A log message containing non-JSON-serializable extras (e.g. a `Path`, an exception): formatter must not crash (Task 3 test).

## File Structure

```
.gitignore
.env.example
.python-version                  # 3.12
pyproject.toml
Makefile
docker-compose.yml
infra/otel-collector.yaml
scripts/check_infra.py
.github/workflows/ci.yml
docs/PROGRESS.md
docs/DECISIONS.md
src/aeos/__init__.py
src/aeos/core/__init__.py
src/aeos/core/config.py          # Settings
src/aeos/core/logging.py         # JSON logging + run context
src/aeos/llm/__init__.py
src/aeos/llm/base.py             # message/tool types + LLMProvider ABC
src/aeos/llm/fake.py             # scripted FakeLLM
tests/conftest.py
tests/core/test_config.py
tests/core/test_logging.py
tests/llm/test_fake.py
tests/test_no_network.py
```

---

### Task 1: Repo scaffold and tooling

**Files:**
- Create: `.gitignore`, `.python-version`, `pyproject.toml`, `Makefile`, `src/aeos/__init__.py`, `src/aeos/core/__init__.py`, `src/aeos/llm/__init__.py`, `tests/conftest.py`, `tests/test_smoke.py`, `docs/PROGRESS.md`, `docs/DECISIONS.md`

**Interfaces:**
- Produces: `make lint`, `make typecheck`, `make test`, `make check` (all three) used by CI and every later phase. `pytest` marker `live` (excluded by default).

- [ ] **Step 1: Initialise git and uv**

```bash
cd /home/srihari/Documents/meincodebase/autonomous-engineering-os
git init -b main
echo "3.12" > .python-version
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "aeos"
version = "0.1.0"
description = "Autonomous Engineering Operating System"
requires-python = ">=3.12"
dependencies = [
  "pydantic>=2.7",
  "pydantic-settings>=2.3",
]

[dependency-groups]
dev = [
  "pytest>=8.2",
  "pytest-asyncio>=0.23",
  "ruff>=0.5",
  "mypy>=1.10",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/aeos"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
addopts = "-m 'not live' -ra"
markers = ["live: hits a real LLM/provider; excluded from default runs"]

[tool.ruff]
line-length = 100
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "ASYNC"]

[tool.mypy]
python_version = "3.12"
plugins = ["pydantic.mypy"]

[[tool.mypy.overrides]]
module = ["aeos.core.*", "aeos.llm.*"]
strict = true
```

- [ ] **Step 3: Write `.gitignore`, package `__init__` files, `Makefile`**

`.gitignore`:
```
.venv/
__pycache__/
*.pyc
.env
.mypy_cache/
.ruff_cache/
.pytest_cache/
dist/
node_modules/
.next/
```

`src/aeos/__init__.py`:
```python
__version__ = "0.1.0"
```
`src/aeos/core/__init__.py` and `src/aeos/llm/__init__.py`: empty files.

`Makefile` (use real tabs):
```makefile
.PHONY: install lint typecheck test check up down infra-check

install:
	uv sync

lint:
	uv run ruff check . && uv run ruff format --check .

typecheck:
	uv run mypy src

test:
	uv run pytest

check: lint typecheck test

up:
	docker compose up -d --wait

down:
	docker compose down

infra-check:
	uv run python scripts/check_infra.py
```

- [ ] **Step 4: Seed docs (roadmap Section 0 items 3, 4 and "Start here")**

`docs/PROGRESS.md`:
```markdown
# Progress

## Plan summary (≤10 bullets; source: ROADMAP.md)
- Goal: durable multi-agent system that investigates, fixes, verifies, and PRs an engineering task with human approval, crash recovery, tracing, evals.
- Phases 0-5 = vertical slice (foundations, demo target, MCP, sandbox, agent loop, verifier+replan).
- Phases 6-7 = risk policy + HITL, durable execution.
- Phases 8-9 = memory, real A2A topology.
- Phases 10-13 = observability, evals, failure injection, security.
- Phases 14-15 = dashboard, docs, demo video.
- Deterministic code owns safety (risk policy, sandbox, verifier gates, DB read-only); LLMs advise.
- Never cut: verifier loop, HITL exact resume, durable execution, sandbox, evals, tracing.
- Tests use FakeLLM; live tests are marked `live`.
- Detailed plans live in docs/superpowers/plans/.

## Phase 0: Foundations
Status: in progress
```

`docs/DECISIONS.md`:
```markdown
# Decisions (lightweight ADRs)

## D-001: Checkpointing: LangGraph vs Temporal
Status: tentative. Use LangGraph + Postgres checkpointer; revisit after Phase 7.
Reason: roadmap tech stack fixes LangGraph; Temporal is a COULD.
```

- [ ] **Step 5: Write the smoke test and run the toolchain**

`tests/conftest.py`: empty file. `tests/test_smoke.py`:
```python
import aeos


def test_package_imports() -> None:
    assert aeos.__version__ == "0.1.0"
```

Run: `make install && make check`
Expected: lint, mypy, and `1 passed`. If `ruff format --check` complains, run `uv run ruff format .` and re-run.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: scaffold uv project with ruff, mypy, pytest and progress docs"
```

---

### Task 2: Settings (Pydantic Settings)

**Files:**
- Create: `src/aeos/core/config.py`, `.env.example`
- Test: `tests/core/test_config.py`

**Interfaces:**
- Produces: `class Settings(BaseSettings)`; `get_settings() -> Settings` (cached). Fields: `database_url: str`, `redis_url: str`, `otel_exporter_endpoint: str`, `langfuse_host: str`, `langfuse_public_key: SecretStr | None`, `langfuse_secret_key: SecretStr | None`, `anthropic_api_key: SecretStr | None`, `openai_api_key: SecretStr | None`, `llm_provider: Literal["anthropic","openai","fake"]`, `log_level: Literal["DEBUG","INFO","WARNING","ERROR"]`. Env prefix `AEOS_`.

- [ ] **Step 1: Write the failing tests**

`tests/core/test_config.py` (create empty `tests/core/__init__.py` is unnecessary with pytest rootdir; keep none):
```python
import pytest
from pydantic import ValidationError

from aeos.core.config import Settings


def test_defaults_point_at_local_compose() -> None:
    s = Settings(_env_file=None)
    assert s.database_url.startswith("postgresql://")
    assert s.redis_url.startswith("redis://")
    assert s.llm_provider == "fake"


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEOS_LOG_LEVEL", "DEBUG")
    assert Settings(_env_file=None).log_level == "DEBUG"


def test_invalid_log_level_names_the_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEOS_LOG_LEVEL", "LOUD")
    with pytest.raises(ValidationError, match="log_level"):
        Settings(_env_file=None)


def test_invalid_database_url_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEOS_DATABASE_URL", "not-a-url")
    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None)


def test_secrets_not_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEOS_ANTHROPIC_API_KEY", "sk-super-secret")
    s = Settings(_env_file=None)
    assert "sk-super-secret" not in repr(s)
    assert "sk-super-secret" not in str(s.model_dump())
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/core/test_config.py -v`
Expected: FAIL (`ModuleNotFoundError: aeos.core.config`).

- [ ] **Step 3: Implement**

`src/aeos/core/config.py`:
```python
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEOS_", env_file=".env", extra="ignore")

    database_url: str = "postgresql://aeos:aeos@localhost:5432/aeos"
    redis_url: str = "redis://localhost:6379/0"
    otel_exporter_endpoint: str = "http://localhost:4317"
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    llm_provider: Literal["anthropic", "openai", "fake"] = "fake"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, v: str) -> str:
        if not v.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError("database_url must start with postgresql://")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

`.env.example`:
```
AEOS_DATABASE_URL=postgresql://aeos:aeos@localhost:5432/aeos
AEOS_REDIS_URL=redis://localhost:6379/0
AEOS_OTEL_EXPORTER_ENDPOINT=http://localhost:4317
AEOS_LANGFUSE_HOST=http://localhost:3000
AEOS_LANGFUSE_PUBLIC_KEY=
AEOS_LANGFUSE_SECRET_KEY=
AEOS_ANTHROPIC_API_KEY=
AEOS_OPENAI_API_KEY=
AEOS_LLM_PROVIDER=fake
AEOS_LOG_LEVEL=INFO
```

Note: blank values in `.env` for `SecretStr | None` become empty strings, not `None`. If a test or run shows that, treat empty string as unset by adding `env_ignore_empty=True` to `model_config`.

- [ ] **Step 4: Run tests, then `make check`**

Run: `uv run pytest tests/core/test_config.py -v && make check`
Expected: 5 passed; check green.

- [ ] **Step 5: Commit**

```bash
git add src/aeos/core/config.py .env.example tests/core/test_config.py
git commit -m "feat(core): add env-based Settings with validation and secret masking"
```

---

### Task 3: Structured JSON logging with run context

**Files:**
- Create: `src/aeos/core/logging.py`
- Test: `tests/core/test_logging.py`

**Interfaces:**
- Produces:
  - `run_context(*, task_id: str | None = None, run_id: str | None = None, agent: str | None = None) -> ContextManager[None]`: sets context vars for the block; unspecified fields keep their current value.
  - `configure_logging(level: str = "INFO") -> None`: installs a JSON formatter on the root logger (idempotent).
  - Each log line is one JSON object with keys `ts`, `level`, `logger`, `message`, `task_id`, `run_id`, `agent`, plus any `extra=` fields; non-serializable values rendered with `repr`.

- [ ] **Step 1: Write the failing tests**

```python
import asyncio
import io
import json
import logging
from pathlib import Path

import pytest

from aeos.core.logging import configure_logging, run_context


@pytest.fixture
def stream() -> io.StringIO:
    buf = io.StringIO()
    configure_logging("DEBUG", stream=buf)
    return buf


def lines(buf: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(x) for x in buf.getvalue().splitlines() if x]


def test_emits_json_with_context(stream: io.StringIO) -> None:
    log = logging.getLogger("t")
    with run_context(task_id="t1", run_id="r1", agent="supervisor"):
        log.info("hello", extra={"step": 3})
    rec = lines(stream)[0]
    assert rec["message"] == "hello"
    assert rec["task_id"] == "t1" and rec["run_id"] == "r1" and rec["agent"] == "supervisor"
    assert rec["step"] == 3
    assert rec["level"] == "INFO"


def test_no_context_defaults_to_null(stream: io.StringIO) -> None:
    logging.getLogger("t").info("x")
    rec = lines(stream)[0]
    assert rec["task_id"] is None and rec["agent"] is None


def test_nested_context_overrides_only_given_fields(stream: io.StringIO) -> None:
    with run_context(task_id="t1", agent="a"):
        with run_context(agent="b"):
            logging.getLogger("t").info("x")
    rec = lines(stream)[0]
    assert rec["task_id"] == "t1" and rec["agent"] == "b"


async def test_context_isolated_between_concurrent_tasks(stream: io.StringIO) -> None:
    async def work(name: str) -> None:
        with run_context(task_id=name):
            await asyncio.sleep(0.01)
            logging.getLogger("t").info(name)

    await asyncio.gather(work("A"), work("B"))
    for rec in lines(stream):
        assert rec["task_id"] == rec["message"]


def test_non_serializable_extra_does_not_crash(stream: io.StringIO) -> None:
    logging.getLogger("t").info("x", extra={"p": Path("/tmp"), "e": ValueError("boom")})
    rec = lines(stream)[0]
    assert "tmp" in str(rec["p"]) and "boom" in str(rec["e"])


def test_exception_info_included(stream: io.StringIO) -> None:
    try:
        raise RuntimeError("bad")
    except RuntimeError:
        logging.getLogger("t").exception("failed")
    assert "RuntimeError" in str(lines(stream)[0]["exc_info"])


def test_configure_is_idempotent(stream: io.StringIO) -> None:
    configure_logging("DEBUG", stream=stream)
    logging.getLogger("t").info("once")
    assert len(lines(stream)) == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/core/test_logging.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

`src/aeos/core/logging.py`:
```python
import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import IO, Any

_task_id: ContextVar[str | None] = ContextVar("task_id", default=None)
_run_id: ContextVar[str | None] = ContextVar("run_id", default=None)
_agent: ContextVar[str | None] = ContextVar("agent", default=None)

# attributes present on every LogRecord; anything else came from `extra=`
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


@contextmanager
def run_context(
    *, task_id: str | None = None, run_id: str | None = None, agent: str | None = None
) -> Iterator[None]:
    tokens = []
    if task_id is not None:
        tokens.append((_task_id, _task_id.set(task_id)))
    if run_id is not None:
        tokens.append((_run_id, _run_id.set(run_id)))
    if agent is not None:
        tokens.append((_agent, _agent.set(agent)))
    try:
        yield
    finally:
        for var, tok in reversed(tokens):
            var.reset(tok)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "task_id": _task_id.get(),
            "run_id": _run_id.get(),
            "agent": _agent.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=repr)


_HANDLER_FLAG = "_aeos_json_handler"


def configure_logging(level: str = "INFO", *, stream: IO[str] | None = None) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _HANDLER_FLAG, False):
            root.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    setattr(handler, _HANDLER_FLAG, True)
    root.addHandler(handler)
    root.setLevel(level)
```

- [ ] **Step 4: Run tests, then `make check`**

Run: `uv run pytest tests/core/test_logging.py -v && make check`
Expected: 7 passed. If mypy strict flags `setattr`/`ContextVar` typing, annotate rather than ignore.

- [ ] **Step 5: Commit**

```bash
git add src/aeos/core/logging.py tests/core/test_logging.py
git commit -m "feat(core): add JSON logging with task/run/agent context"
```

---

### Task 4: LLM provider interface and FakeLLM

**Files:**
- Create: `src/aeos/llm/base.py`, `src/aeos/llm/fake.py`
- Modify: `src/aeos/llm/__init__.py`
- Test: `tests/llm/test_fake.py`, `tests/test_no_network.py`, `tests/conftest.py`

**Interfaces:**
- Produces (`aeos.llm.base`):
  ```python
  class ToolCall(BaseModel):            id: str; name: str; arguments: dict[str, Any]
  class Message(BaseModel):             role: Literal["system","user","assistant","tool"]; content: str = ""; tool_calls: list[ToolCall] = []; tool_call_id: str | None = None
  class ToolSpec(BaseModel):            name: str; description: str; parameters: dict[str, Any]   # JSON Schema
  class Usage(BaseModel):               input_tokens: int = 0; output_tokens: int = 0
  class LLMResponse(BaseModel):         content: str = ""; tool_calls: list[ToolCall] = []; usage: Usage = Usage(); model: str = ""
  class LLMError(Exception)
  class LLMProvider(ABC):               async def complete(self, messages: list[Message], *, tools: list[ToolSpec] | None = None, max_tokens: int = 4096) -> LLMResponse
  ```
- Produces (`aeos.llm.fake`):
  ```python
  class ScriptStep(BaseModel):   response: LLMResponse; expect_in_prompt: str | None = None
  class ScriptExhaustedError(LLMError); class UnexpectedPromptError(LLMError)
  class FakeLLM(LLMProvider):    __init__(self, script: list[ScriptStep]); calls: list[list[Message]]; remaining -> int
  ```
  Later phases rely on: sequential scripted replies, `expect_in_prompt` assertion against the last non-system message content, call recording, and tool-call responses.
- Real providers (Anthropic, OpenAI) are intentionally **not** in Phase 0; they are the first task of Phase 4's plan (they need live-key testing and the loop's tool-call parsing to shape them). Record this in PROGRESS.md.

- [ ] **Step 1: Write the failing tests**

`tests/llm/test_fake.py`:
```python
import pytest

from aeos.llm.base import LLMResponse, Message, ToolCall
from aeos.llm.fake import FakeLLM, ScriptExhaustedError, ScriptStep, UnexpectedPromptError

USER = [Message(role="user", content="find the regression")]


async def test_returns_scripted_responses_in_order() -> None:
    llm = FakeLLM(
        [
            ScriptStep(response=LLMResponse(content="one")),
            ScriptStep(response=LLMResponse(content="two")),
        ]
    )
    assert (await llm.complete(USER)).content == "one"
    assert (await llm.complete(USER)).content == "two"
    assert llm.remaining == 0


async def test_supports_tool_call_responses() -> None:
    call = ToolCall(id="c1", name="search_logs", arguments={"q": "error"})
    llm = FakeLLM([ScriptStep(response=LLMResponse(tool_calls=[call]))])
    resp = await llm.complete(USER)
    assert resp.tool_calls[0].name == "search_logs"
    assert resp.tool_calls[0].arguments == {"q": "error"}


async def test_exhausted_script_fails_loudly() -> None:
    llm = FakeLLM([])
    with pytest.raises(ScriptExhaustedError):
        await llm.complete(USER)


async def test_expect_in_prompt_mismatch_fails_loudly() -> None:
    llm = FakeLLM([ScriptStep(response=LLMResponse(content="x"), expect_in_prompt="benchmark")])
    with pytest.raises(UnexpectedPromptError, match="benchmark"):
        await llm.complete(USER)


async def test_expect_in_prompt_match_passes() -> None:
    llm = FakeLLM([ScriptStep(response=LLMResponse(content="ok"), expect_in_prompt="regression")])
    assert (await llm.complete(USER)).content == "ok"


async def test_records_calls_and_ignores_system_for_matching() -> None:
    llm = FakeLLM([ScriptStep(response=LLMResponse(content="ok"), expect_in_prompt="regression")])
    msgs = [Message(role="system", content="you are an agent"), *USER]
    await llm.complete(msgs)
    assert llm.calls == [msgs]


async def test_returned_response_is_a_copy() -> None:
    step = ScriptStep(response=LLMResponse(content="a"))
    llm = FakeLLM([step])
    resp = await llm.complete(USER)
    resp.content = "mutated"
    assert step.response.content == "a"
```

`tests/test_no_network.py`:
```python
import socket

import pytest

from aeos.llm.base import LLMResponse, Message
from aeos.llm.fake import FakeLLM, ScriptStep


async def test_fake_llm_makes_zero_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    llm = FakeLLM([ScriptStep(response=LLMResponse(content="hi"))])
    assert (await llm.complete([Message(role="user", content="hello")])).content == "hi"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/llm tests/test_no_network.py -v`
Expected: FAIL (modules missing).

- [ ] **Step 3: Implement `base.py`**

```python
from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class LLMResponse(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    model: str = ""


class LLMError(Exception):
    """Base class for provider errors."""


class LLMProvider(ABC):
    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse: ...
```

- [ ] **Step 4: Implement `fake.py`**

```python
from pydantic import BaseModel

from aeos.llm.base import LLMError, LLMProvider, LLMResponse, Message, ToolSpec


class ScriptStep(BaseModel):
    response: LLMResponse
    expect_in_prompt: str | None = None


class ScriptExhaustedError(LLMError):
    pass


class UnexpectedPromptError(LLMError):
    pass


class FakeLLM(LLMProvider):
    """Scripted, deterministic provider. Replies in order; fails loudly on drift."""

    def __init__(self, script: list[ScriptStep]) -> None:
        self._script = list(script)
        self._index = 0
        self.calls: list[list[Message]] = []

    @property
    def remaining(self) -> int:
        return len(self._script) - self._index

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        self.calls.append(messages)
        if self._index >= len(self._script):
            raise ScriptExhaustedError(
                f"FakeLLM script exhausted after {len(self._script)} step(s)"
            )
        step = self._script[self._index]
        if step.expect_in_prompt is not None:
            last = next((m.content for m in reversed(messages) if m.role != "system"), "")
            if step.expect_in_prompt not in last:
                raise UnexpectedPromptError(
                    f"step {self._index}: expected {step.expect_in_prompt!r} in prompt, got {last!r}"
                )
        self._index += 1
        return step.response.model_copy(deep=True)
```

`src/aeos/llm/__init__.py`:
```python
from aeos.llm.base import (
    LLMError,
    LLMProvider,
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
    Usage,
)

__all__ = ["LLMError", "LLMProvider", "LLMResponse", "Message", "ToolCall", "ToolSpec", "Usage"]
```

- [ ] **Step 5: Run tests and `make check`**

Run: `uv run pytest -v && make check`
Expected: all pass (≈20 tests); lint and strict mypy green.

- [ ] **Step 6: Commit**

```bash
git add src/aeos/llm tests/llm tests/test_no_network.py
git commit -m "feat(llm): add provider interface and scripted FakeLLM"
```

---

### Task 5: Docker Compose infra and health check

**Files:**
- Create: `docker-compose.yml`, `infra/otel-collector.yaml`, `scripts/check_infra.py`
- Test: `tests/test_check_infra.py` (unit-tests the parsing logic only; the real check is the acceptance run)

**Interfaces:**
- Produces: services `postgres` (`pgvector/pgvector:pg16`, db/user/pass `aeos`, port 5432), `redis` (`redis:7-alpine`, 6379), `otel-collector` (OTLP gRPC 4317 / HTTP 4318, debug exporter for now), all with healthchecks. Profile `observability`: Langfuse stack. `make up` waits for health (`docker compose up -d --wait`).
- `scripts/check_infra.py`: `unhealthy(ps_json_lines: str) -> list[str]` (pure function, unit-tested) plus a `main()` that runs `docker compose ps --format json` and exits non-zero listing unhealthy/stopped services.

- [ ] **Step 1: Write `docker-compose.yml` (core services)**

```yaml
name: aeos

services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: aeos
      POSTGRES_PASSWORD: aeos
      POSTGRES_DB: aeos
    ports: ["5432:5432"]
    volumes: [pgdata:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U aeos -d aeos"]
      interval: 5s
      timeout: 3s
      retries: 10

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10

  otel-collector:
    image: otel/opentelemetry-collector-contrib:0.104.0
    command: ["--config=/etc/otel/config.yaml"]
    volumes: ["./infra/otel-collector.yaml:/etc/otel/config.yaml:ro"]
    ports: ["4317:4317", "4318:4318", "13133:13133"]
    # The collector image is distroless (no shell/curl): healthcheck is
    # added in Step 3 only if a supported probe exists; otherwise it is
    # verified by check_infra.py via the running state + port 13133.

volumes:
  pgdata:
```

`infra/otel-collector.yaml`:
```yaml
receivers:
  otlp:
    protocols:
      grpc: { endpoint: 0.0.0.0:4317 }
      http: { endpoint: 0.0.0.0:4318 }
extensions:
  health_check: { endpoint: 0.0.0.0:13133 }
exporters:
  debug: { verbosity: basic }
service:
  extensions: [health_check]
  pipelines:
    traces: { receivers: [otlp], exporters: [debug] }
    metrics: { receivers: [otlp], exporters: [debug] }
    logs: { receivers: [otlp], exporters: [debug] }
```

- [ ] **Step 2: Add Langfuse under profile `observability`**

Fetch the official compose for a pinned Langfuse v3 tag and copy its services (`langfuse-web`, `langfuse-worker`, `clickhouse`, `minio`, `redis`→rename `langfuse-redis` to avoid clashing with ours, `postgres`→`langfuse-db`) into `docker-compose.yml`:

```bash
curl -fsSL https://raw.githubusercontent.com/langfuse/langfuse/v3/docker-compose.yml -o /tmp/langfuse-compose.yml
```

Rules when merging:
- Add `profiles: ["observability"]` to every Langfuse service.
- Rename its Postgres/Redis to `langfuse-db` / `langfuse-redis`; keep its own volumes; do not publish host ports other than web `3000`.
- Replace every `CHANGEME` secret with an env var reference having a dev default in `.env.example` (`LANGFUSE_*`); pin images to explicit tags (no `latest`).
- Keep its healthchecks; add one for `langfuse-web` (`wget -qO- http://localhost:3000/api/public/health`) if absent.
- Add `LANGFUSE_INIT_*` env vars so a project and API keys exist on first boot (needed later by Phase 10 without manual UI steps).

Reason for profile: Langfuse v3 needs ClickHouse+MinIO, which is heavy; core dev loop shouldn't pay for it. Log as D-002 in DECISIONS.md.

- [ ] **Step 3: Write the failing unit test for the health parser**

`tests/test_check_infra.py`:
```python
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_infra import unhealthy  # noqa: E402


def line(service: str, state: str, health: str = "") -> str:
    return json.dumps({"Service": service, "State": state, "Health": health})


def test_all_healthy() -> None:
    out = "\n".join([line("postgres", "running", "healthy"), line("redis", "running", "healthy")])
    assert unhealthy(out) == []


def test_flags_unhealthy_and_stopped() -> None:
    out = "\n".join(
        [
            line("postgres", "running", "unhealthy"),
            line("redis", "exited"),
            line("otel-collector", "running"),  # no healthcheck: running is enough
        ]
    )
    assert unhealthy(out) == ["postgres", "redis"]


def test_empty_output_means_nothing_running() -> None:
    assert unhealthy("") == ["<no services running>"]
```

Run: `uv run pytest tests/test_check_infra.py -v` → Expected: FAIL (module missing).

- [ ] **Step 4: Implement `scripts/check_infra.py`**

```python
"""Exit non-zero if any compose service is not running/healthy."""

import json
import subprocess
import sys


def unhealthy(ps_json_lines: str) -> list[str]:
    rows = [json.loads(x) for x in ps_json_lines.splitlines() if x.strip()]
    if not rows:
        return ["<no services running>"]
    bad = []
    for r in rows:
        healthy = r.get("Health", "") in ("", "healthy")
        if r.get("State") != "running" or not healthy:
            bad.append(r["Service"])
    return bad


def main() -> int:
    out = subprocess.run(
        ["docker", "compose", "ps", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        print(f"docker compose failed: {out.stderr.strip()}", file=sys.stderr)
        return 2
    bad = unhealthy(out.stdout)
    if bad:
        print(f"UNHEALTHY: {', '.join(bad)}", file=sys.stderr)
        return 1
    print("all services healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Run: `uv run pytest tests/test_check_infra.py -v` → Expected: 3 passed.

- [ ] **Step 5: Bring the stack up for real**

```bash
cp .env.example .env
make up                            # core services
docker compose --profile observability up -d --wait
make infra-check
```
Expected: `all services healthy`; Langfuse UI reachable at http://localhost:3000. Also verify pgvector: `docker compose exec postgres psql -U aeos -c "CREATE EXTENSION IF NOT EXISTS vector; SELECT extversion FROM pg_extension WHERE extname='vector';"` prints a version.

If `otel-collector` shows no Health (distroless), confirm with `curl -fsS localhost:13133` instead and note it in DECISIONS.md (D-003).

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml infra scripts tests/test_check_infra.py
git commit -m "feat(infra): compose stack with postgres+pgvector, redis, otel collector, langfuse profile"
```

---

### Task 6: GitHub Actions CI

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `make lint`, `make typecheck`, `make test` from Task 1.

- [ ] **Step 1: Write the workflow**

```yaml
name: ci
on:
  push: { branches: [main] }
  pull_request:

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with: { enable-cache: true }
      - run: uv sync --frozen
      - run: make lint
      - run: make typecheck
      - run: make test
```

(No services and no LLM keys: unit tests must pass with zero network, which `tests/test_no_network.py` already guards for the FakeLLM path.)

- [ ] **Step 2: Verify locally, commit**

Run: `uv lock && make check` (commit `uv.lock`; `--frozen` in CI requires it).
Optional local CI run if `act` is installed: `act -j check`.

```bash
git add .github uv.lock
git commit -m "ci: add lint, typecheck and unit-test workflow"
```

- [ ] **Step 3 (when a remote exists): push and confirm the run is green**

Run: `gh run watch` after pushing. If no remote is configured yet, note "CI unverified remotely" in PROGRESS.md rather than claiming green.

---

### Task 7: Phase 0 acceptance and report

**Files:**
- Modify: `docs/PROGRESS.md`, `docs/DECISIONS.md`

- [ ] **Step 1: Run every acceptance check from a clean state and capture output**

```bash
docker compose --profile observability down && docker compose --profile observability up -d --wait
make infra-check                    # expect: all services healthy
make check                          # expect: ruff, mypy strict, all tests pass
uv run pytest tests/test_no_network.py -v   # expect: passed (zero network)
```

- [ ] **Step 2: Update docs**

In `docs/PROGRESS.md` replace the Phase 0 section with: built (list), deferred (real Anthropic/OpenAI providers → start of Phase 4 plan; remote CI verification if no remote), known issues, decisions made. Append to `docs/DECISIONS.md`: D-002 (Langfuse in `observability` profile), D-003 (OTel collector health probe approach), D-004 (provider implementations deferred to Phase 4, reason: need the tool-call loop to shape and live keys to verify).

- [ ] **Step 3: Commit and report**

```bash
git add docs
git commit -m "docs: record phase 0 results and decisions"
```

Report the actual command output for the three acceptance checks to the user before proposing Phase 1. Do not start Phase 1 until the user confirms.

---

## Self-Review

- **Spec coverage (Phase 0):** uv/ruff/mypy-strict-on-core/pytest → T1. Compose w/ Postgres+pgvector, Redis, Langfuse (+deps), OTel → T5. Pydantic Settings + `.env.example` → T2. JSON logging w/ task_id/run_id/agent → T3. LLM interface + FakeLLM (scripted by step, tool-call outputs) → T4. GitHub Actions → T6. Acceptance (compose healthy, `make test`, zero-network FakeLLM test, CI) → T5/T7/T4. "Start here" actions (PROGRESS summary, DECISIONS tentative LangGraph entry) → T1. Gap: roadmap says LLM interface needs ≥2 provider implementations; explicitly deferred to Phase 4 (recorded as D-004), flag to user.
- **Placeholders:** Langfuse service block (T5 Step 2) is derived from upstream's official file by explicit merge rules instead of inlined, because its exact env surface changes by version; the executor must pin a tag and verify the stack boots.
- **Type consistency:** `LLMResponse`/`Message`/`ToolCall`/`ScriptStep` names match across T4 tests and implementation; `run_context`/`configure_logging(level, *, stream=)` match T3 tests; `unhealthy()` matches T5.
