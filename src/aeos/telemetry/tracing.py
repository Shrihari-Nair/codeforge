"""Tracing: OpenTelemetry spans behind the `traced()` seam that every layer already uses.

Span schema (documented in docs/ARCHITECTURE.md):
  task / graph.node / plan.* / agent / llm.call / tool.call / mcp.call / sandbox.exec /
  verifier.gates / approval.wait / memory.*
One trace per task, even across crashes, workers and A2A/MCP processes: the root span context is
stored in the task state and re-attached by every node.
"""

import json
import logging
import re
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import metrics, propagate, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

log = logging.getLogger("aeos.trace")

_TRACER_NAME = "aeos"
_CONTROL_FLOW = {"GraphInterrupt", "GraphBubbleUp", "ParentCommand", "CancelledError"}
MAX_ATTR_CHARS = 4000

# Same families the memory/verifier gates look for; telemetry must never carry secrets.
_FULL_REDACTIONS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----|$)"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
]
_KEY_VALUE = re.compile(
    r"(?i)((?:api[_-]?key|secret|token|passwd|password)['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]{6,}"
)
_secret_values: list[str] = []
_task_id: ContextVar[str | None] = ContextVar("aeos_task_id", default=None)


def register_secrets(values: list[str]) -> None:
    """Exact secret strings (API keys...) that must never appear in a span, log or trace."""
    for v in values:
        if v and v not in _secret_values:
            _secret_values.append(v)


def redact(text: str) -> str:
    for secret in _secret_values:
        text = text.replace(secret, "[REDACTED]")
    for pattern in _FULL_REDACTIONS:
        text = pattern.sub("[REDACTED]", text)
    return _KEY_VALUE.sub(r"\1[REDACTED]", text)


def _coerce(value: Any) -> Any:
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=str)
        except (TypeError, ValueError):
            text = str(value)
    text = redact(text)
    return text if len(text) <= MAX_ATTR_CHARS else text[:MAX_ATTR_CHARS] + "…[truncated]"


class SpanHandle(dict[str, Any]):
    """Yielded by `traced()`. Dict-compatible (old call sites) plus `.set()` for attributes."""

    def __init__(self, span: Span) -> None:
        super().__init__()
        self.span = span

    def set(self, key: str, value: Any) -> None:
        self.span.set_attribute(key, _coerce(value))
        self[key] = value

    def event(self, name: str, **attrs: Any) -> None:
        self.span.add_event(name, {k: _coerce(v) for k, v in attrs.items()})


@contextmanager
def bind_task(task_id: str) -> Iterator[None]:
    """Every span started inside carries the task id (and the Langfuse session id)."""
    token = _task_id.set(task_id)
    try:
        yield
    finally:
        _task_id.reset(token)


def annotate(**attrs: Any) -> None:
    """Add attributes to the currently active span (no-op without one)."""
    span = trace.get_current_span()
    for k, v in attrs.items():
        if v is not None:
            span.set_attribute(k, _coerce(v))


def tracer() -> trace.Tracer:
    return trace.get_tracer(_TRACER_NAME)


@contextmanager
def traced(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: Mapping[str, Any] | None = None,
    **attrs: Any,
) -> Iterator[SpanHandle]:
    start = time.perf_counter()
    attrs = {**(attributes or {}), **attrs}
    if (task := _task_id.get()) is not None:
        attrs = {"aeos.task.id": task, "langfuse.session.id": task, **attrs}
    with tracer().start_as_current_span(
        name, kind=kind, attributes={k: _coerce(v) for k, v in attrs.items() if v is not None},
        record_exception=False, set_status_on_exception=False,
    ) as span:  # fmt: skip
        handle = SpanHandle(span)
        try:
            yield handle
        except BaseException as exc:
            if (
                type(exc).__name__ in _CONTROL_FLOW
            ):  # an approval pause / cancellation is not a failure
                span.add_event("interrupted", {"reason": type(exc).__name__})
                raise
            span.set_attribute("error.type", type(exc).__name__)
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, redact(str(exc))[:500]))
            handle["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            level = logging.WARNING if "error" in handle else logging.DEBUG
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            extra = {k: str(v)[:200] for k, v in handle.items()}
            log.log(level, name, extra={"span": name, "duration_ms": duration_ms, **extra})


def emit_span(
    name: str, start_s: float, end_s: float, parent: Mapping[str, str] | None = None, **attrs: Any
) -> None:
    """A span with explicit wall-clock bounds (e.g. an approval wait that spanned a restart)."""
    ctx = propagate.extract(dict(parent)) if parent else None
    if (task := _task_id.get()) is not None:
        attrs = {"aeos.task.id": task, "langfuse.session.id": task, **attrs}
    span = tracer().start_span(
        name, context=ctx, start_time=int(start_s * 1e9),
        attributes={k: _coerce(v) for k, v in attrs.items() if v is not None},
    )  # fmt: skip
    span.end(end_time=int(end_s * 1e9))


# ------------------------------------------------------------------ context propagation
def inject_context() -> dict[str, str]:
    """W3C trace context of the current span, for A2A metadata / stored task state."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


@contextmanager
def remote_context(carrier: Mapping[str, str] | None) -> Iterator[None]:
    """Make spans started inside this block children of a span from another process/time."""
    if not carrier:
        yield
        return
    token = otel_context.attach(propagate.extract(dict(carrier)))
    try:
        yield
    finally:
        otel_context.detach(token)


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return f"{ctx.trace_id:032x}" if ctx.is_valid else None


# ------------------------------------------------------------------ setup
_configured = False


def setup_telemetry(
    service: str,
    *,
    endpoint: str | None = None,
    exporter: SpanExporter | None = None,
    processor: SpanProcessor | None = None,
    metrics_enabled: bool = True,
) -> TracerProvider:
    """Configure tracing/metrics for this process. Safe to call once per process.

    `endpoint` = OTLP gRPC collector (e.g. http://localhost:4317). Tests pass an in-memory exporter.
    """
    global _configured
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    if processor is not None:
        provider.add_span_processor(processor)
    elif exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    elif endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
        )
    trace.set_tracer_provider(provider)  # first call wins in a process; tests use reset_for_tests()
    if metrics_enabled and endpoint and exporter is None and processor is None:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint, insecure=True),
                                               export_interval_millis=10_000)  # fmt: skip
        metrics.set_meter_provider(
            MeterProvider(resource=provider.resource, metric_readers=[reader])
        )
    _configured = True
    return provider


def is_configured() -> bool:
    return _configured


def reset_for_tests() -> None:  # pragma: no cover - test helper
    """OTel only lets the global provider be set once; tests swap the internal reference."""
    global _configured
    trace._TRACER_PROVIDER_SET_ONCE._done = False  # noqa: SLF001
    trace._TRACER_PROVIDER = None  # noqa: SLF001
    _configured = False
