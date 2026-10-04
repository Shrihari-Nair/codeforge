"""Text embeddings. `HashEmbedder` is deterministic and offline (tests/CI); `GeminiEmbedder` is the
live implementation. Both produce L2-normalised vectors of the same dimension."""

import hashlib
import math
import re
from typing import Protocol

DIM = 768
_TOKEN = re.compile(r"[a-z0-9_]+")


class Embedder(Protocol):
    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]: ...


def _normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class HashEmbedder:
    """Signed feature hashing of unigrams + bigrams. Similar texts -> similar vectors."""

    def __init__(self, dim: int = DIM) -> None:
        self.dim = dim

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        tokens = _TOKEN.findall(text.lower())
        feats = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:], strict=False)]
        vec = [0.0] * self.dim
        for f in feats:
            h = hashlib.blake2b(f.encode(), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "big") % self.dim
            vec[idx] += 1.0 if h[4] & 1 else -1.0
        return _normalise(vec)


class GeminiEmbedder:
    def __init__(self, api_key: str, model: str = "gemini-embedding-001", dim: int = DIM) -> None:
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self.model, self.dim = model, dim

    async def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        from google.genai import types

        resp = await self._client.aio.models.embed_content(
            model=self.model,
            contents=texts,
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT",
                output_dimensionality=self.dim,
            ),
        )
        return [_normalise(list(e.values or [])) for e in (resp.embeddings or [])]
