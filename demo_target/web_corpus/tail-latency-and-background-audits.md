# Tail latency from periodic background work

Periodic jobs such as inventory audits or full-table scans add latency to a small fraction of
requests. They raise p99 but barely move p95 or the median. Optimising them is worthwhile, but it
will not fix a regression that shows up across most requests.
