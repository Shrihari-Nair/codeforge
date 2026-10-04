"""Ordered commit history of the seeded checkout-service.

Source of truth for the demo target. `build_repo.py` materialises this into a
real git repo with fixed authors/dates so commit SHAs are reproducible.

Commit 17 ("refactor(catalog): simplify product lookup") is the planted
regression: it replaces the batched, cache-aside product lookup with a
per-product query loop (N+1) that also bypasses the cache. Tests still pass.
"""

from dataclasses import dataclass, field

ASHA = ("Asha Rao", "asha@example.com")
DEV = ("Dev Patel", "dev@example.com")

# ---------------------------------------------------------------- file bodies

README_V1 = """# checkout-service

Tiny checkout service used as an investigation target.
"""

README_V2 = (
    README_V1
    + """
## Cost model

Latency is measured by counting work, not wall-clock, so benchmarks are
deterministic. See `checkout/costs.py` for the per-operation costs.
Run `python benchmark.py --json` for p50/p95/p99.
"""
)

README_V3 = (
    README_V2
    + """
## Regions

`handle_checkout(ctx, cart_id, region)` supports `US` and `EU` tax rates.
"""
)

PYTEST_INI = """[pytest]
pythonpath = .
testpaths = tests
"""

INIT_V1 = '"""Checkout service."""\n'
INIT_V2 = '"""Checkout service."""\n\n__version__ = "1.2.1"\n'

COSTS = '''"""Simulated cost model (milliseconds): count work instead of timing it."""

DB_QUERY_BASE_MS = 2.0
DB_ROW_MS = 0.05
CACHE_OP_MS = 0.1
SERIALIZE_BASE_MS = 0.5
SERIALIZE_ITEM_MS = 0.01
AUDIT_SCAN_MS = 40.0


class Meter:
    def __init__(self) -> None:
        self.ms = 0.0
        self.db_queries = 0
        self.cache_hits = 0
        self.cache_misses = 0

    def add(self, ms: float) -> None:
        self.ms += ms
'''

DB_V1 = '''"""In-memory sqlite wrapper that charges every query to the meter."""

import sqlite3

from checkout.costs import DB_QUERY_BASE_MS, DB_ROW_MS, Meter

SCHEMA = """
CREATE TABLE products (
    id INTEGER PRIMARY KEY, name TEXT, price_cents INTEGER, stock INTEGER);
CREATE TABLE cart_items (cart_id INTEGER, product_id INTEGER, qty INTEGER);
CREATE INDEX idx_cart_items_cart ON cart_items (cart_id);
"""


class Database:
    def __init__(self, meter: Meter) -> None:
        self._meter = meter
        self._conn = sqlite3.connect(":memory:")
        self._conn.executescript(SCHEMA)

    def query(self, sql, params=(), extra_ms=0.0):
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        self._meter.db_queries += 1
        self._meter.add(DB_QUERY_BASE_MS + DB_ROW_MS * len(rows) + extra_ms)
        return rows
'''

DB_V2 = (
    DB_V1.replace(
        "        self._conn.executescript(SCHEMA)\n",
        "        self._conn.executescript(SCHEMA)\n        self._seed()\n",
    )
    + """
    def _seed(self) -> None:
        products = [(i, f"product-{i}", 500 + (i * 37) % 4500, (i * 11) % 60) for i in range(1, 201)]
        self._conn.executemany("INSERT INTO products VALUES (?, ?, ?, ?)", products)
        items = []
        for cart in range(1, 101):
            for i in range(1 + (cart * 7) % 12):
                items.append((cart, (cart * 13 + i * 17) % 200 + 1, 1 + (cart + i) % 3))
        self._conn.executemany("INSERT INTO cart_items VALUES (?, ?, ?)", items)
        self._conn.commit()
"""
)

CACHE_V1 = '''"""Tiny in-memory cache that charges every operation to the meter."""

from checkout.costs import CACHE_OP_MS, Meter


class Cache:
    def __init__(self, meter: Meter) -> None:
        self._meter = meter
        self._store = {}

    def get(self, key):
        self._meter.add(CACHE_OP_MS)
        value = self._store.get(key)
        if value is None:
            self._meter.cache_misses += 1
        else:
            self._meter.cache_hits += 1
        return value

    def set(self, key, value):
        self._meter.add(CACHE_OP_MS)
        self._store[key] = value
'''

