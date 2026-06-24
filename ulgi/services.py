"""Cienkie wrappery na rdzeń RAG dla widoków."""

from __future__ import annotations

from typing import Any


def search_docs(query: str, **filters: Any) -> list[dict]:
    from search import retrieve

    return retrieve(query, **filters)


def qualify(opis: str, **kwargs: Any) -> dict:
    from qualification import assess

    return assess(opis, **kwargs)


def answer(query: str, *, model: str | None = None, **filters: Any) -> dict:
    """Odpowiedź RAG z semantycznym cache na Redisie.

    Zwraca {'answer', 'sources', 'cache'} — gdzie 'cache' to nazwa warstwy
    trafienia ('exact' / 'semantic:0.97') albo None, jeśli liczono od nowa.
    """
    from .cache import get_cache

    cache = get_cache()
    hit = cache.get(query, filters, model)
    if hit:
        response, sources, layer = hit
        return {"answer": response, "sources": sources, "cache": layer}

    from search import answer as core_answer

    out = core_answer(query, model=model, **filters)
    cache.set(query, filters, model, out.get("answer", ""), out.get("sources", []))
    out["cache"] = None
    return out
