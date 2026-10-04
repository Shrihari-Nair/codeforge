# Diagnosing a latency regression after a deploy

1. Find when the step change started and line it up with deployments.
2. Compare a window before and after: p50, p95, p99, error rate, throughput.
3. Look at what changed in the request profile: database queries per request, cache hits.
4. Open a slow trace and look for repeated spans.
5. Diff the commits in the deployed range, starting with code on the hot path.

## Beware of red herrings
A slow query that was already there before the deploy is not the cause. Check that its rate is
the same before and after. Tail latency (p99) can be dominated by an unrelated slow path while
the median (p50, p95) regresses for a different reason.
