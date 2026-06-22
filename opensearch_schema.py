"""
opensearch_schema.py — schemat indeksu i budowanie zapytań.

Wzorowane na uodo_rag/opensearch_client.py:
  - analizator polski (morfologik/Stempel) + multi-field content_text.pl
  - knn_vector (lucene HNSW, cosine)
  - hybryda BM25 + kNN przez pipeline normalization-processor
Dodane dla aktów prawnych:
  - metadane redakcyjne (akt, article_num, ustep, citation, ulga)
  - typ źródła (ustawa / interpretacja / objaśnienia / orzeczenie)
  - zakres obowiązywania (obowiazuje_od / obowiazuje_do) → „stan prawny na dzień"
"""

from __future__ import annotations

import logging
from typing import Any

from opensearchpy import OpenSearch

from config import (
    EMBED_DIM,
    OPENSEARCH_INDEX,
    OPENSEARCH_URL,
    POLISH_STEM_FILTER,
)

logger = logging.getLogger(__name__)

HYBRID_PIPELINE_ID = "taxpilot-hybrid-pipeline"
_BM25_WEIGHT = 0.6
_KNN_WEIGHT = 0.4

_PIPELINE_BODY = {
    "description": "Hybrid BM25 + kNN (min_max normalization) for taxpilot",
    "phase_results_processors": [
        {
            "normalization-processor": {
                "normalization": {"technique": "min_max"},
                "combination": {
                    "technique": "arithmetic_mean",
                    "parameters": {"weights": [_BM25_WEIGHT, _KNN_WEIGHT]},
                },
            }
        }
    ],
}

# Pole bazowe (standard) zachowuje exact-match (sygnatury, oznaczenia
# artykułów w treści), podpole .pl (morfologik) toleruje fleksję.
_BM25_FIELDS = ["content_text^1.0", "content_text.pl^2.0"]


def get_client() -> OpenSearch:
    return OpenSearch(
        hosts=[OPENSEARCH_URL], timeout=60, max_retries=3, retry_on_timeout=True
    )


# ─────────────────────────── SCHEMAT ─────────────────────────────


def get_index_body(embed_dim: int = EMBED_DIM) -> dict:
    return {
        "settings": {
            "index.knn": True,
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "analysis": {
                "analyzer": {
                    "polish_custom": {
                        "type": "custom",
                        "tokenizer": "standard",
                        # POLISH_STEM_FILTER: "morfologik_stem" (lematyzacja
                        # słownikowa, plugin analysis-morfologik) lub
                        # "polish_stem" (Stempel, plugin analysis-stempel).
                        "filter": ["lowercase", POLISH_STEM_FILTER],
                    }
                }
            },
        },
        "mappings": {
            "properties": {
                "content_text": {
                    "type": "text",
                    "analyzer": "standard",
                    "fields": {
                        "pl": {
                            "type": "text",
                            "analyzer": "polish_custom",
                            "search_analyzer": "polish_custom",
                        }
                    },
                },
                "embedding": {
                    "type": "knn_vector",
                    "dimension": embed_dim,
                    "method": {
                        "name": "hnsw",
                        "engine": "lucene",
                        "space_type": "cosinesimil",
                        "parameters": {"ef_construction": 128, "m": 16},
                    },
                },
                # Metadane redakcyjne / filtry
                "citation": {"type": "keyword"},
                "akt": {"type": "keyword"},
                "article_num": {"type": "keyword"},
                "ustep": {"type": "keyword"},
                "ulga": {"type": "keyword"},
                "source_type": {"type": "keyword"},
                "eli_id": {"type": "keyword"},
                "sygnatura": {"type": "keyword"},  # interpretacje / orzeczenia
                "chunk_index": {"type": "integer"},
                "chunk_total": {"type": "integer"},
                # Stan prawny na dzień
                "obowiazuje_od": {"type": "date", "format": "yyyy-MM-dd"},
                "obowiazuje_do": {"type": "date", "format": "yyyy-MM-dd"},
            }
        },
    }


