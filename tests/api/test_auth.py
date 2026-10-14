import base64
import json
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from pydantic import SecretStr

from aeos.api.app import create_app
from aeos.api.auth import Auth, hash_password, parse_users, verify_password
from aeos.core.config import Settings, get_settings
from aeos.store import approvals, tasks

PW = "correct horse battery"


def settings(**extra: Any) -> Settings:
    base: dict[str, Any] = {
        "auth_users": f"Shrihari:{hash_password(PW)}",
        "auth_secret": SecretStr("s3cret"),
        "api_token": SecretStr("tok-123"),
    }
    base.update({k: SecretStr(v) if k == "auth_secret" else v for k, v in extra.items()})
    return get_settings().model_copy(update=base)


def make_client(s: Settings) -> Iterator[TestClient]:
    @asynccontextmanager
    async def runtime() -> AsyncIterator[Any]:
        yield SimpleNamespace(sandbox=SimpleNamespace(reap_task=lambda _t: None))

    @asynccontextmanager
    async def saver() -> AsyncIterator[Any]:
        yield MemorySaver()

    app = create_app(
        settings=s, runtime_factory=runtime, checkpointer_factory=saver, embedded_worker=False
    )
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def schema(seeded_db: None) -> None:
    tasks.ensure_schema()
    approvals.ensure_schema()
    with psycopg.connect(get_settings().database_url) as conn:
        conn.execute("DELETE FROM aeos.tasks")


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield from make_client(settings())


def login(c: TestClient, pw: str = PW, name: str = "Shrihari") -> Any:
    return c.post("/auth/login", json={"username": name, "password": pw})


# ---------------------------------------------------------------- passwords and sessions
def test_password_hash_roundtrip_and_bad_entries() -> None:
    h = hash_password(PW)
    assert verify_password(PW, h) and not verify_password("nope", h)
    assert h != hash_password(PW)  # salted
    assert not verify_password(PW, "garbage") and not verify_password(PW, "md5.00.00")
    assert list(parse_users(f"a:{h},b:{h}")) == ["a", "b"]
    with pytest.raises(ValueError):
        parse_users("alice:plaintext")


def test_session_cookie_rejects_tampering_and_expiry() -> None:
    auth = Auth(settings())
    good = auth.make_session("Shrihari")
    assert auth.read_session(good) == "Shrihari"
    b64, sig = good.split(".")
    forged = base64.urlsafe_b64encode(json.dumps({"u": "Shrihari", "exp": 9_999_999_999}).encode())
    assert auth.read_session(f"{forged.decode()}.{sig}") is None  # payload changed, same signature
    assert Auth(settings(auth_secret="other")).read_session(good) is None  # different key
    body = json.dumps({"u": "Shrihari", "exp": int(time.time()) - 1}).encode()
    stale = f"{base64.urlsafe_b64encode(body).decode()}.{auth._sign(body)}"
    assert auth.read_session(stale) is None
    removed = Auth(settings(auth_users=f"someone-else:{hash_password(PW)}"))
    assert removed.read_session(good) is None  # the user was deleted after logging in


# ---------------------------------------------------------------- the gate
def test_gate_blocks_everything_but_health_and_auth(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200
    assert client.get("/auth/me").json() == {"auth_required": True, "user": None}
    for path in ("/tasks", "/approvals", "/evals", "/meta"):
        assert client.get(path).status_code == 401, path
    assert client.post("/tasks", json={"goal": "do it"}).status_code == 401


def test_login_then_access_then_logout(client: TestClient) -> None:
    assert login(client, "wrong").status_code == 401
    assert login(client, name="nobody").status_code == 401
    r = login(client)
    assert r.status_code == 200 and "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=lax" in r.headers["set-cookie"].lower()
    assert client.get("/tasks").status_code == 200
    assert client.get("/auth/me").json()["user"] == "Shrihari"
    client.post("/auth/logout")
    assert client.get("/tasks").status_code == 401


def test_logged_in_name_cannot_be_spoofed_with_a_header(client: TestClient) -> None:
    t = f"t-{uuid.uuid4().hex[:6]}"
    tasks.enqueue(t, "goal")
    login(client)
    r = client.post(f"/tasks/{t}/cancel", headers={"X-Actor": "mallory"})
    assert r.status_code == 200
    assert ("Shrihari", "task_cancelled") in [
        (a["actor"], a["event"]) for a in approvals.audit_log(t)
    ]


def test_service_token_names_itself_and_wrong_token_is_refused(client: TestClient) -> None:
    t = f"t-{uuid.uuid4().hex[:6]}"
    tasks.enqueue(t, "goal")
    assert client.get("/tasks", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = client.post(
        f"/tasks/{t}/cancel", headers={"Authorization": "Bearer tok-123", "X-Actor": "ci-bot"}
    )
    assert r.status_code == 200
    assert ("ci-bot", "task_cancelled") in [
        (a["actor"], a["event"]) for a in approvals.audit_log(t)
    ]


def test_repeated_failures_lock_the_account_even_for_the_right_password(client: TestClient) -> None:
    for _ in range(5):
        assert login(client, "wrong").status_code == 401
    assert login(client).status_code == 429


def test_401_still_carries_cors_headers_so_the_browser_can_read_it(client: TestClient) -> None:
    origin = get_settings().cors_origins.split(",")[0]
    r = client.get("/tasks", headers={"Origin": origin})
    assert r.status_code == 401
    assert r.headers["access-control-allow-origin"] == origin
    assert r.headers["access-control-allow-credentials"] == "true"


def test_without_users_nothing_changes() -> None:
    for c in make_client(settings(auth_users="")):
        assert c.get("/tasks").status_code == 200
        assert c.get("/auth/me").json() == {"auth_required": False, "user": None}
        assert login(c).status_code == 404
