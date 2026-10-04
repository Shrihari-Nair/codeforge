from typing import Any

import pytest

from aeos.mcp_client import McpClient, McpToolError, stdio_server

DEPLOY = "2026-03-10T14:00:00+00:00"


@pytest.fixture
def obs(seeded_db: None):  # type: ignore[no-untyped-def]
    return lambda: McpClient(stdio_server("observability"), name="observability")


async def test_tools_listed_with_read_risk(obs) -> None:  # type: ignore[no-untyped-def]
    async with obs() as c:
        tools = await c.list_tools()
    assert set(tools) == {
        "search_logs",
        "get_trace",
        "get_metrics",
        "get_error_rate",
        "list_deployments",
        "compare_time_windows",
        "search_traces",
    }
    assert all(t.risk == "read" for t in tools.values())


async def test_step_change_visible_in_metrics(obs) -> None:  # type: ignore[no-untyped-def]
    async with obs() as c:
        res = await c.call(
            "get_metrics",
            {
                "start": "2026-03-10T02:00:00Z",
                "end": "2026-03-11T02:00:00Z",
                "resolution_minutes": 60,
            },
        )
    pts: list[dict[str, Any]] = res.data["points"]  # type: ignore[index]
    before = [p["p95_ms"] for p in pts if p["ts"] < "2026-03-10T14:00"]
    after = [p["p95_ms"] for p in pts if p["ts"] >= "2026-03-10T14:00"]
    assert min(after) > 2.5 * max(before) * 0.9  # clear step despite the noise blip


async def test_compare_windows_finds_cause_and_the_red_herring(obs) -> None:  # type: ignore[no-untyped-def]
    async with obs() as c:
        res = await c.call(
            "compare_time_windows",
            {
                "a_start": "2026-03-10T08:00:00Z",
                "a_end": "2026-03-10T14:00:00Z",
                "b_start": "2026-03-10T14:00:00Z",
                "b_end": "2026-03-10T20:00:00Z",
            },
        )
    d = res.data
    assert d is not None
    by_metric = {ch["metric"]: ch for ch in d["changes"]}
    assert by_metric["p95_ms"]["ratio"] > 2.5
    assert by_metric["avg_db_queries_per_request"]["ratio"] > 3
    assert by_metric["avg_cache_hits_per_request"]["b"] == 0
    # error rate and request volume did not change materially
    assert 0.5 < by_metric["rps"]["ratio"] < 1.5
    # the red herring is present on both sides at the same rate => zero delta
    slow = next(m for m in d["log_message_rate_changes"] if "inventory_audit" in m["message"])
    assert slow["per_hour_a"] > 20 and slow["delta_per_hour"] == 0
    assert [x["version"] for x in d["deployments_between"]] == ["v1.2.0"]


async def test_list_deployments_newest_first(obs) -> None:  # type: ignore[no-untyped-def]
    async with obs() as c:
        res = await c.call("list_deployments", {"limit": 5})
    versions = [x["version"] for x in res.data["deployments"]]  # type: ignore[index]
    assert versions == ["v1.2.0", "v1.1.0", "v1.0.0"]


async def test_search_logs_filters_and_limits(obs) -> None:  # type: ignore[no-untyped-def]
    async with obs() as c:
        res = await c.call("search_logs", {"query": "slow query", "level": "WARNING", "limit": 5})
        assert res.data["count"] == 5 and res.data["truncated"] is True  # type: ignore[index]
        assert all("slow query" in x["message"] for x in res.data["logs"])  # type: ignore[index]
        none = await c.call("search_logs", {"query": "100% not_a_real_message"})
    assert none.data["count"] == 0  # type: ignore[index]  # wildcard chars are escaped, not expanded


