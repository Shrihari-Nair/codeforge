# Phase 3: Sandbox Executor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** Safe, ephemeral, resource-limited code execution in Docker, with deterministic test/benchmark helpers and an orphan reaper.

**Architecture:** `aeos.sandbox` (sync API; async callers use `asyncio.to_thread`). One container per sandbox from a local pinned image `aeos-sandbox:1` (python:3.12-slim + git + pytest). Container hardening: network none, read-only rootfs, tmpfs workspace (`/workspace`, uid 10001) and `/tmp`, non-root, all caps dropped, no-new-privileges, memory/CPU/pids limits, init process. Commands are argv lists (no shell) checked against an allowlist as defense-in-depth; the container, not the filter, is the security boundary. Wall-clock timeouts enforced via `timeout` in-container plus a host-side deadline and kill-all cleanup; if cleanup fails (e.g. pid limit exhausted) the sandbox is destroyed and flagged. Sandboxes carry labels (`aeos.sandbox`, `aeos.task_id`, `aeos.created_at`) so a reaper can remove orphans after a crash.

**Tech Stack:** docker SDK for Python (new dependency, required by roadmap), pytest (`docker` marker, skipped when Docker unavailable).

## Global Constraints
- Agent-generated code NEVER runs on the host (including tests): all abuse tests run inside containers.
- Network disabled by default. Allowlist mode (dependency install) DEFERRED: needs an egress proxy and the demo has no dependencies (log in DECISIONS).
- Limits: 512m RAM (no swap), 1 CPU, 128 pids, 256MB workspace, 64MB /tmp, 60s default exec timeout, 1MB output cap, 30min TTL.
- Always cleaned up: context manager + reaper (orphans by label / TTL / not-in-keep-set).

## Review Focus
- Fork bomb exhausting pids (cleanup exec itself can fail) -> sandbox destroyed, error surfaced, host unaffected.
- Output flood (10MB) -> capped + `truncated`, no host memory blowup.
- Path traversal in read/write/apply_patch (`../`, absolute, symlink in workspace pointing out).
- Patch touching files outside workspace / `.git`.
- Sandbox used after timeout or after destroy -> clear error or still-working.
- Two sandboxes from different refs do not share state.

## Tasks
1. Image + policy (pure unit tests for command policy) + deps/markers.
2. SandboxManager: create (from git ref or files), exec with limits/timeouts/cap, read/write/apply_patch/snapshot_diff, destroy, context manager.
3. Abuse/escape tests (fork bomb, memory hog, infinite loop, egress, write outside, host path read, non-root, caps, output flood, traversal).
4. Helpers: run_tests (junit parsed), run_benchmark, compare vs baseline; end-to-end on the demo repo (baseline vs regressed vs fixed).
5. Reaper (+ CLI) and crash-orphan test; docs.
