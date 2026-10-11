import json

import httpx
import pytest

from aeos.client.api import AeosClient, ApiError, ApiUnreachable


def client(handler) -> AeosClient:  # type: ignore[no-untyped-def]
    return AeosClient("http://aeos", transport=httpx.MockTransport(handler))


def test_create_task_sends_goal_and_kind() -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(202, json={"task_id": "t-1"})

    assert client(handler).create_task("bump ws", "change") == "t-1"
    assert seen["body"] == {"goal": "bump ws", "kind": "change"}


def test_decide_sends_actor_header_and_feedback() -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(path=req.url.path, actor=req.headers["x-actor"], body=json.loads(req.content))
        return httpx.Response(200, json={"status": "resuming"})

    client(handler).decide("ap-1", "reject", "alice", "too big")
    assert seen == {
        "path": "/approvals/ap-1/reject",
        "actor": "alice",
        "body": {"feedback": "too big"},
    }


@pytest.mark.parametrize(
    "status,detail", [(409, "already decided"), (410, "expired"), (404, "no task")]
)
def test_http_errors_become_api_error_with_detail(status: int, detail: str) -> None:
    c = client(lambda r: httpx.Response(status, json={"detail": detail}))
    with pytest.raises(ApiError) as ei:
        c.task("t-x")
    assert ei.value.status == status and detail in str(ei.value)


def test_unreachable_api_is_a_dedicated_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ApiUnreachable) as ei:
        client(handler).meta()
    assert "http://aeos" in str(ei.value)


def sse(*events: dict) -> bytes:  # type: ignore[type-arg]
    body = b"".join(
        f"id: {e['id']}\nevent: aeos\ndata: {json.dumps(e)}\n\n".encode() for e in events
    )
    return body + b": keep-alive\n\n"


def test_stream_parses_events_and_skips_keepalives() -> None:
    evs = [{"id": 1, "type": "a"}, {"id": 2, "type": "b"}]
    c = client(lambda r: httpx.Response(200, content=sse(*evs)))
    assert [e["id"] for e in c.stream_events("t-1", max_retries=0)] == [1, 2]


def test_stream_resumes_from_last_id_after_a_dropped_connection() -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url.params.get("after")))
        if len(calls) == 1:
            return httpx.Response(200, content=sse({"id": 1, "type": "a"}, {"id": 2, "type": "b"}))
        return httpx.Response(200, content=sse({"id": 3, "type": "c"}))

    it = client(handler).stream_events("t-1", max_retries=1, stop_when=lambda e: e["id"] == 3)
    assert [e["id"] for e in it] == [1, 2, 3]
    assert calls == ["0", "2"]  # resumed after event 2, no duplicates
