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
        "anchors": {"CIT": ["18d", "18da", "18e", "18ea", "18eb"],
                    "PIT": ["26e", "26f", "26g", "26ga", "26gb"]},
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

# ── Wyszukiwanie ─────────────────────────────────────────────────
TOP_K = 8
CHUNK_MAX_CHARS = 1200  # powyżej tej długości artykuł dzielony po ustępach
