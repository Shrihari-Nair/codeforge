# demo_target: seeded checkout-service

A small service with a planted latency regression, plus matching observability data.

- `history.py`: the 20-commit history (source of truth). `build_repo.py` turns it into a real git repo at
  `demo_target/build/checkout-service` with fixed authors/dates, so SHAs are reproducible.
  Run: `uv run python -m demo_target.build_repo`
- `datagen.py` + `seed.py`: seeded logs/metrics/spans/deployments loaded into Postgres schema `demo`.
  Run: `make up && uv run python -m demo_target.seed`
- `GROUND_TRUTH.json`: machine-readable root cause, red herrings, acceptable fixes, benchmark thresholds (used by evals).

Latency in the service is measured by a deterministic cost meter (counted work, not wall-clock).
Benchmark: `python benchmark.py --json` inside the service tree.

## Finding the bug by hand
```sql
-- 1. when did latency step up?
SELECT date_trunc('hour', ts) h, round(avg(p95_ms)) p95 FROM demo.metrics GROUP BY 1 ORDER BY 1;
-- 2. what deployed then?
SELECT ts, version, commit_sha FROM demo.deployments ORDER BY ts;
-- 3. what changed in the request profile?
SELECT ts >= '2026-03-10 14:00+00' AS after, avg((attrs->>'db_queries')::int), avg((attrs->>'cache_hits')::int)
FROM demo.logs WHERE message = 'request completed' GROUP BY 1;
-- 4. drill into a slow trace (look for repeated per-product SELECTs)
SELECT name, duration_ms, attrs->>'db.statement' FROM demo.spans WHERE trace_id = '<id>' ORDER BY start_ts;
```
Then diff the commits in the v1.1.0..v1.2.0 range. Beware the red herrings (`inventory_audit` slow query, the
cache-timeout error burst).
