# The N+1 query problem

An N+1 query happens when code loads a list of N items with one query, then issues one more query
per item to load related data. A request that should cost 2 queries ends up costing N+1.

## Symptoms
- Latency grows with the size of the list (cart size, page size).
- The number of database queries per request jumps after a refactor.
- Traces show many identical `SELECT ... WHERE id = ?` spans back to back.

## Fixes
- Batch the lookups into one `WHERE id IN (...)` query.
- Join the related table in the original query.
- Put a cache in front of per-item lookups so repeat reads skip the database.

```python
rows = db.query("SELECT * FROM products WHERE id IN (%s)" % marks, ids)
```

Learn more at [query batching](https://example.com/batching).
