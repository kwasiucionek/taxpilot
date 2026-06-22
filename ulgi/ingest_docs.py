"""
ingest_docs — ingest dokumentów prozą:
  • objaśnienia podatkowe MF (PDF z gov.pl) — ingest_objasnienie_to_stores,
  • interpretacje indywidualne KIS (EUREKA, publiczne API) — ingest_interpretacja_to_stores.

Odróżnia się od ingest_core (akty ELI) tym, że źródłem nie jest akt z resolverem
tekstu jednolitego, lecz pojedynczy dokument (PDF objaśnień / JSON interpretacji).
Chunkowanie prozą (chunking.chunk_document), reszta toru (embedding, Postgres,
OpenSearch, IngestJob) wspólna — w _store_and_index.

Dokument rejestrujemy jako wpis Akt (pseudo-akt: eli_id pusty), żeby reużyć FK
Chunk.akt i całą maszynerię indeksu bez zmian schematu. Pieczęć „stan prawny"
pomija pseudo-akty (filtruje akty z eli_id).
"""

from __future__ import annotations

from datetime import date


def _store_and_index(akt, chunks, od_date, od_str: str) -> tuple[int, int]:
    """Wspólny tor: embedding → reindex Postgres → bulk OpenSearch. Zwraca (ok, errors)."""
    from opensearchpy.helpers import bulk

    from config import OPENSEARCH_INDEX
    from embedder import embed_documents
    from opensearch_schema import get_client

    from .models import Chunk

    vecs = embed_documents([c.content_text for c in chunks])

    # Reindex „na czysto" — usuń stare chunki tego dokumentu z Postgresa.
    Chunk.objects.filter(akt=akt).delete()

    rows: list = []
    actions: list[dict] = []
    for c, vec in zip(chunks, vecs):
        src = c.to_source()
        if od_str:
            src["obowiazuje_od"] = od_str
        doc_id = c.doc_id()
        rows.append(
            Chunk(
                akt=akt,
                opensearch_id=doc_id,
                article_num=c.article_num,
                ustep="",
                citation=c.citation,
                ulga=c.ulga,
                source_type=c.source_type,
                content_text=c.content_text,
                eli_id="",
                obowiazuje_od=od_date,
            )
        )
        actions.append(
            {"_index": OPENSEARCH_INDEX, "_id": doc_id, "_source": {**src, "embedding": vec}}
        )

    Chunk.objects.bulk_create(rows, batch_size=500)
    client = get_client()
    ok, errors = bulk(client, actions, raise_on_error=False)
    client.indices.refresh(index=OPENSEARCH_INDEX)
    return ok, len(errors)


def _register_pseudo_akt(kod: str, title: str, publisher: str, od_date):
    """Pseudo-akt (eli_id pusty) jako rodzic chunków dokumentu."""
    from .models import Akt

    akt, _ = Akt.objects.update_or_create(
        kod=kod,
        defaults=dict(
            title=title[:512],
            publisher=publisher,
            year=od_date.year if od_date else 0,
            position=0,
            citation_suffix="",
            eli_id="",
        ),
    )
    return akt


def ingest_objasnienie_to_stores(kod: str) -> dict:
    import requests
    from django.utils import timezone

    from config import OBJASNIENIA, SOURCE_OBJASNIENIA
    from eli_client import pdf_to_text
    from chunking import chunk_document
    from opensearch_schema import ensure_hybrid_pipeline, ensure_index, get_client

    from .models import IngestJob

    spec = OBJASNIENIA[kod]
    url = spec["url"]
    data_str = spec.get("data") or ""
    ulga = spec.get("ulga", "")
    od_date = date.fromisoformat(data_str) if data_str else None

    ensure_index(get_client())
    ensure_hybrid_pipeline(get_client())

    akt = _register_pseudo_akt(kod, spec["title"], "MF", od_date)
    job = IngestJob.objects.create(akt=akt, status="running", obowiazuje_od=od_date)
    try:
        r = requests.get(url, headers={"User-Agent": "TaxPilot/1.0"}, timeout=180)
        r.raise_for_status()
        text = pdf_to_text(r.content)
        if not text.strip():
            raise ValueError("Pusty tekst po ekstrakcji PDF (sprawdź URL / format).")

        chunks = chunk_document(
            text, kod=kod, citation=spec["citation"], ulga=ulga,
            source_type=SOURCE_OBJASNIENIA, zrodlo_url=url,
        )
        if not chunks:
            raise ValueError("Brak chunków po podziale dokumentu.")

        ok, errors = _store_and_index(akt, chunks, od_date, data_str)
        akt.last_ingested_at = timezone.now()
        akt.save()
        job.status = "success"; job.chunks_indexed = ok
        job.finished_at = timezone.now(); job.save()
        return {"objasnienie": kod, "url": url, "ok": ok, "errors": errors}
    except Exception as e:  # noqa: BLE001
        job.status = "failed"; job.error = str(e)[:2000]
        job.finished_at = timezone.now(); job.save()
        raise


def ingest_interpretacja_to_stores(info_id: str, ulga: str = "") -> dict:
    from django.utils import timezone

    from config import SOURCE_INTERPRETACJA
    from eli_client import html_to_text
    from chunking import chunk_document
    from opensearch_schema import ensure_hybrid_pipeline, ensure_index, get_client

    from .kis_client import fetch_interpretacja
    from .models import IngestJob

    meta = fetch_interpretacja(info_id)
    text = html_to_text(meta["tresc_html"])
    if not text.strip():
        raise ValueError(f"Pusty tekst po html_to_text dla informacji {info_id}.")

    syg = meta["sygnatura"] or f"informacja {meta['id']}"
    kod = f"KIS-{meta['id']}"  # ≤16 znaków (np. KIS-604348) — dyskryminator doc_id
    od_date = date.fromisoformat(meta["data_wyd"]) if meta["data_wyd"] else None
    url = f"https://eureka.mf.gov.pl/informacje/podglad/{meta['id']}"
    citation = f"Interpretacja indywidualna {syg}"

    ensure_index(get_client())
    ensure_hybrid_pipeline(get_client())

    akt = _register_pseudo_akt(kod, meta["teza"] or meta["nazwa"], "KIS", od_date)
    job = IngestJob.objects.create(akt=akt, status="running", obowiazuje_od=od_date)
    try:
        chunks = chunk_document(
            text, kod=kod, citation=citation, ulga=ulga,
            source_type=SOURCE_INTERPRETACJA, zrodlo_url=url,
        )
        if not chunks:
            raise ValueError("Brak chunków po podziale interpretacji.")

        ok, errors = _store_and_index(akt, chunks, od_date, meta["data_wyd"])
        akt.last_ingested_at = timezone.now()
        akt.save()
        job.status = "success"; job.chunks_indexed = ok
        job.finished_at = timezone.now(); job.save()
        return {"interpretacja": syg, "id": meta["id"], "ok": ok, "errors": errors}
    except Exception as e:  # noqa: BLE001
        job.status = "failed"; job.error = str(e)[:2000]
        job.finished_at = timezone.now(); job.save()
        raise