CACHE_V2 = CACHE_V1.replace(
    "    def __init__(self, meter: Meter) -> None:\n        self._meter = meter\n        self._store = {}\n",
    "    def __init__(self, meter: Meter, max_entries: int = 1024) -> None:\n"
    "        self._meter = meter\n        self._store = {}\n        self._max_entries = max_entries\n",
).replace(
    "        self._store[key] = value\n",
    "        self._store[key] = value\n"
    "        while len(self._store) > self._max_entries:\n"
    "            self._store.pop(next(iter(self._store)))\n",
)

CATALOG_GOOD = '''"""Product lookup: batched, cache-aside."""


def _key(product_id):
    return f"product:{product_id}"


def get_products(ctx, ids):
    found = {}
    missing = []
    for product_id in ids:
        cached = ctx.cache.get(_key(product_id))
        if cached is None:
            missing.append(product_id)
        else:
            found[product_id] = cached
    if missing:
        marks = ",".join("?" * len(missing))
        rows = ctx.db.query(
            f"SELECT id, name, price_cents FROM products WHERE id IN ({marks})", missing
        )
        for product_id, name, price_cents in rows:
            product = {"id": product_id, "name": name, "price_cents": price_cents}
            ctx.cache.set(_key(product_id), product)
            found[product_id] = product
    return [found[product_id] for product_id in ids]
'''

CATALOG_BAD = '''"""Product lookup."""


def get_product(ctx, product_id):
    rows = ctx.db.query(
        "SELECT id, name, price_cents FROM products WHERE id = ?", (product_id,)
    )
    pid, name, price_cents = rows[0]
    return {"id": pid, "name": name, "price_cents": price_cents}


def get_products(ctx, ids):
    return [get_product(ctx, product_id) for product_id in ids]
'''

PRICING = '''"""Totals and tax."""

TAX_RATES = {"US": 0.07, "EU": 0.2}


def tax_rate(ctx, region):
    key = f"tax:{region}"
    cached = ctx.cache.get(key)
    if cached is not None:
        return cached
    rate = TAX_RATES[region]
    ctx.cache.set(key, rate)
    return rate


def price_cart(ctx, products, qtys, region):
    subtotal = sum(p["price_cents"] * q for p, q in zip(products, qtys, strict=True))
    tax = round(subtotal * tax_rate(ctx, region))
    return {"subtotal_cents": subtotal, "tax_cents": tax, "total_cents": subtotal + tax}
'''

SERIALIZER_V1 = '''"""Receipt serialization."""

import json

from checkout.costs import SERIALIZE_BASE_MS, SERIALIZE_ITEM_MS


def serialize_receipt(ctx, receipt):
    ctx.meter.add(SERIALIZE_BASE_MS + SERIALIZE_ITEM_MS * len(receipt["items"]))
    return json.dumps(receipt, sort_keys=True)
'''

SERIALIZER_V2 = SERIALIZER_V1.replace(
    "json.dumps(receipt, sort_keys=True)",
    'json.dumps(receipt, sort_keys=True, separators=(",", ":"))',
)

AUDIT = '''"""Periodic inventory audit: a slow full scan on every Nth checkout."""

from checkout.costs import AUDIT_SCAN_MS

AUDIT_EVERY = 25


def maybe_audit(ctx):
    if ctx.request_count % AUDIT_EVERY != 0:
        return None
    rows = ctx.db.query(
        "SELECT id FROM products WHERE stock < ?", (5,), extra_ms=AUDIT_SCAN_MS
    )
    return len(rows)
'''

SERVICE_V1 = '''"""Checkout handler."""

from checkout import catalog, pricing, serializer
from checkout.cache import Cache
from checkout.costs import Meter
from checkout.db import Database


class Context:
    def __init__(self) -> None:
        self.meter = Meter()
        self.db = Database(self.meter)
        self.cache = Cache(self.meter)
        self.request_count = 0


def make_context() -> Context:
    return Context()


def handle_checkout(ctx, cart_id):
    rows = ctx.db.query(
        "SELECT product_id, qty FROM cart_items WHERE cart_id = ? ORDER BY rowid", (cart_id,)
    )
    ids = [r[0] for r in rows]
    qtys = [r[1] for r in rows]
    products = catalog.get_products(ctx, ids)
    totals = pricing.price_cart(ctx, products, qtys, "US")
    receipt = {
        "cart_id": cart_id,
        "items": [
            {"product_id": p["id"], "name": p["name"], "qty": q}
            for p, q in zip(products, qtys, strict=True)
        ],
        **totals,
    }
    return serializer.serialize_receipt(ctx, receipt)
'''

