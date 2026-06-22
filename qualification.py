"""
qualification.py — asystent kwalifikacji ulg (feature „wow").

Wejście: opis działalności (np. czym zajmuje się zespół/programista).
Wyjście: ocena kwalifikacji do ulgi B+R i/lub IP Box, ugruntowana w
pobranych przepisach i objaśnieniach, z konkretną podstawą prawną.

To NIE jest wiążąca porada — to wsparcie decyzji doradcy. Model jest
zmuszony cytować podstawę z kontekstu i wskazywać, czego brakuje do
jednoznacznej oceny.
"""

from __future__ import annotations

import json
import logging

import requests

from config import (
    DEFAULT_OLLAMA_MODEL,
    OLLAMA_CLOUD_API_KEY,
    OLLAMA_URL,
    SOURCE_INTERPRETACJA,
    SOURCE_OBJASNIENIA,
    SOURCE_USTAWA,
)
from search import retrieve

logger = logging.getLogger(__name__)

# Ulgi rozpatrywane przy ocenie działalności (PKUP zostawiamy poza zakresem
# asystenta — dotyczy formy wynagrodzenia, nie charakteru prac).
_DEFAULT_ULGI = ["BR", "IPBOX"]

_SYSTEM = (
    "Jesteś asystentem prawno-podatkowym wspierającym doradcę w ocenie, czy "
    "opisana działalność kwalifikuje się do ulgi B+R lub IP Box. Oceniaj "
    "WYŁĄCZNIE na podstawie dostarczonego kontekstu (przepisy, objaśnienia, "
    "interpretacje). Dla każdej ulgi wskaż podstawę prawną z kontekstu "
    "(np. art. 18d ustawy o CIT, objaśnienia MF, sygnatura interpretacji). "
    "Jeśli z opisu nie wynika spełnienie przesłanki (np. systematyczność, "
    "twórczy charakter, zwiększanie zasobów wiedzy) — napisz, czego brakuje, "
    "zamiast zgadywać. To wsparcie informacyjne, nie wiążąca porada podatkowa."
)

# Model proszony o zwięzły JSON — łatwy do wyrenderowania w UI.
_FORMAT = (
    "Zwróć WYŁĄCZNIE poprawny JSON (bez ```), w schemacie:\n"
    "{\n"
    '  "oceny": [\n'
    "    {\n"
    '      "ulga": "B+R" | "IP Box",\n'
    '      "werdykt": "kwalifikuje" | "częściowo" | "nie kwalifikuje" | "za mało danych",\n'
    '      "uzasadnienie": "2-4 zdania",\n'
    '      "podstawa_prawna": ["art. 18d ust. 1 ustawy o CIT", "..."],\n'
    '      "czego_brakuje": ["pytanie lub brakująca przesłanka", "..."]\n'
    "    }\n"
    "  ],\n"
    '  "zastrzezenie": "krótka nota, że to nie wiążąca porada"\n'
    "}"
)


def _ctx(docs: list[dict]) -> str:
    blocks = []
    for d in docs:
        cite = d.get("citation") or d.get("sygnatura") or d.get("eli_id", "")
        blocks.append(f"[{cite}]\n{d.get('content_text', '')}")
    return "\n\n---\n\n".join(blocks)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {OLLAMA_CLOUD_API_KEY}"} if OLLAMA_CLOUD_API_KEY else {}


def assess(
    opis: str,
    *,
    ulgi: list[str] | None = None,
    k_per_ulga: int = 6,
    model: str | None = None,
    on_date: str | None = None,
) -> dict:
    """Ocena kwalifikacji. Zwraca {'ocena': {...}, 'sources': [...]}"""
    ulgi = ulgi or _DEFAULT_ULGI
    source_types = [SOURCE_USTAWA, SOURCE_OBJASNIENIA, SOURCE_INTERPRETACJA]

    # Retrieval per ulga — opis działalności jako zapytanie, filtr po uldze.
    docs: list[dict] = []
    seen: set[str] = set()
    for code in ulgi:
        for d in retrieve(
            opis, k=k_per_ulga, ulga=code, source_types=source_types, on_date=on_date
        ):
            key = d.get("citation") or d.get("content_text", "")[:60]
            if key not in seen:
                seen.add(key)
                docs.append(d)

    if not docs:
        return {
            "ocena": {
                "oceny": [],
                "zastrzezenie": "Brak materiału w bazie — najpierw zaindeksuj akty i objaśnienia.",
            },
            "sources": [],
        }

    payload = {
        "model": model or DEFAULT_OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Opis działalności:\n{opis}\n\n"
                    f"Kontekst (przepisy / objaśnienia / interpretacje):\n{_ctx(docs)}\n\n"
                    f"{_FORMAT}"
                ),
            },
        ],
        "stream": False,
        "format": "json",
    }
    r = requests.post(
        f"{OLLAMA_URL}/api/chat", headers=_headers(), json=payload, timeout=180
    )
    r.raise_for_status()
    raw = r.json().get("message", {}).get("content", "{}")

    try:
        ocena = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Model zwrócił niepoprawny JSON — oddaję surowo.")
        ocena = {"oceny": [], "zastrzezenie": "Błąd parsowania odpowiedzi.", "_raw": raw}

    return {
        "ocena": ocena,
        "sources": [
            {"citation": d.get("citation"), "ulga": d.get("ulga"), "score": d.get("_score")}
            for d in docs
        ],
    }
