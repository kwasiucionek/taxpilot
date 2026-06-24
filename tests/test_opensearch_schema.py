"""Testy budowania filtrów i zapytań OpenSearch (czysta logika, bez klienta)."""

from __future__ import annotations

from opensearch_schema import build_filters, hits_to_docs, hybrid_body, knn_body


def test_build_filters_empty():
    assert build_filters() == []


def test_build_filters_simple_terms():
    f = build_filters(akt="CIT", ulga="BR", source_types=["ustawa", "interpretacja"])
    assert {"term": {"akt": "CIT"}} in f
    assert {"term": {"ulga": "BR"}} in f
    assert {"terms": {"source_type": ["ustawa", "interpretacja"]}} in f


def test_build_filters_on_date_legal_status_range():
    f = build_filters(on_date="2024-06-01")
    # obowiazuje_od <= dzień
    assert {"range": {"obowiazuje_od": {"lte": "2024-06-01"}}} in f
    # obowiazuje_do >= dzień LUB brak końca obowiązywania
    bool_clause = next(c for c in f if "bool" in c)
    should = bool_clause["bool"]["should"]
    assert {"range": {"obowiazuje_do": {"gte": "2024-06-01"}}} in should
    assert bool_clause["bool"]["minimum_should_match"] == 1


def test_knn_body_without_and_with_filters():
    vec = [0.1, 0.2, 0.3]
    body = knn_body(vec, k=5)
    assert body["size"] == 5
    assert body["query"]["knn"]["embedding"]["vector"] == vec
    assert "filter" not in body["query"]["knn"]["embedding"]

    filtered = knn_body(vec, k=5, filter_must=[{"term": {"ulga": "BR"}}])
    assert filtered["query"]["knn"]["embedding"]["filter"]["bool"]["must"]


def test_hybrid_body_disabled_falls_back_to_knn():
    body = hybrid_body("pytanie", [0.1], k=3, use_hybrid=False)
    assert "knn" in body["query"]
    assert "hybrid" not in body["query"]


def test_hybrid_body_has_two_subqueries():
    body = hybrid_body("pytanie", [0.1], k=3, use_hybrid=True)
    queries = body["query"]["hybrid"]["queries"]
    assert len(queries) == 2
    assert any("knn" in q for q in queries)
    assert any("multi_match" in q for q in queries)


def test_hybrid_body_embeds_filters_in_both_subqueries():
    filters = [{"term": {"akt": "PIT"}}]
    body = hybrid_body("pytanie", [0.1], k=3, filter_must=filters, use_hybrid=True)
    bm25_q, knn_q = body["query"]["hybrid"]["queries"]
    assert bm25_q["bool"]["filter"] == filters
    assert knn_q["knn"]["embedding"]["filter"]["bool"]["must"] == filters


def test_hits_to_docs_strips_embedding_and_keeps_score():
    hits = [
        {"_score": 1.5, "_source": {"citation": "art. 18d", "embedding": [0.1, 0.2]}},
        {"_score": 0.9, "_source": {"citation": "art. 24d"}},
    ]
    docs = hits_to_docs(hits)
    assert all("embedding" not in d for d in docs)
    assert docs[0]["_score"] == 1.5
    assert docs[0]["citation"] == "art. 18d"
    assert docs[1]["_score"] == 0.9
