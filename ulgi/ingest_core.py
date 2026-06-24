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

import hashlib
from datetime import date


def ingest_act_to_stores(
    kod: str, obowiazuje_od: str | None = None, task_id: str = "", force: bool = False
) -> dict:
    from django.utils import timezone
    from opensearchpy.helpers import bulk

    from chunking import chunk_act
    from config import ACTS, OPENSEARCH_INDEX
    from eli_client import fetch_act
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
    year = int(meta["year"])
    position = int(meta["pos"])
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

        # Hash treści każdego chunku (dokładnie to, co idzie do embeddera) →
        # inkrementalny ingest: liczymy wektory tylko dla nowych/zmienionych.
        new_by_id: dict[str, tuple] = {}
        for c in chunks:
            cid = c.doc_id()
            h = hashlib.sha256(c.content_text.encode("utf-8")).hexdigest()
            new_by_id[cid] = (c, h)

        existing = dict(Chunk.objects.filter(akt=akt).values_list("opensearch_id", "content_hash"))

        # nowe lub zmienione (albo wszystko, gdy force) — tylko te embedujemy
        to_write = [
            (c, h, cid) for cid, (c, h) in new_by_id.items() if force or existing.get(cid) != h
        ]
        # chunki, których już nie ma w nowym tekście — do usunięcia z obu baz
        removed_ids = [cid for cid in existing if cid not in new_by_id]

        vecs = embed_documents([c.content_text for c, _h, _cid in to_write]) if to_write else []

        # OpenSearch: upsert zmienionych, usuń znikłe (unchanged zostają nietknięte)
        actions: list[dict] = []
        for (c, _h, cid), vec in zip(to_write, vecs, strict=False):
            src = c.to_source()
            if eff:
                src["obowiazuje_od"] = eff
            actions.append(
                {"_index": OPENSEARCH_INDEX, "_id": cid, "_source": {**src, "embedding": vec}}
            )
        for cid in removed_ids:
            actions.append({"_op_type": "delete", "_index": OPENSEARCH_INDEX, "_id": cid})

        ok, errors = bulk(client, actions, raise_on_error=False) if actions else (0, [])
        client.indices.refresh(index=OPENSEARCH_INDEX)

        # Postgres: usuń znikłe, podmień zmienione (unchanged zostawiamy z ich
        # dotychczasowym obowiazuje_od — semantyka „obowiązuje od ostatniej zmiany").
        if removed_ids:
            Chunk.objects.filter(akt=akt, opensearch_id__in=removed_ids).delete()
        if to_write:
            changed_ids = [cid for _c, _h, cid in to_write]
            Chunk.objects.filter(akt=akt, opensearch_id__in=changed_ids).delete()
            rows = [
                Chunk(
                    akt=akt,
                    opensearch_id=cid,
                    article_num=c.article_num,
                    ustep=c.ustep,
                    citation=c.citation,
                    ulga=c.ulga,
                    source_type=c.source_type,
                    content_text=c.content_text,
                    content_hash=h,
                    eli_id=eid,
                    obowiazuje_od=od_date,
                )
                for c, h, cid in to_write
            ]
            Chunk.objects.bulk_create(rows, batch_size=500)

        _nowele = meta.get("_nowele_po_tj", [])
        akt.last_ingested_at = timezone.now()
        akt.nowele_po_tj = len(_nowele)
        akt.nowele = _nowele
        akt.save()

        job.status = "success"
        job.chunks_indexed = len(to_write)
        job.finished_at = timezone.now()
        job.save()
        return {
            "akt": kod,
            "eli": eid,
            "stan_prawny": eff,
            "nowele_po_tj": len(_nowele),
            "ok": ok,
            "errors": len(errors),
            "embedded": len(to_write),
            "skipped": len(chunks) - len(to_write),
            "removed": len(removed_ids),
        }

    except Exception as e:  # noqa: BLE001
        job.status = "failed"
        job.error = str(e)[:2000]
        job.finished_at = timezone.now()
        job.save()
        raise


def refresh_corpus(
    *,
    acts: list[str] | None = None,
    with_interpretacje: bool = False,
    interp_limit: int = 20,
    od_daty: str | None = None,
    force: bool = False,
    log=print,
) -> dict:
    """Odświeża korpus.

    Re-ingest aktów ELI (resolver bierze najnowszy tekst jednolity i przelicza
    nowelizacje po t.j.), opcjonalnie dociąga najnowsze interpretacje KIS per
    ulga. Odporne na błąd pojedynczej pozycji — leci dalej i raportuje w
    podsumowaniu. Każdy akt zakłada własny `IngestJob` (przez ingest_act_to_stores).
    """
    from config import ACTS

    kody = acts or list(ACTS)
    summary: dict = {"acts": {}, "interpretacje": {}}

    for kod in kody:
        log(f"[refresh] akt {kod}...")
        try:
            out = ingest_act_to_stores(kod, obowiazuje_od=od_daty, force=force)
            summary["acts"][kod] = {
                "ok": out["ok"],
                "errors": out["errors"],
                "embedded": out["embedded"],
                "skipped": out["skipped"],
                "removed": out["removed"],
                "nowele_po_tj": out["nowele_po_tj"],
                "stan_prawny": out["stan_prawny"],
            }
        except Exception as e:  # noqa: BLE001
            summary["acts"][kod] = {"error": str(e)[:300]}
            log(f"[refresh] akt {kod} BŁĄD: {e}")

    if with_interpretacje:
        from config import PRZEPISY_BY_ULGA

        for ulga in PRZEPISY_BY_ULGA:
            log(f"[refresh] interpretacje {ulga}...")
            try:
                r = ingest_interpretacje_for_ulga(
                    ulga, limit=interp_limit, od_daty=od_daty, log=log
                )
                summary["interpretacje"][ulga] = {
                    "znaleziono": r["znaleziono"],
                    "zaindeksowano": r["zaindeksowano"],
                }
            except Exception as e:  # noqa: BLE001
                summary["interpretacje"][ulga] = {"error": str(e)[:300]}
                log(f"[refresh] interpretacje {ulga} BŁĄD: {e}")

    return summary


def ingest_interpretacje_for_ulga(
    ulga: str, *, limit: int = 20, od_daty: str | None = None, log=print
) -> dict:
    """Wyszukuje i indeksuje najnowsze interpretacje KIS dla JEDNEJ ulgi.

    Jednostka pracy dla zadania Celery `ingest_interpretacje_task`. Zwraca
    {'ulga', 'znaleziono', 'zaindeksowano'}. Rzuca, gdy padnie samo wyszukiwanie
    (błąd pojedynczej interpretacji jest łapany i pomijany).
    """
    from config import PRZEPISY_BY_ULGA

    from .ingest_docs import ingest_interpretacja_to_stores
    from .kis_client import search_interpretacje

    przepisy = PRZEPISY_BY_ULGA[ulga]
    hits = search_interpretacje(przepisy, od_daty=od_daty, limit=limit)
    done = 0
    for h in hits:
        try:
            ingest_interpretacja_to_stores(h["id"], ulga=ulga)
            done += 1
        except Exception as e:  # noqa: BLE001
            log(f"[interpretacje {ulga}] {h['id']} BŁĄD: {e}")
    return {"ulga": ulga, "znaleziono": len(hits), "zaindeksowano": done}
