import uuid

import psycopg
import pytest

from aeos.core.config import get_settings
from aeos.memory.embedder import HashEmbedder
from aeos.memory.gate import FactProposal
from aeos.memory.store import Episode, MemoryStore


@pytest.fixture
async def store(seeded_db: None) -> MemoryStore:
    s = MemoryStore(HashEmbedder())
    await s.ensure_schema()
    with psycopg.connect(get_settings().database_url) as conn:  # this test module owns memory
        conn.execute("TRUNCATE aeos.episodes, aeos.facts")
    return s


def ep(task: str | None = None, goal: str = "checkout latency regression after deploy",
       rc: str = "N+1 queries in catalog get_products", outcome: str = "completed") -> Episode:  # fmt: skip
    return Episode(
        task_id=task or f"t-{uuid.uuid4().hex[:6]}",
        goal=goal,
        outcome=outcome,  # type: ignore[arg-type]
        summary={
            "root_cause_file": "checkout/catalog.py",
            "root_cause_summary": rc,
            "fix_summary": "restored batched cache-aside lookup",
        },  # fmt: skip
        lessons=["fixing the audit scan did not help"],
    )


def fp(subject: str = "checkout-service", predicate: str = "cache", value: str = "redis",
       **kw: object) -> FactProposal:  # fmt: skip
    return FactProposal(
        subject=subject, predicate=predicate, value=value, confidence=0.9, evidence=["c1"], **kw
    )  # type: ignore[arg-type]


# ------------------------------------------------------------------ episodic
async def test_episode_roundtrip_and_semantic_retrieval(store: MemoryStore) -> None:
    await store.save_episode(ep(task="t-a"))
    await store.save_episode(ep(task="t-b", goal="rotate the TLS certificates on the edge proxy",
                                rc="certificate expired"))  # fmt: skip
    hits = await store.search_episodes("checkout latency regression after the deploy", k=2)
    assert hits[0].task_id == "t-a" and hits[0].summary["root_cause_file"] == "checkout/catalog.py"
    assert hits[0].similarity > 0.3
    assert hits[0].summary["lessons"] == ["fixing the audit scan did not help"]
    assert all(h.task_id != "t-b" or h.similarity < hits[0].similarity for h in hits)


async def test_save_episode_is_idempotent_per_task(store: MemoryStore) -> None:
    assert await store.save_episode(ep(task="t-same")) is True
    assert await store.save_episode(ep(task="t-same")) is False
    assert await store.episode_count() == 1


async def test_unrelated_queries_return_nothing_above_the_similarity_floor(
    store: MemoryStore,
) -> None:
    await store.save_episode(ep())
    assert (
        await store.search_episodes("quarterly marketing budget spreadsheet", min_similarity=0.3)
        == []
    )


async def test_episode_outcome_filter(store: MemoryStore) -> None:
    await store.save_episode(ep(task="t-ok", outcome="completed"))
    await store.save_episode(ep(task="t-bad", outcome="escalated"))
    hits = await store.search_episodes("checkout latency regression", k=5, outcome="escalated")
    assert [h.task_id for h in hits] == ["t-bad"]


# ------------------------------------------------------------------ semantic
async def test_new_fact_then_same_fact_only_confirms(store: MemoryStore) -> None:
    first = await store.upsert_fact(fp(), "t1")
    again = await store.upsert_fact(fp(), "t2")
    assert first.kind == "created" and again.kind == "confirmed" and again.fact_id == first.fact_id
    assert len(await store.all_facts("checkout-service")) == 1  # no duplicate row


async def test_contradicting_fact_supersedes_instead_of_duplicating(store: MemoryStore) -> None:
    old = await store.upsert_fact(fp(value="redis"), "t1")
    new = await store.upsert_fact(fp(value="memcached"), "t2")
    assert new.kind == "superseded" and new.superseded_id == old.fact_id
    active = await store.active_fact("checkout-service", "cache")
    assert active is not None and active.value == "memcached" and active.id == new.fact_id
    history = {f.id: f for f in await store.all_facts("checkout-service")}
    assert history[old.fact_id].status == "superseded"
    assert history[old.fact_id].superseded_by == new.fact_id
    hits = await store.search_facts("which cache does checkout-service use")
    assert [h.value for h in hits] == ["memcached"]  # stale facts are never returned


async def test_chain_of_supersessions_keeps_exactly_one_active_fact(store: MemoryStore) -> None:
    for v in ("redis", "memcached", "in-process dict", "redis"):
        await store.upsert_fact(fp(value=v), "t")
    facts = await store.all_facts("checkout-service")
    assert [f.status for f in facts].count("active") == 1 and len(facts) == 4
    assert (await store.active_fact("checkout-service", "cache")).value == "redis"  # type: ignore[union-attr]


async def test_different_predicates_do_not_supersede_each_other(store: MemoryStore) -> None:
    await store.upsert_fact(fp(predicate="cache", value="redis"), "t1")
    await store.upsert_fact(fp(predicate="payments_timeout", value="5s"), "t1")
    assert len([f for f in await store.all_facts() if f.status == "active"]) == 2


async def test_explicit_invalidation(store: MemoryStore) -> None:
    f = await store.upsert_fact(fp(), "t1")
    assert await store.invalidate_fact(f.fact_id, "service migrated") is True
    assert await store.invalidate_fact(f.fact_id, "again") is False
    assert await store.active_fact("checkout-service", "cache") is None
    assert await store.search_facts("cache redis") == []
