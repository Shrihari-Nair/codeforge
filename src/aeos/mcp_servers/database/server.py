"""Read-only database MCP server: schema, explain, and guarded SELECT queries."""

from typing import Any

import psycopg
from mcp.server.mcpserver import MCPServer
from psycopg import errors as pg_errors
from pydantic import BaseModel

from aeos.core.config import get_settings
from aeos.mcp_servers.common import fail, tool_kwargs
from aeos.mcp_servers.database.guard import GuardError, validate_select

mcp = MCPServer("aeos-database")

MAX_ROWS = 500
DEFAULT_ROWS = 100
STATEMENT_TIMEOUT = "5s"
SCHEMAS = (get_settings().demo_schema,)  # schemas exposed to agents


class Column(BaseModel):
    name: str
    type: str
    nullable: bool


class Table(BaseModel):
    name: str
    columns: list[Column]
    indexes: list[str]
    estimated_rows: int


class SchemaInfo(BaseModel):
    schema_name: str
    tables: list[Table]


class ExplainResult(BaseModel):
    plan: dict[str, Any]
    total_cost: float


class QueryResult(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool


def _connect() -> psycopg.Connection[Any]:
    return psycopg.connect(
        get_settings().readonly_database_url,
        options=f"-c statement_timeout={STATEMENT_TIMEOUT} -c default_transaction_read_only=on",
    )


def _guard(sql: str) -> None:
    try:
        validate_select(sql)
    except GuardError as exc:
        fail(exc.code, exc.message)


@mcp.tool(**tool_kwargs("read"))
def get_schema(schema: str = SCHEMAS[0]) -> SchemaInfo:
    """List tables, columns, indexes and row estimates for the telemetry schema."""
    if schema not in SCHEMAS:
        fail("invalid_argument", f"unknown schema {schema!r}", allowed=list(SCHEMAS))
    with _connect() as conn:
        cols = conn.execute(
            "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns"
            " WHERE table_schema = %s ORDER BY table_name, ordinal_position",
            (schema,),
        ).fetchall()
        idx = conn.execute(
            "SELECT tablename, indexdef FROM pg_indexes WHERE schemaname = %s ORDER BY 1, 2",
            (schema,),
        ).fetchall()
        est = dict(
            conn.execute(
                "SELECT c.relname, c.reltuples::bigint FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s"
                " AND c.relkind = 'r'",
                (schema,),
            ).fetchall()
        )
    tables: dict[str, Table] = {}
    for table, column, dtype, nullable in cols:
        t = tables.setdefault(table, Table(name=table, columns=[], indexes=[], estimated_rows=0))
        t.columns.append(Column(name=column, type=dtype, nullable=nullable == "YES"))
    for table, indexdef in idx:
        if table in tables:
            tables[table].indexes.append(indexdef)
    for name, t in tables.items():
        t.estimated_rows = max(int(est.get(name, 0)), 0)
    return SchemaInfo(schema_name=schema, tables=list(tables.values()))


@mcp.tool(**tool_kwargs("read"))
def explain_query(sql: str) -> ExplainResult:
    """Show the Postgres plan for a SELECT without executing it (no ANALYZE)."""
    _guard(sql)
    try:
        with _connect() as conn:
            row = conn.execute(f"EXPLAIN (FORMAT JSON) {sql}").fetchone()  # noqa: S608 (guarded)
    except psycopg.Error as exc:
        fail("db_error", str(exc).splitlines()[0])
    assert row is not None
    plan = row[0][0]["Plan"]
    return ExplainResult(plan=plan, total_cost=float(plan["Total Cost"]))


@mcp.tool(**tool_kwargs("read"))
def query_database(sql: str, limit: int = DEFAULT_ROWS) -> QueryResult:
    """Run one read-only SELECT. Rows are capped (max 500) and queries time out after 5s."""
    if not 1 <= limit <= MAX_ROWS:
        fail("invalid_argument", f"limit must be between 1 and {MAX_ROWS}")
    _guard(sql)
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(sql)
            columns = [d.name for d in cur.description or []]
            fetched = cur.fetchmany(limit + 1)
    except pg_errors.QueryCanceled:
        fail("timeout", f"query exceeded {STATEMENT_TIMEOUT}")
    except psycopg.Error as exc:
        fail("db_error", str(exc).splitlines()[0])
    truncated = len(fetched) > limit
    rows = [list(r) for r in fetched[:limit]]
    return QueryResult(columns=columns, rows=rows, row_count=len(rows), truncated=truncated)
