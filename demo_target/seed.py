"""Create the demo schema and load generated observability data into Postgres.

Idempotent: truncates and reloads in a single transaction.
Usage: uv run python -m demo_target.seed
"""

import psycopg
from psycopg.types.json import Jsonb

from aeos.core.config import get_settings
from demo_target.build_repo import load_commits
from demo_target.datagen import Dataset, generate

_DDL_TEMPLATE = """
CREATE SCHEMA IF NOT EXISTS demo;
CREATE TABLE IF NOT EXISTS demo.deployments (
    ts timestamptz NOT NULL, service text NOT NULL, version text NOT NULL,
    commit_sha text NOT NULL);
CREATE TABLE IF NOT EXISTS demo.logs (
    ts timestamptz NOT NULL, level text NOT NULL, service text NOT NULL,
    trace_id text, message text NOT NULL, attrs jsonb NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS demo.metrics (
    ts timestamptz NOT NULL, service text NOT NULL, p50_ms double precision,
    p95_ms double precision, p99_ms double precision, error_rate double precision,
    rps double precision);
CREATE TABLE IF NOT EXISTS demo.spans (
    trace_id text NOT NULL, span_id text NOT NULL, parent_id text, name text NOT NULL,
    kind text NOT NULL, start_ts timestamptz NOT NULL, duration_ms double precision NOT NULL,
    attrs jsonb NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS logs_ts_idx ON demo.logs (ts);
CREATE INDEX IF NOT EXISTS logs_trace_idx ON demo.logs (trace_id);
CREATE INDEX IF NOT EXISTS metrics_ts_idx ON demo.metrics (ts);
CREATE INDEX IF NOT EXISTS spans_trace_idx ON demo.spans (trace_id);
CREATE INDEX IF NOT EXISTS spans_start_idx ON demo.spans (start_ts);

-- Read-only role used by the observability and database MCP servers (defense in depth:
-- even if the SQL guard were bypassed, the database refuses writes).
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'aeos_readonly') THEN
        CREATE ROLE aeos_readonly LOGIN PASSWORD 'aeos_readonly';
    END IF;
END $$;
ALTER ROLE aeos_readonly SET default_transaction_read_only = on;
GRANT USAGE ON SCHEMA demo TO aeos_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA demo TO aeos_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA demo GRANT SELECT ON TABLES TO aeos_readonly;
"""


def ddl(schema: str = "demo") -> str:
    """DDL for one scenario's schema (each eval scenario gets its own, so runs are isolated)."""
    import re

    if not re.fullmatch(r"[a-z][a-z0-9_]{0,60}", schema):
        raise ValueError(f"bad schema name {schema!r}")
    return (
        _DDL_TEMPLATE.replace("demo.", f"{schema}.")
        .replace("CREATE SCHEMA IF NOT EXISTS demo;", f"CREATE SCHEMA IF NOT EXISTS {schema};")
        .replace("SCHEMA demo", f"SCHEMA {schema}")
    )


DDL = ddl("demo")

_COLUMNS = {
    "deployments": ["ts", "service", "version", "commit_sha"],
    "logs": ["ts", "level", "service", "trace_id", "message", "attrs"],
    "metrics": ["ts", "service", "p50_ms", "p95_ms", "p99_ms", "error_rate", "rps"],
    "spans": [
        "trace_id",
        "span_id",
        "parent_id",
        "name",
        "kind",
        "start_ts",
        "duration_ms",
        "attrs",
    ],
}


def load(conn: psycopg.Connection, data: Dataset, schema: str = "demo") -> dict[str, int]:
    counts: dict[str, int] = {}
    with conn.transaction():
        conn.execute(
            "SELECT pg_advisory_xact_lock(7731)"
        )  # role grants race when seeding concurrently
        conn.execute(ddl(schema))
        for table, cols in _COLUMNS.items():
            conn.execute(f"TRUNCATE {schema}.{table}")  # noqa: S608 (validated schema, static table)
            placeholders = ", ".join(["%s"] * len(cols))
            rows = [tuple(Jsonb(r[c]) if c == "attrs" else r[c] for c in cols) for r in data[table]]
            with conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO {schema}.{table} ({', '.join(cols)}) VALUES ({placeholders})",
                    rows,
                )
            counts[table] = len(rows)
    return counts


def main() -> None:
    data = generate(load_commits())
    with psycopg.connect(get_settings().database_url) as conn:
        for table, n in load(conn, data).items():
            print(f"demo.{table}: {n} rows")


if __name__ == "__main__":
    main()
