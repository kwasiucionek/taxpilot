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
)
from search import retrieve_mixed

logger = logging.getLogger(__name__)

# Ulgi rozpatrywane przy ocenie działalności (PKUP zostawiamy poza zakresem
# asystenta — dotyczy formy wynagrodzenia, nie charakteru prac).
_DEFAULT_ULGI = ["BR", "IPBOX", "PKUP"]

_SYSTEM = (
    "Jesteś asystentem prawno-podatkowym wspierającym doradcę w ocenie, czy "
    "opisana sytuacja kwalifikuje się do ulgi B+R, IP Box lub 50% kosztów "
    "autorskich (50% KUP). Oceniaj WYŁĄCZNIE na podstawie dostarczonego "
    "kontekstu (przepisy, objaśnienia, interpretacje). Dla każdej ulgi wskaż "
    "podstawę prawną z kontekstu (np. art. 18d ustawy o CIT, art. 22 ust. 9 "
    "pkt 3 ustawy o PIT, objaśnienia MF, sygnatura interpretacji). Przesłanki, "
    "na które zwracaj uwagę: dla B+R — systematyczność, twórczy charakter, "
    "zwiększanie zasobów wiedzy; dla IP Box — kwalifikowane prawo własności "
    "intelektualnej i wskaźnik nexus; dla 50% KUP — powstanie utworu w "
    "rozumieniu prawa autorskiego, przeniesienie praw, kwotowe wyodrębnienie "
    "honorarium autorskiego (nie procent czasu pracy), ewidencja utworów oraz "
    "mieszczenie się w katalogu z art. 22 ust. 9b ustawy o PIT. Opis może "
    "zawierać sekcję „Ankieta (deklaracje)” z odpowiedziami tak / nie / "
    "nie wiem na pytania o przesłanki — deklaracje przyjmuj jako opis stanu "
    "faktycznego, a „nie wiem” i brak odpowiedzi traktuj jak brak danych i "
    "wymień w polu czego_brakuje. Jeśli z opisu "
    "nie wynika spełnienie przesłanki — napisz, czego brakuje, zamiast zgadywać. "
    "To wsparcie informacyjne, nie wiążąca porada podatkowa."
)

# Model proszony o zwięzły JSON — łatwy do wyrenderowania w UI.
_FORMAT = (
    "Zwróć WYŁĄCZNIE poprawny JSON (bez ```), w schemacie:\n"
    "{\n"
    '  "oceny": [\n'
    "    {\n"
    '      "ulga": "B+R" | "IP Box" | "50% KUP",\n'
    '      "werdykt": "kwalifikuje" | "częściowo" | "nie kwalifikuje" | "za mało danych",\n'
    '      "uzasadnienie": "2-4 zdania",\n'
    '      "podstawa_prawna": ["art. 18d ust. 1 ustawy o CIT", "art. 22 ust. 9 pkt 3 ustawy o PIT", "..."],\n'
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
    model: str | None = None,
    on_date: str | None = None,
) -> dict:
    """Ocena kwalifikacji. Zwraca {'ocena': {...}, 'sources': [...]}"""
    ulgi = ulgi or _DEFAULT_ULGI

    # Retrieval per ulga z kwotą per typ źródła (ustawa + objaśnienia + interpretacje);
    # opis działalności jako zapytanie. Bez kwoty interpretacje (~75% indeksu)
    # zdominowałyby wyniki i ocena stałaby tylko na interpretacjach, nie na ustawie.
    docs: list[dict] = []
    seen: set[str] = set()
    for code in ulgi:
        for d in retrieve_mixed(opis, ulga=code, on_date=on_date):
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
    r = requests.post(f"{OLLAMA_URL}/api/chat", headers=_headers(), json=payload, timeout=180)
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
            {
                "citation": d.get("citation"),
                "ulga": d.get("ulga"),
                "eli_id": d.get("eli_id", ""),
                "zrodlo_url": d.get("zrodlo_url", ""),
                "content_text": d.get("content_text", ""),
                "score": d.get("_score"),
            }
            for d in docs
        ],
    }
