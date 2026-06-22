"""
ingest_core — orkiestracja ingestu zintegrowana z Django.

Różni się od core `ingest.py` tym, że oprócz indeksowania w OpenSearch
zapisuje chunki do PostgreSQL (system of record) i prowadzi `IngestJob`.
Wołane zarówno z zadania Celery, jak i z management command (oszczędność RAM
na małym VPS — bez always-on workera).

Tekst jednolity ustalany jest dynamicznie z referencji ELI (eli_client.fetch_act),
a `obowiazuje_od` domyślnie pochodzi z `legalStatusDate` metadanych t.j.
(można nadpisać argumentem).
"""

from __future__ import annotations

from datetime import date


def ingest_act_to_stores(kod: str, obowiazuje_od: str | None = None, task_id: str = "") -> dict:
    from django.utils import timezone
    from opensearchpy.helpers import bulk

    from config import ACTS, OPENSEARCH_INDEX
    from eli_client import fetch_act
    from chunking import chunk_act
    from embedder import embed_documents
    from opensearch_schema import ensure_hybrid_pipeline, ensure_index, get_client

    from .models import Akt, Chunk, IngestJob

    spec = ACTS[kod]

    client = get_client()
    ensure_index(client)
    ensure_hybrid_pipeline(client)

    # 1. Rozwiąż najnowszy t.j. z referencji ELI i pobierz tekst + metadane.
    #    (Faza „znajdź dokument" — przed rejestracją aktu, bo year/pos pochodzą z t.j.)
    eid, text, meta = fetch_act(spec)
    year = int(meta.get("year"))
    position = int(meta.get("pos"))
    publisher = meta.get("publisher", "DU")

    # Data stanu prawnego: argument > legalStatusDate z metadanych t.j.
    eff = obowiazuje_od or meta.get("legalStatusDate")
    od_date = date.fromisoformat(eff) if eff else None

    # 2. Zarejestruj akt (współrzędne realnie pobranego t.j.) + przebieg ingestu.
    akt, _ = Akt.objects.update_or_create(
        kod=kod,
        defaults=dict(
            title=spec["title"],
            publisher=publisher,
            year=year,
            position=position,
            citation_suffix=spec["citation_suffix"],
            eli_id=eid,
        ),
    )
    job = IngestJob.objects.create(
        akt=akt, status="running", obowiazuje_od=od_date, celery_task_id=task_id
    )

    try:
        chunks = chunk_act(
            text,
            akt_short=spec["short"],
            eli_id=eid,
            citation_suffix=spec["citation_suffix"],
        )
        if not chunks:
            raise ValueError("Brak chunków — sprawdź parsowanie tekstu aktu.")

        vecs = embed_documents([c.content_text for c in chunks])

        # Reindex „na czysto" — usuń stare chunki tego aktu z Postgresa.
        Chunk.objects.filter(akt=akt).delete()

        rows: list[Chunk] = []
        actions: list[dict] = []
        for c, vec in zip(chunks, vecs):
            src = c.to_source()
            if eff:
                src["obowiazuje_od"] = eff
            doc_id = c.doc_id()

            rows.append(
                Chunk(
                    akt=akt,
                    opensearch_id=doc_id,
                    article_num=c.article_num,
                    ustep=c.ustep,
                    citation=c.citation,
                    ulga=c.ulga,
                    source_type=c.source_type,
                    content_text=c.content_text,
                    eli_id=eid,
                    obowiazuje_od=od_date,
                )
            )
            actions.append(
                {"_index": OPENSEARCH_INDEX, "_id": doc_id, "_source": {**src, "embedding": vec}}
            )

        Chunk.objects.bulk_create(rows, batch_size=500)
        ok, errors = bulk(client, actions, raise_on_error=False)
        client.indices.refresh(index=OPENSEARCH_INDEX)

        _nowele = meta.get("_nowele_po_tj", [])
        akt.last_ingested_at = timezone.now()
        akt.nowele_po_tj = len(_nowele)
        akt.nowele = _nowele
        akt.save()

        job.status = "success"
        job.chunks_indexed = ok
        job.finished_at = timezone.now()
        job.save()
        return {
            "akt": kod,
            "eli": eid,
            "stan_prawny": eff,
            "nowele_po_tj": len(meta.get("_nowele_po_tj", [])),
            "ok": ok,
            "errors": len(errors),
        }

    except Exception as e:  # noqa: BLE001
        job.status = "failed"
        job.error = str(e)[:2000]
        job.finished_at = timezone.now()
        job.save()
        raise
