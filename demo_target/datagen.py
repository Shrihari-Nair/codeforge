"""Seeded generation of deployments, logs, metrics and spans around the bad deploy.

Pure and deterministic: same (commits, seed) -> identical output. The latency
model mirrors the service's behaviour:
  * before v1.2.0: cart query + cache-hit product batch  -> ~2 DB queries/request
  * after  v1.2.0: cart query + one query per product (N+1) -> 1 + k queries
Red herring: every 25th request runs a slow `inventory_audit` scan, at the same
rate before and after the deploy. Noise: an unrelated cache-backend timeout burst.
"""

import math
import random
from datetime import datetime, timedelta
from typing import Any

from demo_target.history import DEPLOYS
from demo_target.templates import Profile, Template

SERVICE = "checkout-service"
DEPLOY_TS = datetime.fromisoformat(DEPLOYS[-1][2])
WINDOW_HOURS = 12
START = DEPLOY_TS - timedelta(hours=WINDOW_HOURS)
END = DEPLOY_TS + timedelta(hours=WINDOW_HOURS)
BURST_START = DEPLOY_TS - timedelta(hours=6)  # unrelated noise
BURST_MINUTES = 5

REQUESTS_PER_MINUTE = 120
BASE_MS = 45.0
PER_QUERY_MS = 25.0
PER_ITEM_MS = 2.0
AUDIT_MS = 350.0
AUDIT_EVERY = 25
BASE_ERROR_RATE = 0.002
BURST_ERROR_RATE = 0.06
LOGS_PER_MINUTE = 6
TRACE_EVERY_MINUTES = 3

Dataset = dict[str, list[dict[str, Any]]]


def _pct(sorted_values: list[float], p: float) -> float:
    return sorted_values[math.ceil(p / 100 * len(sorted_values)) - 1]


def _hex(rng: random.Random, n: int) -> str:
    return f"{rng.getrandbits(n * 4):0{n}x}"


def _deployments(commits: list[dict[str, Any]], deploys: Any = DEPLOYS) -> list[dict[str, Any]]:
    by_key = {c["key"]: c["sha"] for c in commits}
    return [
        {
            "ts": datetime.fromisoformat(ts),
            "service": SERVICE,
            "version": version,
            "commit_sha": by_key[key],
        }
        for version, key, ts in deploys
    ]


