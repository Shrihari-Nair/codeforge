"""Regression templates: same service, different planted regressions.

Every template shares the first 16 commits of `history.py` (the base service plus several
benign decoy commits), then plants ONE regression in a different file, followed by benign commits
(docs, a test that still passes, a version bump). `n_plus_one` is the original scenario with its
original history, so its commit SHAs and ground truth are unchanged.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from demo_target.history import ASHA, CATALOG_GOOD, COMMITS, DEPLOYS, DEV, Commit

BASE = COMMITS[:16]  # ... up to and including "region"
BASE_FILES: dict[str, str] = {}
for _c in BASE:
    BASE_FILES.update(_c.files)

LAST_DEPLOY_TS = DEPLOYS[-1][2]


@dataclass(frozen=True)
class Profile:
    """How the regression shows up in telemetry (drives the data generator)."""

    queries_after: Callable[[int], int] = lambda k: 2  # DB queries per request, k = items in cart
    cache_hits_after: Callable[[int], int] = lambda k: k + 1
    extra_latency_ms: Callable[[int], float] = lambda k: (
        0.0
    )  # production-scale ms added per request
    audit_every_request: bool = False
    product_query_spans: bool = False  # per-product SELECTs from catalog.get_product
    pricing_query_spans: bool = False  # per-item SELECTs from pricing.price_cart
    slow_cart_query: bool = False  # cart query doing a full table scan
    slow_serialize_span: bool = False


@dataclass(frozen=True)
class Template:
    id: str
    title: str
    summary: str  # one sentence: what the bad commit does
    category: str
    root_cause_file: str
    commits: tuple[Commit, ...]
    bad_key: str
    last_good_key: str
    deploys: tuple[tuple[str, str, str], ...]
    fix_files: dict[str, str] = field(default_factory=dict)  # path -> content of an acceptable fix
    acceptable_fixes: tuple[tuple[str, str], ...] = ()
    profile: Profile = field(default_factory=Profile)
    min_p95_ratio: float = 2.0  # benchmark must regress at least this much (sanity of the template)
    expected_tools: tuple[str, ...] = ("list_deployments", "compare_time_windows", "get_diff")
    red_herring: str = "inventory_audit slow query (constant rate)"
    red_herring_applies: bool = True


def _tail(tid: str, note: str, test_name: str, test_body: str) -> list[Commit]:
    readme = BASE_FILES["README.md"] + f"\n## Changelog\n\n{note}\n"
    return [
        Commit(f"{tid}-docs", f"docs: {note.lower()[:50]}", "2026-03-09T15:00:00+0000", ASHA,
               {"README.md": readme}),
        Commit(f"{tid}-test", f"test: {test_name}", "2026-03-10T10:00:00+0000", DEV,
               {f"tests/test_{tid}.py": test_body}),
        Commit(f"{tid}-version", "chore: bump version", "2026-03-10T16:00:00+0000", ASHA,
               {"checkout/__init__.py": '"""Checkout service."""\n\n__version__ = "1.2.1"\n'}),
    ]  # fmt: skip


def _history(tid: str, bad: Commit, note: str, test_name: str, test_body: str,
             pre: tuple[Commit, ...] = ()) -> tuple[Commit, ...]:  # fmt: skip
    return (*BASE, *pre, bad, *_tail(tid, note, test_name, test_body))


def _deploys(tid: str) -> tuple[tuple[str, str, str], ...]:
    return (DEPLOYS[0], DEPLOYS[1], ("v1.2.0", f"{tid}-test", LAST_DEPLOY_TS))


def _swap(path: str, old: str, new: str) -> str:
    text = BASE_FILES[path]
    assert old in text, f"{path}: {old!r} not found"
    return text.replace(old, new, 1)


_QUERY_OLD = BASE_FILES["checkout/db.py"][
    BASE_FILES["checkout/db.py"].index("    def query") : BASE_FILES["checkout/db.py"].index(
        "    def _seed"
    )
]
_QUERY_NEW = """    def query(self, sql, params=(), extra_ms=0.0):
        params = tuple(params)
        plan = " ".join(str(r[3]) for r in self._conn.execute("EXPLAIN QUERY PLAN " + sql, params))
        rows = self._conn.execute(sql, params).fetchall()
        scanned = len(rows)
        match = re.search(r"\\bSCAN (\\w+)", plan)
        if match:  # a full table scan reads every row of the table
            total = self._conn.execute(f"SELECT count(*) FROM {match.group(1)}").fetchone()[0]
            scanned = max(scanned, total)
        self._meter.db_queries += 1
        self._meter.add(DB_QUERY_BASE_MS + DB_ROW_MS * scanned + extra_ms)
        return rows

