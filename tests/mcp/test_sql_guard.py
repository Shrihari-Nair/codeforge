import pytest

from aeos.mcp_servers.database.guard import GuardError, validate_select

ALLOWED = [
    "SELECT 1",
    "select * from demo.metrics limit 5",
    "SELECT a FROM t UNION ALL SELECT b FROM u",
    "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
    "SELECT ';' AS semi",  # semicolon inside a string literal is fine
    "-- note\nSELECT 1 /* trailing */",
    "SELECT date_trunc('hour', ts), avg(p95_ms) FROM demo.metrics GROUP BY 1",
    "(SELECT 1)",
]

FORBIDDEN = [
    ("DROP TABLE demo.logs", "forbidden_statement"),
    ("UPDATE demo.metrics SET p95_ms = 0", "forbidden_statement"),
    ("DELETE FROM demo.logs", "forbidden_statement"),
    ("INSERT INTO demo.logs VALUES (1)", "forbidden_statement"),
    ("TRUNCATE demo.logs", "forbidden_statement"),
    ("ALTER TABLE demo.logs ADD COLUMN x int", "forbidden_statement"),
    ("CREATE TABLE x (a int)", "forbidden_statement"),
    ("GRANT ALL ON demo.logs TO public", "forbidden_statement"),
    ("COPY demo.logs TO '/tmp/x'", "forbidden_statement"),
    ("SET ROLE postgres", "forbidden_statement"),
    ("EXPLAIN ANALYZE DELETE FROM demo.logs", "forbidden_statement"),
    ("SELECT 1; DROP TABLE demo.logs", "forbidden_statement"),  # stacked
    ("SELECT 1; SELECT 2", "forbidden_statement"),  # stacked, even if harmless
    ("/* hi */ DROP TABLE demo.logs", "forbidden_statement"),  # comment before write
    ("WITH d AS (DELETE FROM demo.logs RETURNING *) SELECT * FROM d", "forbidden_statement"),
    ("SELECT * INTO demo.copy FROM demo.logs", "forbidden_statement"),
    ("SELECT * FROM demo.logs FOR UPDATE", "forbidden_statement"),
    ("SELECT pg_sleep(30)", "forbidden_function"),
    ("SELECT pg_read_file('/etc/passwd')", "forbidden_function"),
    ("SELECT lo_import('/etc/passwd')", "forbidden_function"),
    ("SELECT set_config('default_transaction_read_only','off',false)", "forbidden_function"),
    ("SELECT nextval('s')", "forbidden_function"),
    ("SELECT query_to_xml('DELETE FROM demo.logs', true, true, '')", "forbidden_function"),
    ("SELECT x FROM (SELECT pg_sleep(1) AS x) q", "forbidden_function"),  # nested
    ("", "invalid_sql"),
    ("   ", "invalid_sql"),
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allows_read_only_selects(sql: str) -> None:
    validate_select(sql)


@pytest.mark.parametrize(("sql", "code"), FORBIDDEN)
def test_rejects_everything_else(sql: str, code: str) -> None:
    with pytest.raises(GuardError) as ei:
        validate_select(sql)
    assert ei.value.code == code
