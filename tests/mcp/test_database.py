import psycopg
import pytest

from aeos.core.config import get_settings
from aeos.mcp_client import McpClient, McpToolError


async def test_tools_are_read_risk(database_client: McpClient) -> None:
    tools = await database_client.list_tools()
    assert set(tools) == {"get_schema", "explain_query", "query_database"}
    assert all(t.risk == "read" for t in tools.values())


async def test_get_schema_describes_demo_tables(database_client: McpClient) -> None:
    data = (await database_client.call("get_schema")).data
    assert data is not None
    tables = {t["name"]: t for t in data["tables"]}
    assert {"deployments", "logs", "metrics", "spans"} <= set(tables)
    assert {c["name"] for c in tables["metrics"]["columns"]} >= {"ts", "p95_ms"}
    assert any("metrics_ts_idx" in i for i in tables["metrics"]["indexes"])


async def test_get_schema_rejects_unknown_schema(database_client: McpClient) -> None:
    with pytest.raises(McpToolError) as ei:
        await database_client.call("get_schema", {"schema": "pg_catalog"})
    assert ei.value.code == "invalid_argument"


async def test_query_returns_rows(database_client: McpClient) -> None:
    res = await database_client.call(
        "query_database", {"sql": "SELECT version FROM demo.deployments ORDER BY ts", "limit": 10}
    )
    assert res.data is not None
    assert [r[0] for r in res.data["rows"]] == ["v1.0.0", "v1.1.0", "v1.2.0"]
    assert res.data["truncated"] is False


async def test_row_limit_truncates(database_client: McpClient) -> None:
    res = await database_client.call(
        "query_database", {"sql": "SELECT ts FROM demo.metrics", "limit": 7}
    )
    assert res.data is not None
    assert res.data["row_count"] == 7 and res.data["truncated"] is True


async def test_limit_bounds_enforced(database_client: McpClient) -> None:
    with pytest.raises(McpToolError) as ei:
        await database_client.call("query_database", {"sql": "SELECT 1", "limit": 100000})
    assert ei.value.code == "invalid_argument"


async def test_explain_does_not_execute_and_returns_plan(database_client: McpClient) -> None:
    res = await database_client.call(
        "explain_query", {"sql": "SELECT * FROM demo.logs WHERE trace_id = 'abc'"}
    )
    assert res.data is not None
    assert res.data["total_cost"] > 0
    assert "Node Type" in res.data["plan"]


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE demo.logs",
        "UPDATE demo.metrics SET p95_ms = 0",
        "SELECT 1; DROP TABLE demo.logs",
        "WITH d AS (DELETE FROM demo.logs RETURNING *) SELECT * FROM d",
        "EXPLAIN ANALYZE DELETE FROM demo.logs",
        "SELECT pg_sleep(30)",
    ],
)
async def test_write_attempts_are_rejected_and_data_survives(
    database_client: McpClient, sql: str
) -> None:
    with pytest.raises(McpToolError) as ei:
        await database_client.call("query_database", {"sql": sql})
    assert ei.value.code in {"forbidden_statement", "forbidden_function"}
    count = await database_client.call("query_database", {"sql": "SELECT count(*) FROM demo.logs"})
    assert count.data is not None and count.data["rows"][0][0] > 1000


async def test_explain_of_write_is_rejected(database_client: McpClient) -> None:
    with pytest.raises(McpToolError) as ei:
        await database_client.call("explain_query", {"sql": "DELETE FROM demo.logs"})
    assert ei.value.code == "forbidden_statement"


async def test_slow_query_times_out(database_client: McpClient) -> None:
    sql = "SELECT count(*) FROM generate_series(1, 400000000) g"
    with pytest.raises(McpToolError) as ei:
        await database_client.call("query_database", {"sql": sql}, timeout=20)
    assert ei.value.code == "timeout"


async def test_db_error_is_structured_not_a_crash(database_client: McpClient) -> None:
    with pytest.raises(McpToolError) as ei:
        await database_client.call("query_database", {"sql": "SELECT * FROM demo.nope"})
    assert ei.value.code == "db_error"


def test_database_role_itself_refuses_writes(seeded_db: None) -> None:
    """Layer 2: even a guard bypass is stopped by the read-only role."""
    with psycopg.connect(get_settings().readonly_database_url) as conn:
        for stmt in (
            "DELETE FROM demo.logs",
            "DROP TABLE demo.logs",
            "UPDATE demo.metrics SET rps=0",
        ):
            with pytest.raises(psycopg.Error):
                conn.execute(stmt)
            conn.rollback()