"""
SCAN_AWARE_DB = (
    BASE_FILES["checkout/db.py"]
    .replace(_QUERY_OLD, _QUERY_NEW)
    .replace("import sqlite3\n", "import re\nimport sqlite3\n", 1)
)
NO_INDEX_DB = SCAN_AWARE_DB.replace(
    "CREATE INDEX idx_cart_items_cart ON cart_items (cart_id);\n", "", 1
)
assert NO_INDEX_DB != SCAN_AWARE_DB and "SCAN" in SCAN_AWARE_DB


def _smoke_test(import_line: str, body: str) -> str:
    return f"{import_line}\n\n\n{body}\n"


# --------------------------------------------------------------------------- templates
CACHE_DISABLED = Template(
    id="cache_disabled",
    title="Cache capacity default set to 0",
    summary="A 'configurable capacity' refactor made max_entries default to 0, so every cache set is immediately evicted.",
    category="cache_misconfiguration",
    root_cause_file="checkout/cache.py",
    commits=_history(
        "cache_disabled",
        Commit(
            "bad",
            "refactor(cache): make capacity configurable (0 = unbounded)",
            "2026-03-09T10:00:00+0000",
            DEV,
            {
                "checkout/cache.py": _swap(
                    "checkout/cache.py", "max_entries: int = 1024", "max_entries: int = 0"
                )
            },
        ),
        "Cache capacity is now configurable.",
        "cache keeps entries",
        _smoke_test(
            "from checkout.cache import Cache\nfrom checkout.costs import Meter",
            "def test_cache_get_set_roundtrip_within_a_call():\n    c = Cache(Meter(), max_entries=4)\n"
            "    c.set('a', 1)\n    assert c.get('a') == 1",
        ),
    ),
    bad_key="bad",
    last_good_key="region",
    deploys=_deploys("cache_disabled"),
    fix_files={"checkout/cache.py": BASE_FILES["checkout/cache.py"]},
    acceptable_fixes=(
        ("restore_default_capacity", "default max_entries back to a positive capacity"),
        ("treat_zero_as_unbounded", "make 0 mean unbounded as the commit message claims"),
    ),
    profile=Profile(queries_after=lambda k: 3, cache_hits_after=lambda k: 0),
    min_p95_ratio=1.5,
    expected_tools=("list_deployments", "compare_time_windows", "get_diff", "get_file"),
)

SERIALIZATION_QUADRATIC = Template(
    id="serialization_quadratic",
    title="Receipt serialization became quadratic",
    summary="A serializer rewrite re-encodes the growing prefix for every item, so cost grows with items squared.",
    category="algorithmic_complexity",
    root_cause_file="checkout/serializer.py",
    commits=_history(
        "serialization_quadratic",
        Commit(
            "bad",
            "refactor(serializer): stream items so partial receipts can be flushed",
            "2026-03-09T10:00:00+0000",
            DEV,
            {
                "checkout/serializer.py": '''"""Receipt serialization (streaming)."""

import json

from checkout.costs import SERIALIZE_BASE_MS, SERIALIZE_ITEM_MS


def serialize_receipt(ctx, receipt):
    ctx.meter.add(SERIALIZE_BASE_MS)
    items = receipt["items"]
    encoded = []
    for i, item in enumerate(items):
        # flush-safe: re-encode everything seen so far so a partial receipt is always valid
        ctx.meter.add(SERIALIZE_ITEM_MS * 15 * (i + 1))
        encoded = [json.dumps(it, sort_keys=True, separators=(",", ":")) for it in items[: i + 1]]
    rest = {k: v for k, v in receipt.items() if k != "items"}
    return json.dumps(rest, sort_keys=True, separators=(",", ":"))[:-1] + ',"items":[' + ",".join(encoded) + "]}"
'''
            },
        ),
        "Receipts stream item by item.",
        "receipt serialization is valid json",
        _smoke_test(
            "import json\n\nfrom checkout.service import handle_checkout, make_context",
            "def test_receipt_is_valid_json():\n    receipt = json.loads(handle_checkout(make_context(), 3))\n"
            "    assert receipt['cart_id'] == 3 and receipt['items']",
        ),
    ),
    bad_key="bad",
    last_good_key="region",
    deploys=_deploys("serialization_quadratic"),
    fix_files={"checkout/serializer.py": BASE_FILES["checkout/serializer.py"]},
    acceptable_fixes=(
        ("encode_once", "encode each item once (linear), restore the single json.dumps"),
    ),
    profile=Profile(extra_latency_ms=lambda k: 3.0 * k * (k + 1) / 2, slow_serialize_span=True),
    min_p95_ratio=2.0,
    expected_tools=("list_deployments", "compare_time_windows", "get_trace", "get_diff"),
)

AUDIT_EVERY_REQUEST = Template(
    id="audit_every_request",
    title="Inventory audit runs on every request",
    summary="AUDIT_EVERY was changed from 25 to 1, so the slow full-table audit scan runs on every checkout.",
    category="unintended_work_per_request",
    root_cause_file="checkout/audit.py",
    commits=_history(
        "audit_every_request",
        Commit(
            "bad",
            "fix(audit): audit every checkout so stock counts never drift",
            "2026-03-09T10:00:00+0000",
            DEV,
            {
                "checkout/audit.py": _swap(
                    "checkout/audit.py", "AUDIT_EVERY = 25", "AUDIT_EVERY = 1"
                )
            },
        ),
        "Inventory audit cadence tightened.",
        "audit returns a count",
        _smoke_test(
            "from checkout.audit import maybe_audit\nfrom checkout.service import make_context",
            "def test_audit_returns_a_count_when_it_runs():\n    ctx = make_context()\n    ctx.request_count = 25\n"
            "    assert isinstance(maybe_audit(ctx), int)",
        ),
    ),
    bad_key="bad",
    last_good_key="region",
    deploys=_deploys("audit_every_request"),
    fix_files={"checkout/audit.py": BASE_FILES["checkout/audit.py"]},
    acceptable_fixes=(
        ("restore_cadence", "AUDIT_EVERY back to 25"),
        ("move_off_hot_path", "run the audit asynchronously / off the request path"),
    ),
    profile=Profile(queries_after=lambda k: 3, audit_every_request=True),
    min_p95_ratio=3.0,
    red_herring="the audit slow-query warnings are the CAUSE here (rate jumped), not a constant-rate red herring",
    red_herring_applies=False,
    expected_tools=("list_deployments", "compare_time_windows", "search_logs", "get_diff"),
)

MISSING_INDEX = Template(
    id="missing_index",
    title="Index on cart_items dropped",
    summary="A schema cleanup removed the index on cart_items(cart_id), so the cart query scans the whole table.",
    category="missing_index",
    root_cause_file="checkout/db.py",
    commits=_history(
        "missing_index",
        Commit(
            "bad",
            "chore(db): drop redundant cart_items index",
            "2026-03-09T10:00:00+0000",
            DEV,
            {"checkout/db.py": NO_INDEX_DB},
        ),
        "Schema cleanup.",
        "cart items load",
        _smoke_test(
            "from checkout.service import handle_checkout, make_context",
            "def test_cart_loads():\n    assert 'items' in handle_checkout(make_context(), 7)",
        ),
        pre=(
            Commit(
                "scan-aware-db",
                "perf(db): charge full table scans in the cost model",
                "2026-03-08T10:00:00+0000",
                ASHA,
                {"checkout/db.py": SCAN_AWARE_DB},
            ),
        ),
    ),
    bad_key="bad",
    last_good_key="scan-aware-db",
    deploys=_deploys("missing_index"),
    fix_files={"checkout/db.py": SCAN_AWARE_DB},
    acceptable_fixes=(("recreate_index", "CREATE INDEX on cart_items(cart_id)"),),
    profile=Profile(extra_latency_ms=lambda k: 160.0, slow_cart_query=True),
    min_p95_ratio=3.0,
    expected_tools=(
        "list_deployments",
        "compare_time_windows",
        "get_trace",
        "get_diff",
        "explain_query",
    ),
)

PRICING_PER_ITEM_QUERY = Template(
    id="pricing_per_item_query",
    title="Pricing re-validates every price with a query",
    summary="price_cart now re-reads each product price from the database 'to avoid stale cache', one query per item.",
    category="n_plus_one_query",
    root_cause_file="checkout/pricing.py",
    commits=_history(
        "pricing_per_item_query",
        Commit(
            "bad",
            "fix(pricing): re-read prices from the database to avoid stale totals",
            "2026-03-09T10:00:00+0000",
            DEV,
            {
                "checkout/pricing.py": _swap(
                    "checkout/pricing.py",
                    '    subtotal = sum(p["price_cents"] * q for p, q in zip(products, qtys, strict=True))\n',
                    "    subtotal = 0\n    for p, q in zip(products, qtys, strict=True):\n"
                    '        row = ctx.db.query("SELECT price_cents FROM products WHERE id = ?", (p["id"],))\n'
                    "        subtotal += row[0][0] * q\n",
                )
            },
        ),
        "Prices are re-validated.",
        "totals add up",
        _smoke_test(
            "import json\n\nfrom checkout.service import handle_checkout, make_context",
            "def test_totals_add_up():\n    r = json.loads(handle_checkout(make_context(), 9))\n"
            "    assert r['total_cents'] == r['subtotal_cents'] + r['tax_cents']",
        ),
    ),
    bad_key="bad",
    last_good_key="region",
    deploys=_deploys("pricing_per_item_query"),
    fix_files={"checkout/pricing.py": BASE_FILES["checkout/pricing.py"]},
    acceptable_fixes=(
        ("use_cached_prices", "compute the subtotal from the already-loaded products"),
        ("batch_revalidation", "if revalidation is required, do it in ONE query"),
    ),
    profile=Profile(queries_after=lambda k: 2 + k, pricing_query_spans=True),
    min_p95_ratio=2.0,
    expected_tools=("list_deployments", "compare_time_windows", "get_trace", "get_diff"),
)

N_PLUS_ONE = Template(
    id="n_plus_one",
    title="Product lookup became N+1 and bypassed the cache",
    summary="get_products replaced a batched cache-aside lookup with one SELECT per product.",
    category="n_plus_one_query",
    root_cause_file="checkout/catalog.py",
    commits=tuple(COMMITS),
    bad_key="n-plus-one",
    last_good_key="region",
    deploys=tuple(DEPLOYS),
    fix_files={"checkout/catalog.py": CATALOG_GOOD},
    acceptable_fixes=(
        ("restore_batched_lookup", "single IN (...) query"),
        ("restore_cache_aside", "use the cache for product lookups"),
    ),
    profile=Profile(
        queries_after=lambda k: 1 + k, cache_hits_after=lambda k: 0, product_query_spans=True
    ),
    min_p95_ratio=2.5,
    expected_tools=("list_deployments", "compare_time_windows", "get_trace", "get_diff"),
)

TEMPLATES: dict[str, Template] = {
    t.id: t for t in (N_PLUS_ONE, CACHE_DISABLED, SERIALIZATION_QUADRATIC, AUDIT_EVERY_REQUEST,
                      MISSING_INDEX, PRICING_PER_ITEM_QUERY)
}  # fmt: skip