async def test_get_trace_returns_spans_and_logs(obs) -> None:  # type: ignore[no-untyped-def]
    async with obs() as c:
        db = McpClient(stdio_server("database"), name="database")
        async with db:
            r = await db.call(
                "query_database",
                {
                    "sql": "SELECT trace_id FROM demo.spans WHERE parent_id IS NULL"
                    " AND start_ts > '2026-03-10 15:00+00' LIMIT 1"
                },
            )
        tid = r.data["rows"][0][0]  # type: ignore[index]
        trace = await c.call("get_trace", {"trace_id": tid})
    t = trace.data
    assert t is not None
    assert t["span_count"] == len(t["spans"]) > 3
    product_queries = [
        s for s in t["spans"] if "products WHERE id = ?" in s["attrs"].get("db.statement", "")
    ]
    assert len(product_queries) >= 1


async def test_errors_are_structured(obs) -> None:  # type: ignore[no-untyped-def]
    async with obs() as c:
        with pytest.raises(McpToolError) as ei:
            await c.call("get_trace", {"trace_id": "nope"})
        assert ei.value.code == "not_found"
        with pytest.raises(McpToolError) as ei:
            await c.call("get_metrics", {"start": DEPLOY, "end": "2026-03-10T13:00:00Z"})
        assert ei.value.code == "invalid_argument"
        with pytest.raises(McpToolError) as ei:
            await c.call(
                "get_metrics",
                {
                    "start": "2026-03-10T02:00:00Z",
                    "end": "2026-03-11T02:00:00Z",
                    "resolution_minutes": 1,
                },
            )
        assert ei.value.code == "invalid_argument"
        with pytest.raises(McpToolError) as ei:
            await c.call("search_logs", {"limit": 5000})
        assert ei.value.code == "invalid_argument"
        with pytest.raises(McpToolError) as ei:
            await c.call(
                "get_error_rate", {"start": "2030-01-01T00:00:00Z", "end": "2030-01-02T00:00:00Z"}
            )
        assert ei.value.code == "not_found"


async def test_naive_timestamps_are_treated_as_utc(obs) -> None:  # type: ignore[no-untyped-def]
    async with obs() as c:
        a = await c.call(
            "get_error_rate", {"start": "2026-03-10T08:00:00", "end": "2026-03-10T09:00:00"}
        )
        b = await c.call(
            "get_error_rate", {"start": "2026-03-10T08:00:00Z", "end": "2026-03-10T09:00:00Z"}
        )
    assert a.data == b.data


async def test_search_traces_lists_slowest_sampled_requests_after_the_deploy(obs) -> None:  # type: ignore[no-untyped-def]
    async with obs() as c:
        after = (await c.call("search_traces", {"start": "2026-03-10T14:00:00Z",
                                                "end": "2026-03-10T20:00:00Z", "limit": 3})).data  # fmt: skip
        before = (await c.call("search_traces", {"start": "2026-03-10T08:00:00Z",
                                                 "end": "2026-03-10T14:00:00Z", "limit": 20})).data  # fmt: skip
        filtered = (await c.call("search_traces", {"start": "2026-03-10T14:00:00Z",
                                                   "end": "2026-03-10T20:00:00Z",
                                                   "min_duration_ms": 100000})).data  # fmt: skip
        with pytest.raises(McpToolError) as ei:
            await c.call(
                "search_traces",
                {"start": "2026-03-10T14:00:00Z", "end": "2026-03-10T20:00:00Z", "limit": 99},
            )
        with pytest.raises(McpToolError) as nf:
            await c.call("get_trace", {"trace_id": "deadbeef"})
    assert after is not None and before is not None and filtered is not None
    assert len(after["traces"]) == 3
    durations = [t["root_duration_ms"] for t in after["traces"]]
    assert durations == sorted(durations, reverse=True)
    assert max(t["db_query_spans"] for t in after["traces"]) > max(
        t["db_query_spans"] for t in before["traces"]
    )
    assert filtered["traces"] == []
    assert ei.value.code == "invalid_argument"
    assert (
        "search_traces" in nf.value.message and "SAMPLE" in nf.value.message
    )  # tells the agent what to do
