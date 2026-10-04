"""Deterministic offline research corpus: a markdown directory served over local HTTP.

`LocalCorpusSearch` is the default `search_web` provider (a real web search provider can be
plugged in behind the same interface). `python -m aeos.mcp_servers.web.corpus` serves the
directory so `fetch_page` exercises real HTTP.
"""

import functools
import http.server
import re
import sys
from pathlib import Path

from pydantic import BaseModel

_TOKEN = re.compile(r"[a-z0-9_]+")


class SearchHit(BaseModel):
    title: str
    url: str
    snippet: str
    score: float


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class LocalCorpusSearch:
    name = "local-corpus"

    def __init__(self, corpus_dir: Path, base_url: str) -> None:
        self._docs: list[tuple[str, str, str, list[str]]] = []
        for path in sorted(corpus_dir.glob("*.md")):
            text = path.read_text()
            title = next(
                (ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("# ")),
                path.stem,
            )
            self._docs.append((title, f"{base_url.rstrip('/')}/{path.name}", text, _tokens(text)))

    def search(self, query: str, limit: int) -> list[SearchHit]:
        q = set(_tokens(query))
        hits: list[SearchHit] = []
        for title, url, text, toks in self._docs:
            if not q:
                break
            title_toks = set(_tokens(title))
            score = sum(toks.count(t) for t in q) + 5 * len(q & title_toks)
            if score:
                hits.append(SearchHit(title=title, url=url, snippet=_snippet(text, q), score=score))
        hits.sort(key=lambda h: (-h.score, h.url))
        return hits[:limit]


def _snippet(text: str, q: set[str]) -> str:
    for line in text.splitlines():
        if line.strip() and not line.startswith("#") and q & set(_tokens(line)):
            return line.strip()[:200]
    return ""


def serve_corpus(corpus_dir: Path, port: int) -> None:  # pragma: no cover - thin wrapper
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(corpus_dir))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":  # pragma: no cover
    serve_corpus(
        Path(sys.argv[1] if len(sys.argv) > 1 else "demo_target/web_corpus"),
        int(sys.argv[2]) if len(sys.argv) > 2 else 8765,
    )
