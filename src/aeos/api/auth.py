"""Optional login for the dashboard and API.

Off by default (no `AEOS_AUTH_USERS`): behaviour is unchanged and the approver is whatever name
the client sends in `X-Actor`. When users are configured:

- every endpoint except `/healthz` and `/auth/*` needs a session cookie (from `/auth/login`) or the
  service token (`Authorization: Bearer <AEOS_API_TOKEN>`, for the terminal client and scripts);
- a logged-in user's name is the actor on approvals and cancellations. It cannot be overridden by
  a header, so the audit log names a real login;
- passwords are stored as scrypt hashes, and repeated failures lock the account briefly.

Make a user entry with:  python -m aeos.api.auth hash   (prints `name:hash` for AEOS_AUTH_USERS).
The hash uses `.` separators on purpose: a `$` would be mangled by docker compose's .env parsing.
"""

import base64
import getpass
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from aeos.core.config import Settings

log = logging.getLogger("aeos.auth")

COOKIE = "aeos_session"
SESSION_TTL_S = 12 * 3600
MAX_FAILURES = 5
LOCKOUT_S = 60.0
_DUMMY = "scrypt." + "00" * 16 + "." + "00" * 32  # burned on unknown users so timing is the same


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt.{salt.hex()}.{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, hash_hex = stored.split(".")
        if scheme != "scrypt":
            return False
        salt, want = bytes.fromhex(salt_hex), bytes.fromhex(hash_hex)
    except ValueError:
        return False
    got = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=len(want))
    return hmac.compare_digest(got, want)


def parse_users(raw: str) -> dict[str, str]:
    """`name:hash,name2:hash2` -> {name: hash}. Names are case-sensitive and shown as typed."""
    users: dict[str, str] = {}
    for part in filter(None, (p.strip() for p in raw.split(","))):
        name, sep, stored = part.partition(":")
        if not sep or not name.strip() or not stored.startswith("scrypt."):
            raise ValueError("AEOS_AUTH_USERS entries look like name:scrypt.salt.hash")
        users[name.strip()] = stored
    return users


class Identity(BaseModel):
    name: str
    kind: str  # "session" (a person) or "token" (a script; names itself via X-Actor)


class Auth:
    def __init__(self, s: Settings) -> None:
        self.users = parse_users(s.auth_users)
        self.token = s.api_token.get_secret_value() if s.api_token else None
        secret = s.auth_secret.get_secret_value() if s.auth_secret else None
        if self.users and not secret:
            secret = secrets.token_hex(32)
            log.warning("AEOS_AUTH_SECRET is not set: sessions end whenever the API restarts")
        self._key = (secret or "").encode()
        self.secure_cookie = s.auth_cookie_secure
        self._fails: dict[str, tuple[int, float]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.users)

    # ---- sessions: base64url(payload).hex(hmac)
    def _sign(self, payload: bytes) -> str:
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()

    def make_session(self, name: str) -> str:
        body = json.dumps({"u": name, "exp": int(time.time()) + SESSION_TTL_S}).encode()
        return f"{base64.urlsafe_b64encode(body).decode()}.{self._sign(body)}"

    def read_session(self, value: str | None) -> str | None:
        if not value or "." not in value:
            return None
        b64, _, sig = value.partition(".")
        try:
            body = base64.urlsafe_b64decode(b64.encode())
            claims: dict[str, Any] = json.loads(body)
        except ValueError:
            return None
        if not hmac.compare_digest(sig, self._sign(body)):
            return None
        name = claims.get("u")
        if (
            not isinstance(name, str)
            or name not in self.users
            or claims.get("exp", 0) < time.time()
        ):
            return None
        return name

    def identify(self, cookie_header: str | None, authorization: str | None) -> Identity | None:
        if self.token and authorization and authorization.startswith("Bearer "):
            if hmac.compare_digest(authorization[7:].strip(), self.token):
                return Identity(name="service", kind="token")
        for chunk in (cookie_header or "").split(";"):
            key, _, val = chunk.strip().partition("=")
            if key == COOKIE and (name := self.read_session(val)):
                return Identity(name=name, kind="session")
        return None

    # ---- login with lockout
    def login(self, username: str, password: str, who: str) -> str | None:
        key = f"{who}|{username.lower()}"
        fails, until = self._fails.get(key, (0, 0.0))
        if until > time.time():
            raise HTTPException(429, "too many failed attempts; try again in a minute")
        stored = self.users.get(username)
        ok = verify_password(password, stored or _DUMMY) and stored is not None
        if not ok:
            fails += 1
            self._fails[key] = (fails, time.time() + LOCKOUT_S if fails >= MAX_FAILURES else 0.0)
            if fails >= MAX_FAILURES:
                self._fails[key] = (0, time.time() + LOCKOUT_S)
            return None
        self._fails.pop(key, None)
        return self.make_session(username)


class AuthGate:
    """Pure ASGI middleware (it must not buffer the SSE event streams)."""

    def __init__(self, app: Any, auth: Auth) -> None:
        self.app, self.auth = app, auth

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or not self.auth.enabled
            or scope["method"] == "OPTIONS"
            or path == "/healthz"
            or path.startswith("/auth/")
        ):
            await self.app(scope, receive, send)
            return
        h = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        who = self.auth.identify(h.get("cookie"), h.get("authorization"))
        if who is None:
            body = b'{"detail":"login required"}'
            headers = [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ]
            await send({"type": "http.response.start", "status": 401, "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return
        scope.setdefault("state", {})["identity"] = who
        await self.app(scope, receive, send)


def actor_for(request: Request, claimed: str | None) -> str | None:
    """Who to record. A logged-in person is who they logged in as; scripts name themselves."""
    who: Identity | None = getattr(request.state, "identity", None)
    if who is not None and who.kind == "session":
        return who.name
    return claimed


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


def auth_router(auth: Auth) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"])

    @router.get("/me")
    def me(request: Request) -> dict[str, Any]:
        who = auth.identify(request.headers.get("cookie"), request.headers.get("authorization"))
        return {"auth_required": auth.enabled, "user": who.name if who else None}

    @router.post("/login")
    def login(body: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
        if not auth.enabled:
            raise HTTPException(404, "login is not enabled")
        value = auth.login(
            body.username, body.password, request.client.host if request.client else "?"
        )
        if value is None:
            raise HTTPException(401, "wrong username or password")
        response.set_cookie(
            COOKIE, value, max_age=SESSION_TTL_S, httponly=True, samesite="lax",
            secure=auth.secure_cookie, path="/",
        )  # fmt: skip
        return {"user": body.username}

    @router.post("/logout")
    def logout(response: Response) -> dict[str, str]:
        response.delete_cookie(COOKIE, path="/")
        return {"status": "signed out"}

    return router


def _main() -> None:
    if sys.argv[1:] != ["hash"]:
        sys.exit("usage: python -m aeos.api.auth hash")
    name = input("username: ").strip()
    pw = getpass.getpass("password: ")
    if pw != getpass.getpass("again: "):
        sys.exit("passwords differ")
    print(
        f"\nAdd to .env (comma-separate several users):\nAEOS_AUTH_USERS={name}:{hash_password(pw)}"
    )


if __name__ == "__main__":
    _main()
