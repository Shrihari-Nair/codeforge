import math

from aeos.memory.embedder import DIM, HashEmbedder


def cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


async def test_deterministic_normalised_and_right_dimension() -> None:
    e = HashEmbedder()
    (a,), (b,) = await e.embed(["N+1 query in catalog"]), await e.embed(["N+1 query in catalog"])
    assert a == b and len(a) == DIM
    assert math.isclose(sum(v * v for v in a), 1.0, rel_tol=1e-6)


async def test_similar_text_is_closer_than_unrelated_text() -> None:
    e = HashEmbedder()
    q, near, far = await e.embed([
        "checkout latency increased after deploy, N+1 queries in the product catalog",
        "latency regression in checkout caused by N+1 product catalog queries",
        "quarterly marketing budget spreadsheet for the sales team",
    ])  # fmt: skip
    assert cos(q, near) > 0.4 > cos(q, far)


async def test_empty_text_does_not_crash() -> None:
    (v,) = await HashEmbedder().embed([""])
    assert len(v) == DIM
