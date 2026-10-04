# Phase 2: MCP Servers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** Agents reach external systems only through four real, standalone MCP servers (GitHub-like, observability, database, web/research) plus a shared client wrapper.

**Architecture:** Each server is a separate process built on the official `mcp` SDK v2 (`MCPServer`), runnable over stdio (default) or streamable HTTP: `python -m aeos.mcp_servers.<name> [--transport streamable-http --port N]`. Tools have typed Pydantic outputs (structured content + output schema), structured errors (`ToolError` carrying `{"code","message"}` JSON), and a risk level in tool `meta["aeos"]["risk"]` (+ standard annotations) that the Phase 6 risk policy reads. Postgres-facing servers use a **read-only DB role**; the DB server additionally validates SQL with sqlglot (defense in depth). The agent-side `McpClient` wraps connection, timeouts, retries (read tools or idempotency-keyed writes only) and a `traced()` span around every call.

**Tech Stack:** `mcp` 2.x (v2 API: `mcp.server.mcpserver.MCPServer`, `mcp.client.Client`), sqlglot, httpx, psycopg 3, git CLI (bare remote).

**Spec:** ROADMAP.md Phase 2; master plan.

## Global Constraints
- Servers are separate processes; integration tests call tools **through the MCP protocol**, never by importing the tool functions.
- Every tool: typed schema, clear description, structured errors, risk metadata (`read` | `write_reversible` | `write_irreversible`).
- DB MCP: read-only, statement allowlist, row limit, timeout. Must reject `DROP TABLE`, `UPDATE`, stacked statements.
- GitHub MCP: local bare git remote + PR metadata in Postgres; interface compatible with a future real GitHub backend. Writes only to `aeos/`-prefixed branches. No merge tool exists.
- Write tools honor an `idempotency_key` (needed by Phase 7).
- Web content is untrusted: marked as such in every result (Phase 13 builds on this).
- No git on the outer project; git only against the demo bare remote under `demo_target/build/`.

## Review Focus
- SQL guard bypasses: comments/whitespace tricks, stacked statements, data-modifying CTE, `SELECT INTO`, `EXPLAIN ANALYZE DELETE`, `COPY`, dangerous functions (`pg_sleep`, `pg_read_file`, `lo_import`, `dblink`, `set_config`); DB-level read-only role must still block writes if the guard were bypassed.
- Path traversal / odd refs in `get_file`, `get_diff`, `search_code` (`../`, `--option` injection as a ref or pattern).
- Branch-name abuse in `create_branch` (`main`, `refs/heads/main`, `../`, no `aeos/` prefix, spaces).
- Replayed write call with the same idempotency key: exactly one branch/commit/PR.
- Huge outputs (diffs, logs, pages): size-capped with a `truncated` flag, never unbounded.
- Fetching disallowed hosts/schemes (`file://`, link-local metadata IPs) is refused.

## Files
```
src/aeos/mcp_servers/__init__.py
src/aeos/mcp_servers/common.py          # risk meta helpers, ToolFailure/fail(), run_server() CLI
src/aeos/mcp_servers/database/{__init__,__main__,server,guard}.py
src/aeos/mcp_servers/observability/{__init__,__main__,server}.py
src/aeos/mcp_servers/github/{__init__,__main__,server,backend,store}.py
src/aeos/mcp_servers/web/{__init__,__main__,server,corpus}.py
src/aeos/mcp_client.py                  # McpClient wrapper, ToolInfo, McpToolError
src/aeos/telemetry/{__init__,tracing}.py  # traced(): logs duration now; OTel in Phase 10
demo_target/web_corpus/*.md             # small deterministic research corpus
scripts/mcp_probe.py                    # generic client: list tools / call a tool (no agents involved)
tests/mcp/{conftest,test_client,test_database,test_observability,test_github,test_web}.py
```

## Tasks
1. **Common + client wrapper + tracing stub + config.** `common.py`, `mcp_client.py`, `telemetry/tracing.py`, settings (`readonly_database_url`, `git_remote_path`, `web_allowed_hosts`). Tests: risk meta present on listed tools; error parsing; timeout; retry only for read tools / keyed writes.
2. **Read-only DB role + Database MCP.** Role `aeos_readonly` created idempotently by `demo_target.seed` DDL. Tools `get_schema`, `explain_query`, `query_database`. sqlglot guard (single statement, SELECT/UNION/WITH-of-selects only, no INTO, function denylist), row limit, `statement_timeout`. Tests over MCP incl. DROP/UPDATE/stacked/CTE-DML/EXPLAIN ANALYZE DELETE, and a DB-level check that the role itself cannot write.
3. **Observability MCP** over `demo.*`: `search_logs`, `get_trace`, `get_metrics`, `get_error_rate`, `compare_time_windows` (p50/p95/p99, error rate, avg db_queries/request, cache hits, top log messages that changed). Tests assert the seeded step-change is visible via the tools alone.
4. **GitHub-like MCP.** `build_repo.build_remote()` makes bare remote from the demo repo; git backend (`git grep`, `show`, `log`, `diff`, clone-apply-commit-push for `commit_patch`); PR/idempotency store in schema `vcs`. Tools: `get_repository`, `search_code`, `get_file`, `list_commits`, `get_diff`, `create_branch`, `commit_patch` (addition: roadmap's list has no way to put code on a branch), `create_pull_request`, `get_pull_request`. Tests over MCP incl. idempotent replays and ref/branch abuse.
5. **Web/Research MCP.** `search_web` (pluggable provider; default local corpus search), `fetch_page` (httpx, host allowlist, size/time caps, text extraction, `untrusted: true`), `extract_information` (deterministic structured extraction: title, headings, code blocks, links, keyword sentences). Local static corpus server for tests/demo.
6. **Acceptance.** `scripts/mcp_probe.py` exercises each server with a generic client over stdio and once over streamable HTTP; full `make check`; docs updated.
