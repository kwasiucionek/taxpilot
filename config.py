"""
Konfiguracja TaxPilot — RAG nad polskim prawem podatkowym ulg
(B+R, IP Box, koszty autorskie / 50% KUP).

Wzorce (klient OpenSearch, embedder stella-pl-mini, auth Ollama, hybryda)
przeniesione z uodo_rag i dostosowane do aktów prawnych.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    _env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(dotenv_path=_env_path if _env_path.exists() else None)
except ImportError:
    pass

# ── OpenSearch ────────────────────────────────────────────────────
OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "http://localhost:9200")
OPENSEARCH_INDEX = os.getenv("OPENSEARCH_INDEX", "taxpilot")

# Filtr stemmera polskiego w analizatorze `polish_custom`.
#   "morfologik_stem" — lematyzacja słownikowa (plugin analysis-morfologik).
#                       Lepsza dla prawa: sprowadza formy fleksyjne do lematu,
#                       więc "kosztów kwalifikowanych" ≈ "koszty kwalifikowane".
#   "polish_stem"     — Stempel (plugin analysis-stempel), algorytmiczny.
#                       Lżejszy fallback; tego używał uodo_rag.
# Plugin trzeba doinstalować w obrazie OpenSearch (patrz README).
POLISH_STEM_FILTER = os.getenv("POLISH_STEM_FILTER", "morfologik_stem")

# ── Model embeddingowy ────────────────────────────────────────────
# sdadas/stella-pl-retrieval-mini-8k — 435M, dim=1024, kontekst 8192,
# działa na CPU (z patchem xformers, patrz embedder.py). Dokumenty bez
# prefiksu, zapytania z prefiksem instrukcji (QUERY_PREFIX w embedder.py).
EMBED_MODEL = os.getenv("EMBED_MODEL", "sdadas/stella-pl-retrieval-mini-8k")
EMBED_DIM = 1024
# CPU bez xformers materializuje macierz atencji S×S — przy kontekście 8k to OOM.
# Tniemy długość sekwencji (S² spada ~256×) i batch. Na mocniejszej maszynie/GPU
# można podnieść przez zmienne środowiskowe.
EMBED_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "8"))
EMBED_MAX_SEQ = int(os.getenv("EMBED_MAX_SEQ", "512"))

# ── LLM (generacja RAG) ───────────────────────────────────────────
OLLAMA_CLOUD_API_KEY = os.getenv("OLLAMA_CLOUD_API_KEY", "")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
DEFAULT_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "deepseek-v4-flash:cloud")

# ── ELI API (api.sejm.gov.pl) ─────────────────────────────────────
ELI_API_BASE = os.getenv("ELI_API_BASE", "https://api.sejm.gov.pl/eli")

# Rejestr aktów do zaciągnięcia. Podajemy WYŁĄCZNIE adres ELI aktu PIERWOTNEGO
# (`original`) — niezmienny w czasie. Aktualny tekst jednolity ustala resolver
# (eli_client.resolve_consolidated) z referencji ELI „Inf. o tekście jednolitym",
# więc po publikacji nowego t.j. ingest sam pobierze najnowszą wersję. Datę stanu
# prawnego bierzemy z `legalStatusDate` metadanych t.j.
ACTS: dict[str, dict] = {
    "CIT": {
        "short": "CIT",
        "title": "Ustawa o podatku dochodowym od osób prawnych",
        "original": "DU/1992/86",  # ustawa z 15.02.1992 (Dz.U. 1992 nr 21 poz. 86)
        "citation_suffix": "ustawy o CIT",
    },
    "PIT": {
        "short": "PIT",
        "title": "Ustawa o podatku dochodowym od osób fizycznych",
        "original": "DU/1991/350",  # ustawa z 26.07.1991 (Dz.U. 1991 nr 80 poz. 350)
        "citation_suffix": "ustawy o PIT",
    },
    "ORD": {
        "short": "ORD",
        "title": "Ordynacja podatkowa",
        "original": "DU/1997/926",  # ustawa z 29.08.1997 (Dz.U. 1997 nr 137 poz. 926)
        "citation_suffix": "Ordynacji podatkowej",
    },
}

# ── Mapa ulg → kotwice artykułów ──────────────────────────────────
# Pozwala otagować chunk znacznikiem ulgi (gdy artykuł pasuje do kotwicy)
# — wykorzystywane do filtrowania i przez asystenta kwalifikacji.
ULGI: dict[str, dict] = {
    "BR": {
        "name": "Ulga B+R (badawczo-rozwojowa)",
        "anchors": {
            "CIT": ["18d", "18da", "18e", "18ea", "18eb"],
            "PIT": ["26e", "26f", "26g", "26ga", "26gb"],
        },
    },
    "IPBOX": {
        "name": "IP Box (preferencyjne 5% od kwalifikowanego IP)",
        "anchors": {"CIT": ["24d", "24e"], "PIT": ["30ca", "30cb"]},
    },
    "PKUP": {
        "name": "Koszty autorskie / 50% KUP",
        "anchors": {"PIT": ["22"]},  # art. 22 ust. 9 pkt 3 ustawy o PIT
    },
}

# ── Typy źródeł (source_type) ─────────────────────────────────────
SOURCE_USTAWA = "ustawa"
SOURCE_INTERPRETACJA = "interpretacja"
SOURCE_OBJASNIENIA = "objasnienia"
SOURCE_ORZECZENIE = "orzeczenie"

# ── Objaśnienia podatkowe MF (kuratorska lista PDF z gov.pl) ──────
# Proza (nie akty ELI) — chunkowane przez chunking.chunk_document, source_type
# = SOURCE_OBJASNIENIA. `kod` ≤16 znaków (trafia do article_num jako dyskryminator
# doc_id, niewyświetlany). Link do PDF wędruje do wyników jako `zrodlo_url`.
# Objaśnienia IP Box 2019 omawiają też szczegółowo kryteria B+R (twórczość,
# systematyczność), więc zasilają oba tematy.
OBJASNIENIA: dict[str, dict] = {
    "OBJ-IPBOX-2019": {
        "title": "Objaśnienia podatkowe MF z 15.07.2019 — IP Box",
        "data": "2019-07-15",
        "ulga": "IPBOX",
        "citation": "Objaśnienia MF z 15.07.2019 (IP Box)",
        # Stary link /media/5137/ na podatki.gov.pl wygasł (404). Bezpośredni
        # załącznik gov.pl z pełną treścią; gdyby kiedyś też padł, świeży URL
        # bierzemy ze strony: gov.pl/web/finanse → objaśnienia IP Box.
        "url": "https://www.gov.pl/attachment/8b23d192-0777-4e1a-8fb3-355f797a1200",
    },
    "OBJ-PKUP-2020": {
        "title": "Interpretacja ogólna MF z 15.09.2020 — 50% KUP (honorarium autorskie)",
        "data": "2020-09-15",
        "ulga": "PKUP",
        "citation": "Interpretacja ogólna MF z 15.09.2020 (50% KUP)",
        "url": "https://www.mf.gov.pl/documents/764034/6831363/Dz.+Urz.+Min.+Fin.+z+dnia+18+wrze%C5%9Bnia+2020+r.+-+poz.+107+-",
    },
    # B+R: MF nie wydało objaśnień podatkowych stricte dla ulgi B+R (kryteria
    # działalności B+R — twórczość, systematyczność, zwiększanie zasobów wiedzy —
    # omawiają szczegółowo objaśnienia IP Box powyżej). Istnieje natomiast
    # interpretacja ogólna z 13.02.2024 (DD8.8203.1.2021, Dz. Urz. MF poz. 16)
    # o kosztach pracowniczych B+R za czas usprawiedliwionej nieobecności —
    # pierwsza interpretacja ogólna dot. ulgi B+R. Aby ją dodać: na stronie
    # gov.pl/web/finanse znajdź wpis "DD8.8203.1.2021", skopiuj adres załącznika
    # PDF "20240221_Dz_Urz_Min_Fin_2024_poz_16.pdf" (postać
    # https://www.gov.pl/attachment/<uuid>), wklej niżej i odkomentuj wpis:
    #   python manage.py ingest_objasnienia --kod OBJ-BR-2024
    "OBJ-BR-2024": {
        "title": "Interpretacja ogólna MF z 13.02.2024 — ulga B+R (koszty pracownicze)",
        "data": "2024-02-13",
        "ulga": "BR",
        "citation": "Interpretacja ogólna MF z 13.02.2024 (B+R)",
        "url": "https://www.gov.pl/attachment/179a35a8-7a54-4c6c-85a2-9cf40ee069f6",
    },
}

# ── EUREKA / KIS — wyszukiwanie interpretacji indywidualnych ─────
# Kody filtrów serwerowych wyszukiwarki EUREKA (POST wyszukiwarka/informacje):
KIS_KATEGORIA_INTERPRETACJA_ID = 1  # KATEGORIA_INFORMACJI: „Interpretacja indywidualna"
KIS_STATUS_AKTUALNA_ID = 27  # STATUS_INFORMACJI: „Aktualna"

# Mapa: ulga → ID węzłów przepisów (słownik PRZEPISY, sid=19) dla naszych kotwic.
# Pozwala `ingest_interpretacje --ulga IPBOX` samodzielnie dobrać artykuły.
# PKUP celuje w art. 22 ust. 9 pkt 3 PIT (węzeł 35893) — wprost przepis o 50%
# kosztach z praw autorskich, a nie całe (szerokie) art. 22. Do poszerzenia o
# katalog działalności twórczych można dołożyć 35900 (art. 22 ust. 9b).
PRZEPISY_BY_ULGA: dict[str, list[int]] = {
    "BR": [35573, 40951],  # art. 18d CIT, art. 26e PIT
    "IPBOX": [36247, 36274, 41618, 41633],  # art. 24d/24e CIT, art. 30ca/30cb PIT
    "PKUP": [35893],  # art. 22 ust. 9 pkt 3 PIT (50% KUP)
}

# ── Wyszukiwanie ─────────────────────────────────────────────────
TOP_K = 8

# Kwota retrievalu per typ źródła dla retrieve_mixed (czat).
# Ustawa = podstawa, objaśnienia + interpretacje = wykładnia. Bez tego
# interpretacje (~75% indeksu) dominują globalne top-k i wypychają przepisy.
# Wartości łatwo stroić (np. 2–5 każde).
RETRIEVE_MIX = {
    SOURCE_USTAWA: 4,
    SOURCE_OBJASNIENIA: 3,
    SOURCE_INTERPRETACJA: 4,
}
CHUNK_MAX_CHARS = 1200  # powyżej tej długości artykuł dzielony po ustępach