def ensure_index(client: OpenSearch, embed_dim: int = EMBED_DIM) -> None:
    if not client.indices.exists(index=OPENSEARCH_INDEX):
        client.indices.create(index=OPENSEARCH_INDEX, body=get_index_body(embed_dim))
        logger.info("Indeks '%s' utworzony (dim=%d).", OPENSEARCH_INDEX, embed_dim)
    else:
        logger.info("Indeks '%s' już istnieje.", OPENSEARCH_INDEX)


def ensure_hybrid_pipeline(client: OpenSearch) -> bool:
    """Tworzy/aktualizuje pipeline hybrydy. PUT jest idempotentny."""
    try:
        client.transport.perform_request(
            "PUT", f"/_search/pipeline/{HYBRID_PIPELINE_ID}", body=_PIPELINE_BODY
        )
        logger.info(
            "Pipeline '%s' gotowy (BM25=%.2f, kNN=%.2f).",
            HYBRID_PIPELINE_ID,
            _BM25_WEIGHT,
            _KNN_WEIGHT,
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("Pipeline hybrydy niedostępny (%s) — fallback do kNN.", e)
        return False


# ─────────────────────────── FILTRY ──────────────────────────────


def build_filters(
    *,
    akt: str | None = None,
    ulga: str | None = None,
    source_types: list[str] | None = None,
    on_date: str | None = None,  # "YYYY-MM-DD" → stan prawny na ten dzień
) -> list[dict]:
    must: list[dict] = []
    if akt:
        must.append({"term": {"akt": akt}})
    if ulga:
        must.append({"term": {"ulga": ulga}})
    if source_types:
        must.append({"terms": {"source_type": source_types}})
    if on_date:
        # przepis obowiązuje, gdy obowiazuje_od <= dzień oraz
        # (obowiazuje_do >= dzień lub brak końca obowiązywania).
        must.append({"range": {"obowiazuje_od": {"lte": on_date}}})
        must.append(
            {
                "bool": {
                    "should": [
                        {"range": {"obowiazuje_do": {"gte": on_date}}},
                        {"bool": {"must_not": {"exists": {"field": "obowiazuje_do"}}}},
                    ],
                    "minimum_should_match": 1,
                }
            }
        )
    return must


# ─────────────────────────── ZAPYTANIA ───────────────────────────


def _content_match(text: str) -> dict:
    return {
        "multi_match": {"query": text, "fields": _BM25_FIELDS, "type": "best_fields"}
    }


def knn_body(vector: list[float], k: int, filter_must: list[dict] | None = None) -> dict:
    knn_clause: dict = {"vector": vector, "k": k}
    if filter_must:
        knn_clause["filter"] = {"bool": {"must": filter_must}}
    return {"query": {"knn": {"embedding": knn_clause}}, "size": k}


def hybrid_body(
    text: str,
    vector: list[float],
    k: int,
    filter_must: list[dict] | None = None,
    use_hybrid: bool = True,
) -> dict:
    """Hybryda BM25 + kNN. Filtry osadzone wewnątrz każdej pod-query."""
    if not use_hybrid:
        return knn_body(vector, k, filter_must)

    if filter_must:
        bm25_q: dict = {
            "bool": {"must": [_content_match(text)], "filter": filter_must}
        }
        knn_q: dict = {
            "knn": {
                "embedding": {
                    "vector": vector,
                    "k": k,
                    "filter": {"bool": {"must": filter_must}},
                }
            }
        }
    else:
        bm25_q = _content_match(text)
        knn_q = {"knn": {"embedding": {"vector": vector, "k": k}}}

    return {"query": {"hybrid": {"queries": [bm25_q, knn_q]}}, "size": k}


def hits_to_docs(hits: list[dict]) -> list[dict[str, Any]]:
    docs = []
    for hit in hits:
        d: dict = hit["_source"].copy()
        d.pop("embedding", None)
        d["_score"] = hit.get("_score", 0.0)
        docs.append(d)
    return docs
