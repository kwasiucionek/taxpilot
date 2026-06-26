"""
search.py — wyszukiwanie hybrydowe + generacja odpowiedzi (RAG).

retrieve()  — hybryda BM25+kNN z filtrami (ulga, akt, typ źródła,
              „stan prawny na dzień").
answer()    — buduje kontekst z cytatami i generuje odpowiedź na Ollama;
              instrukcja systemowa wymusza powoływanie się na podstawę prawną
              i odmowę, gdy kontekst nie zawiera odpowiedzi.
"""

from __future__ import annotations

import logging

import requests

from config import (
    DEFAULT_OLLAMA_MODEL,
    OLLAMA_CLOUD_API_KEY,
    OLLAMA_URL,
    OPENSEARCH_INDEX,
    TOP_K,
)
from embedder import embed_query
from opensearch_schema import (
    HYBRID_PIPELINE_ID,
    build_filters,
    get_client,
    hits_to_docs,
    hybrid_body,
)

logger = logging.getLogger(__name__)


def retrieve(
    query: str,
    *,
    k: int = TOP_K,
    akt: str | None = None,
    ulga: str | None = None,
    source_types: list[str] | None = None,
    on_date: str | None = None,
    use_hybrid: bool = True,
) -> list[dict]:
    client = get_client()
    vector = embed_query(query)
    filters = build_filters(akt=akt, ulga=ulga, source_types=source_types, on_date=on_date)
    body = hybrid_body(query, vector, k, filters or None, use_hybrid=use_hybrid)

    params = {}
    if use_hybrid and "hybrid" in body.get("query", {}):
        params["search_pipeline"] = HYBRID_PIPELINE_ID

    try:
        resp = client.search(index=OPENSEARCH_INDEX, body=body, params=params)
    except Exception as e:  # noqa: BLE001
        logger.warning("Hybryda nieudana (%s) — fallback do kNN.", e)
        body = hybrid_body(query, vector, k, filters or None, use_hybrid=False)
        resp = client.search(index=OPENSEARCH_INDEX, body=body)

    return hits_to_docs(resp["hits"]["hits"])


def retrieve_mixed(
    query: str,
    *,
    per_source: dict[str, int] | None = None,
    ulga: str | None = None,
    on_date: str | None = None,
    use_hybrid: bool = True,
) -> list[dict]:
    """Retrieval z kwotą per typ źródła.

    Pobiera OSOBNO z ustaw, objaśnień i interpretacji (każde z własnym filtrem
    source_type i własnym k z RETRIEVE_MIX), po czym scala w kolejności
    ustawa → objaśnienia → interpretacja. Gwarantuje obecność przepisu w
    źródłach — inaczej interpretacje (długa proza zbliżona do pytań, ~75%
    indeksu) dominują globalne top-k i wypychają suche przepisy ustawy.
    """
    from config import (
        RETRIEVE_MIX,
        SOURCE_INTERPRETACJA,
        SOURCE_OBJASNIENIA,
        SOURCE_USTAWA,
    )

    plan = per_source or RETRIEVE_MIX
    order = (SOURCE_USTAWA, SOURCE_OBJASNIENIA, SOURCE_INTERPRETACJA)

    out: list[dict] = []
    seen: set[str] = set()
    for stype in order:
        k = plan.get(stype, 0)
        if k <= 0:
            continue
        for d in retrieve(
            query,
            k=k,
            ulga=ulga,
            source_types=[stype],
            on_date=on_date,
            use_hybrid=use_hybrid,
        ):
            key = d.get("citation") or d.get("content_text", "")[:80]
            if key in seen:
                continue
            seen.add(key)
            out.append(d)
    return out


# ─────────────────────────── GENERACJA ───────────────────────────

_SYSTEM = (
    "Jesteś asystentem prawno-podatkowym wspierającym doradcę. Specjalizujesz "
    "się w ulgach: B+R (art. 18d CIT / 26e PIT), IP Box (art. 24d CIT / 30ca PIT) "
    "oraz kosztach autorskich (50% KUP). Odpowiadaj po polsku, precyzyjnie. "
    "ZAWSZE powołuj się na konkretną podstawę prawną z kontekstu (np. art. 18d "
    "ust. 2 ustawy o CIT) oraz sygnatury interpretacji/wyroków, jeśli są. "
    "Jeśli kontekst nie zawiera odpowiedzi — powiedz to wprost i nie zgaduj. "
    "Zaznacz, że to wsparcie informacyjne, a nie wiążąca porada podatkowa."
)


def _build_context(docs: list[dict]) -> str:
    blocks = []
    for d in docs:
        cite = d.get("citation") or d.get("sygnatura") or d.get("eli_id", "")
        blocks.append(f"[{cite}]\n{d.get('content_text', '')}")
    return "\n\n---\n\n".join(blocks)


def _ollama_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {OLLAMA_CLOUD_API_KEY}"} if OLLAMA_CLOUD_API_KEY else {}


def build_chat_payload(
    query: str,
    docs: list[dict],
    *,
    model: str | None = None,
    stream: bool = False,
) -> dict:
    """Buduje payload czatu Ollamy (system + pytanie + kontekst).

    Jedno źródło prawdy dla wariantu blokującego (`answer`) i strumieniowego
    (widok SSE), żeby prompt i kontekst nie rozjechały się między nimi.
    """
    context = _build_context(docs)
    return {
        "model": model or DEFAULT_OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": f"Pytanie: {query}\n\nKontekst (podstawy prawne):\n{context}",
            },
        ],
        "stream": stream,
    }


def answer(
    query: str,
    *,
    model: str | None = None,
    k: int = TOP_K,
    **retrieve_kwargs,
) -> dict:
    """Zwraca {'answer': str, 'sources': list[dict]}."""
    docs = retrieve(query, k=k, **retrieve_kwargs)
    if not docs:
        return {
            "answer": "Brak materiału w bazie do odpowiedzi na to pytanie.",
            "sources": [],
        }

    payload = build_chat_payload(query, docs, model=model, stream=False)
    r = requests.post(
        f"{OLLAMA_URL}/api/chat",
        headers=_ollama_headers(),
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    text = r.json().get("message", {}).get("content", "")
    return {
        "answer": text,
        "sources": [
            {"citation": d.get("citation"), "ulga": d.get("ulga"), "score": d.get("_score")}
            for d in docs
        ],
    }


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "Jakie koszty są kwalifikowane w uldze B+R?"
    out = answer(q, ulga="BR")
    print(out["answer"])
    print("\nŹródła:")
    for s in out["sources"]:
        print(" -", s["citation"], f"({s['score']:.3f})" if s.get("score") else "")
