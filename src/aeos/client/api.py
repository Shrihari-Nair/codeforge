"""Synchronous HTTP client for the AEOS API (REST + Server-Sent Events)."""

import getpass
import json
import os
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx


class ApiError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status, self.detail = status, detail


class ApiUnreachable(Exception):
    def __init__(self, url: str, wrong_service: bool = False) -> None:
        what = (
            "did not get an AEOS API answer from"
            if wrong_service
            else "cannot reach the AEOS API at"
        )
        super().__init__(
            f"{what} {url}. Is `docker compose up -d` running? "
            "(set AEOS_API_URL or pass --api to point elsewhere)"
        )
        self.url = url


def default_api_url() -> str:
    return os.environ.get("AEOS_API_URL", "http://localhost:8800")


def default_actor() -> str:
    if actor := os.environ.get("AEOS_ACTOR") or os.environ.get("USER"):
        return actor
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "cli-user"


class AeosClient:
    def __init__(
        self, base_url: str, *, timeout: float = 15.0, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        headers = (
            {"Authorization": f"Bearer {tok}"} if (tok := os.environ.get("AEOS_API_TOKEN")) else {}
        )
        self._http = httpx.Client(
            base_url=self.base_url, timeout=timeout, transport=transport, headers=headers
        )

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = self._http.request(method, path, **kw)
        except httpx.TransportError as exc:
            raise ApiUnreachable(self.base_url) from exc
        if r.status_code >= 400:
            try:
                detail = str(r.json().get("detail", r.reason_phrase))
            except Exception:  # noqa: BLE001
                detail = r.text[:200] or r.reason_phrase
            raise ApiError(r.status_code, detail)
        try:
            return r.json()
        except ValueError as exc:  # HTML from a proxy or the dashboard: the wrong service
            raise ApiUnreachable(self.base_url, wrong_service=True) from exc

    # ------------------------------------------------------------------ reads
    def meta(self) -> dict[str, Any]:
        return self._request("GET", "/meta")  # type: ignore[no-any-return]

    def tasks(self) -> list[dict[str, Any]]:
        return self._request("GET", "/tasks")  # type: ignore[no-any-return]

    def task(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/tasks/{task_id}")  # type: ignore[no-any-return]

    def stats(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/tasks/{task_id}/stats")  # type: ignore[no-any-return]

    def events(self, task_id: str, after: int = 0) -> list[dict[str, Any]]:
        return self._request("GET", f"/tasks/{task_id}/events", params={"after": after})  # type: ignore[no-any-return]

    def approvals(
        self, status: str | None = "pending", task_id: str | None = None
    ) -> list[dict[str, Any]]:
        params = {"status": status or "", **({"task_id": task_id} if task_id else {})}
        return self._request("GET", "/approvals", params=params)  # type: ignore[no-any-return]

    # ------------------------------------------------------------------ writes
    def create_task(self, goal: str, kind: str | None = None) -> str:
        body: dict[str, Any] = {"goal": goal, **({"kind": kind} if kind else {})}
        return str(self._request("POST", "/tasks", json=body)["task_id"])

    def decide(
        self, approval_id: str, decision: str, actor: str, feedback: str | None = None
    ) -> dict[str, Any]:
        assert decision in ("approve", "reject")
        return self._request(  # type: ignore[no-any-return]
            "POST", f"/approvals/{approval_id}/{decision}",
            headers={"X-Actor": actor}, json={"feedback": feedback or None},
        )  # fmt: skip

    # ------------------------------------------------------------------ SSE
    def stream_events(
        self,
        task_id: str,
        after: int = 0,
        *,
        max_retries: int = 5,
        stop_when: Callable[[dict[str, Any]], bool] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield events after `after`, following live. A dropped connection resumes from the last
        id (no duplicates); after `max_retries` fruitless reconnects it stops (or raises)."""
        last, attempts = after, 0
        while True:
            try:
                with self._http.stream(
                    "GET", f"/tasks/{task_id}/events/stream", params={"after": last},
                    timeout=httpx.Timeout(15.0, read=None),
                ) as r:  # fmt: skip
                    if r.status_code >= 400:
                        raise ApiError(r.status_code, r.read().decode(errors="replace")[:200])
                    for line in r.iter_lines():
                        if not line.startswith("data:"):
                            continue  # id:/event:/keep-alive comments
                        ev = json.loads(line[5:].strip())
                        if ev["id"] <= last:
                            continue
                        last, attempts = ev["id"], 0
                        yield ev
                        if stop_when and stop_when(ev):
                            return
            except httpx.TransportError as exc:
                if attempts >= max_retries:
                    raise ApiUnreachable(self.base_url) from exc
                attempts += 1
                time.sleep(min(attempts * 0.5, 3))
                continue
            if attempts >= max_retries:
                return
            attempts += 1