SERVICE_V2 = (
    SERVICE_V1.replace(
        "from checkout import catalog, pricing, serializer",
        "from checkout import audit, catalog, pricing, serializer",
    )
    .replace(
        "def handle_checkout(ctx, cart_id):\n",
        "def handle_checkout(ctx, cart_id):\n    ctx.request_count += 1\n",
    )
    .replace(
        "    return serializer.serialize_receipt(ctx, receipt)\n",
        "    audit.maybe_audit(ctx)\n    return serializer.serialize_receipt(ctx, receipt)\n",
    )
)

SERVICE_V3 = SERVICE_V2.replace(
    "def handle_checkout(ctx, cart_id):", 'def handle_checkout(ctx, cart_id, region="US"):'
).replace('products, qtys, "US")', "products, qtys, region)")

BENCHMARK = '''"""Deterministic benchmark: fixed workload, simulated-cost latency percentiles."""

import argparse
import json
import math

from checkout.service import handle_checkout, make_context

N_REQUESTS = 500
WARMUP = 100


def run():
    ctx = make_context()

    def request(i):
        cart_id = (i * 37) % 100 + 1
        before = ctx.meter.ms
        handle_checkout(ctx, cart_id)
        return ctx.meter.ms - before

    for i in range(WARMUP):
        request(i)
    queries_before = ctx.meter.db_queries
    latencies = sorted(request(WARMUP + i) for i in range(N_REQUESTS))

    def pct(p):
        return round(latencies[math.ceil(p / 100 * len(latencies)) - 1], 3)

    return {
        "requests": N_REQUESTS,
        "db_queries": ctx.meter.db_queries - queries_before,
        "p50_ms": pct(50),
        "p95_ms": pct(95),
        "p99_ms": pct(99),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    result = run()
    if parser.parse_args().json:
        print(json.dumps(result, sort_keys=True))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
'''

TEST_SERVICE = """import json

from checkout.service import handle_checkout, make_context


def test_receipt_has_items_and_totals():
    ctx = make_context()
    receipt = json.loads(handle_checkout(ctx, 1))
    assert receipt["cart_id"] == 1
    assert len(receipt["items"]) == 1 + (1 * 7) % 12
    assert receipt["total_cents"] == receipt["subtotal_cents"] + receipt["tax_cents"]


def test_same_cart_same_receipt():
    ctx = make_context()
    assert handle_checkout(ctx, 5) == handle_checkout(ctx, 5)
"""

TEST_PRICING = """from checkout.pricing import tax_rate
from checkout.service import make_context


def test_tax_rates_by_region():
    ctx = make_context()
    assert tax_rate(ctx, "US") == 0.07
    assert tax_rate(ctx, "EU") == 0.2
"""

TEST_CATALOG = """from checkout.catalog import get_products
from checkout.service import make_context


def test_get_products_preserves_order():
    ctx = make_context()
    products = get_products(ctx, [7, 3, 11])
    assert [p["id"] for p in products] == [7, 3, 11]
    assert products[0]["name"] == "product-7"
"""


@dataclass(frozen=True)
class Commit:
    key: str
    message: str
    date: str
    author: tuple[str, str]
    files: dict[str, str] = field(default_factory=dict)


