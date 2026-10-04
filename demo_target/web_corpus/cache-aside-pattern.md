# Cache-aside pattern

With cache-aside, the application checks the cache first. On a miss it reads the database and
writes the result back to the cache. Repeat reads are then served from memory.

## Pitfalls
- Bypassing the cache in a refactor silently turns every read into a database query.
- Unbounded caches grow without limit; cap the number of entries.
- Cache stampedes: many misses for the same key at once.

Cache hit ratio is the first number to check when read latency regresses.
