import psycopg
import pytest
from demo_target.build_repo import build
from demo_target.datagen import generate
from demo_target.seed import load

from aeos.core.config import get_settings


@pytest.fixture
def conn():  # type: ignore[no-untyped-def]
    try:
        c = psycopg.connect(get_settings().database_url, connect_timeout=2)
    except psycopg.OperationalError:
        pytest.skip("Postgres not available")
    yield c
    c.close()


def test_load_is_idempotent_and_queryable(conn) -> None:  # type: ignore[no-untyped-def]
    data = generate(build())
    first = load(conn, data)
    second = load(conn, data)
    assert first == second
    (n,) = conn.execute("SELECT count(*) FROM demo.metrics").fetchone()  # type: ignore[misc]
    assert n == 1440
    (v,) = conn.execute("SELECT version FROM demo.deployments ORDER BY ts DESC LIMIT 1").fetchone()  # type: ignore[misc]
    assert v == "v1.2.0"
    (hits,) = conn.execute(
        "SELECT count(*) FROM demo.spans WHERE attrs->>'db.statement' LIKE '%products WHERE id = ?'"
    ).fetchone()  # type: ignore[misc]
    assert hits > 0