COMMITS: list[Commit] = [
    Commit(
        "skeleton",
        "chore: project skeleton",
        "2026-02-16T10:00:00+0000",
        ASHA,
        {"README.md": README_V1, "pytest.ini": PYTEST_INI, "checkout/__init__.py": INIT_V1},
    ),
    Commit(
        "db",
        "feat(db): in-memory sqlite wrapper with cost meter",
        "2026-02-17T10:00:00+0000",
        ASHA,
        {"checkout/costs.py": COSTS, "checkout/db.py": DB_V1},
    ),
    Commit(
        "db-seed",
        "feat(db): seed products and carts",
        "2026-02-18T10:00:00+0000",
        ASHA,
        {"checkout/db.py": DB_V2},
    ),
    Commit(
        "cache",
        "feat(cache): add in-memory cache",
        "2026-02-19T10:00:00+0000",
        DEV,
        {"checkout/cache.py": CACHE_V1},
    ),
    Commit(
        "catalog",
        "feat(catalog): batched cache-aside product lookup",
        "2026-02-20T10:00:00+0000",
        DEV,
        {"checkout/catalog.py": CATALOG_GOOD},
    ),
    Commit(
        "pricing",
        "feat(pricing): tax and totals",
        "2026-02-23T10:00:00+0000",
        ASHA,
        {"checkout/pricing.py": PRICING},
    ),
    Commit(
        "serializer",
        "feat(serializer): receipt serialization",
        "2026-02-24T10:00:00+0000",
        ASHA,
        {"checkout/serializer.py": SERIALIZER_V1},
    ),
    Commit(
        "service",
        "feat(service): checkout handler",
        "2026-02-25T10:00:00+0000",
        ASHA,
        {"checkout/service.py": SERVICE_V1},
    ),
    Commit(
        "tests",
        "test: checkout service tests",
        "2026-02-26T10:00:00+0000",
        DEV,
        {"tests/test_service.py": TEST_SERVICE},
    ),
    Commit(
        "benchmark",
        "feat(bench): deterministic benchmark",
        "2026-02-27T10:00:00+0000",
        DEV,
        {"benchmark.py": BENCHMARK},
    ),
    Commit(
        "audit",
        "feat(audit): periodic inventory audit",
        "2026-03-01T10:00:00+0000",
        ASHA,
        {"checkout/audit.py": AUDIT, "checkout/service.py": SERVICE_V2},
    ),
    Commit(
        "docs-cost-model",
        "docs: document cost model",
        "2026-03-02T09:00:00+0000",
        ASHA,
        {"README.md": README_V2},
    ),
    Commit(
        "serializer-compact",
        "perf(serializer): compact json separators",
        "2026-03-03T10:00:00+0000",
        DEV,
        {"checkout/serializer.py": SERIALIZER_V2},
    ),
    Commit(
        "test-pricing",
        "test: cover tax rates",
        "2026-03-04T10:00:00+0000",
        DEV,
        {"tests/test_pricing.py": TEST_PRICING},
    ),
    Commit(
        "cache-cap",
        "fix(cache): cap cache size",
        "2026-03-05T10:00:00+0000",
        ASHA,
        {"checkout/cache.py": CACHE_V2},
    ),
    Commit(
        "region",
        "feat(service): support region parameter",
        "2026-03-07T10:00:00+0000",
        DEV,
        {"checkout/service.py": SERVICE_V3},
    ),
    Commit(
        "n-plus-one",
        "refactor(catalog): simplify product lookup",
        "2026-03-09T10:00:00+0000",
        DEV,
        {"checkout/catalog.py": CATALOG_BAD},
    ),
    Commit(
        "docs-regions",
        "docs: document regions",
        "2026-03-09T15:00:00+0000",
        ASHA,
        {"README.md": README_V3},
    ),
    Commit(
        "test-catalog",
        "test(catalog): product lookup ordering",
        "2026-03-10T10:00:00+0000",
        DEV,
        {"tests/test_catalog.py": TEST_CATALOG},
    ),
    Commit(
        "version",
        "chore: bump version",
        "2026-03-10T16:00:00+0000",
        ASHA,
        {"checkout/__init__.py": INIT_V2},
    ),
]

# (version, deployed commit key, timestamp). Last deploy contains the regression.
DEPLOYS: list[tuple[str, str, str]] = [
    ("v1.0.0", "docs-cost-model", "2026-03-02T10:00:00+00:00"),
    ("v1.1.0", "cache-cap", "2026-03-06T10:00:00+00:00"),
    ("v1.2.0", "test-catalog", "2026-03-10T14:00:00+00:00"),
]

BAD_COMMIT_KEY = "n-plus-one"
LAST_GOOD_COMMIT_KEY = "region"