def generate(
    commits: list[dict[str, Any]], seed: int = 7, template: Template | None = None
) -> Dataset:
    """Without a template this is the original n_plus_one scenario, byte for byte."""
    rng = random.Random(seed)
    prof = template.profile if template else None
    data: Dataset = {
        "deployments": _deployments(commits, template.deploys if template else DEPLOYS),
        "logs": [],
        "metrics": [],
        "spans": [],
    }
    n_minutes = int((END - START).total_seconds() // 60)
    request_counter = 0
    for m in range(n_minutes):
        ts = START + timedelta(minutes=m)
        regressed = ts >= DEPLOY_TS
        in_burst = BURST_START <= ts < BURST_START + timedelta(minutes=BURST_MINUTES)
        err_rate = BURST_ERROR_RATE if in_burst else BASE_ERROR_RATE

        reqs: list[dict[str, Any]] = []
        for _ in range(REQUESTS_PER_MINUTE):
            request_counter += 1
            k = 1 + rng.randrange(12)
            queries = (prof.queries_after(k) if prof else 1 + k) if regressed else 2
            latency = BASE_MS + PER_QUERY_MS * queries + PER_ITEM_MS * k + rng.gauss(0, 6)
            if prof and regressed:
                latency += prof.extra_latency_ms(k)
            audit = request_counter % AUDIT_EVERY == 0 or bool(
                prof and regressed and prof.audit_every_request
            )
            if audit:
                latency += AUDIT_MS
                queries += 1
            error = rng.random() < err_rate
            if error and in_burst:
                latency += 200
            reqs.append(
                {
                    "k": k,
                    "queries": queries,
                    "latency": max(latency, 10.0),
                    "audit": audit,
                    "error": error,
                    "ts": ts + timedelta(seconds=rng.uniform(0, 59.9)),
                    "trace_id": _hex(rng, 32),
                    "cache_hits": (prof.cache_hits_after(k) if prof else 0) if regressed else k + 1,
                }
            )

        lat = sorted(r["latency"] for r in reqs)
        data["metrics"].append(
            {
                "ts": ts,
                "service": SERVICE,
                "p50_ms": round(_pct(lat, 50), 1),
                "p95_ms": round(_pct(lat, 95), 1),
                "p99_ms": round(_pct(lat, 99), 1),
                "error_rate": round(sum(r["error"] for r in reqs) / len(reqs), 4),
                "rps": round(len(reqs) / 60, 2),
            }
        )

        for r in rng.sample(reqs, LOGS_PER_MINUTE):
            data["logs"].append(_request_log(r))
        for r in reqs:
            if r["error"]:
                data["logs"].append(_error_log(r, in_burst))
        audits = [r for r in reqs if r["audit"]]
        # one warning/min normally; when every request audits, the storm of warnings is the symptom
        for a in audits[: 24 if prof and regressed and prof.audit_every_request else 1]:
            data["logs"].append(
                {
                    "ts": a["ts"],
                    "level": "WARNING",
                    "service": SERVICE,
                    "trace_id": a["trace_id"],
                    "message": "slow query inventory_audit",
                    "attrs": {
                        "query": "inventory_audit",
                        "duration_ms": round(AUDIT_MS + rng.uniform(-30, 30), 1),
                    },
                }
            )
        if m % TRACE_EVERY_MINUTES == 0:
            data["spans"].extend(_trace(rng, reqs[0], regressed, prof))

    data["logs"].sort(key=lambda r: r["ts"])
    return data


def _request_log(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": r["ts"],
        "level": "INFO",
        "service": SERVICE,
        "trace_id": r["trace_id"],
        "message": "request completed",
        "attrs": {
            "path": "/checkout",
            "status": 500 if r["error"] else 200,
            "duration_ms": round(r["latency"], 1),
            "db_queries": r["queries"],
            "cache_hits": r["cache_hits"],
            "items": r["k"],
        },
    }


def _error_log(r: dict[str, Any], in_burst: bool) -> dict[str, Any]:
    message = "cache backend timeout after 200ms" if in_burst else "checkout failed: upstream error"
    return {
        "ts": r["ts"],
        "level": "ERROR",
        "service": SERVICE,
        "trace_id": r["trace_id"],
        "message": message,
        "attrs": {"path": "/checkout", "status": 500},
    }


def _trace(
    rng: random.Random, r: dict[str, Any], regressed: bool, prof: Profile | None = None
) -> list[dict[str, Any]]:
    n_plus_one = prof.product_query_spans if prof else True
    trace_id, root_id = r["trace_id"], _hex(rng, 16)
    t0: datetime = r["ts"]
    spans: list[dict[str, Any]] = []
    cursor = 0.0

    def add(name: str, kind: str, dur: float, attrs: dict[str, Any], parent: str = root_id) -> None:
        nonlocal cursor
        spans.append(
            {
                "trace_id": trace_id,
                "span_id": _hex(rng, 16),
                "parent_id": parent,
                "name": name,
                "kind": kind,
                "start_ts": t0 + timedelta(milliseconds=cursor),
                "duration_ms": round(dur, 2),
                "attrs": attrs,
            }
        )
        cursor += dur

    slow_cart = bool(prof and regressed and prof.slow_cart_query)
    cart_attrs: dict[str, Any] = {
        "db.statement": "SELECT product_id, qty FROM cart_items WHERE cart_id = ?",
        "db.rows": r["k"],
    }
    if slow_cart:
        cart_attrs |= {"db.plan": "SCAN cart_items", "db.rows_scanned": 650}
    elif prof:
        cart_attrs["db.plan"] = "SEARCH cart_items USING INDEX idx_cart_items_cart (cart_id=?)"
    add("db.query", "db", PER_QUERY_MS + (160.0 if slow_cart else 0.0), cart_attrs)
    if regressed and n_plus_one:
        for _ in range(r["k"]):
            add(
                "db.query",
                "db",
                PER_QUERY_MS + rng.uniform(-3, 3),
                {
                    "db.statement": "SELECT id, name, price_cents FROM products WHERE id = ?",
                    "db.rows": 1,
                },
            )
    elif regressed and prof and prof.cache_hits_after(r["k"]) == 0:  # cache gone: one batched query
        add(
            "db.query",
            "db",
            PER_QUERY_MS,
            {"db.statement": "SELECT id, name, price_cents FROM products WHERE id IN (...)",
             "db.rows": r["k"], "cache": "miss"},
        )  # fmt: skip
    else:
        add(
            "catalog.get_products",
            "internal",
            0.1 * (r["k"] + 1),
            {"cache": "hit", "items": r["k"]},
        )
    if regressed and prof and prof.pricing_query_spans:
        for _ in range(r["k"]):
            add(
                "db.query",
                "db",
                PER_QUERY_MS + rng.uniform(-3, 3),
                {"db.statement": "SELECT price_cents FROM products WHERE id = ?", "db.rows": 1,
                 "code.function": "pricing.price_cart"},
            )  # fmt: skip
    else:
        add("pricing.price_cart", "internal", 0.2, {})
    slow_ser = bool(prof and regressed and prof.slow_serialize_span)
    add(
        "serialize_receipt",
        "internal",
        0.5 + 0.01 * r["k"] + (prof.extra_latency_ms(r["k"]) if slow_ser and prof else 0.0),
        {"items": r["k"]},
    )
    if r["audit"]:
        add(
            "db.query",
            "db",
            AUDIT_MS,
            {
                "db.statement": "SELECT id FROM products WHERE stock < ?",
                "db.name": "inventory_audit",
            },
        )
    spans.insert(
        0,
        {
            "trace_id": trace_id,
            "span_id": root_id,
            "parent_id": None,
            "name": "POST /checkout",
            "kind": "server",
            "start_ts": t0,
            "duration_ms": round(r["latency"], 2),
            "attrs": {"http.status_code": 500 if r["error"] else 200, "items": r["k"]},
        },
    )
    return spans
