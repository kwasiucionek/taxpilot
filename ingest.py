"""
ingest.py — pipeline ingestu: ELI → chunking → embeddingi → OpenSearch.

Wariant framework-agnostyczny (bez Postgresa). Tekst jednolity ustalany jest
dynamicznie z referencji ELI (eli_client.fetch_act), a `obowiazuje_od` domyślnie
pochodzi z `legalStatusDate` metadanych t.j. (można nadpisać przez --od).

CLI:
  python ingest.py --setup                # tworzy indeks + pipeline hybrydy
  python ingest.py --act CIT              # data stanu prawnego z metadanych t.j.
  python ingest.py --all  --od 2026-01-01 # ręczne nadpisanie daty
"""

from __future__ import annotations

import argparse
import logging

from opensearchpy.helpers import bulk

from chunking import chunk_act
from config import ACTS, OPENSEARCH_INDEX
from eli_client import fetch_act
from embedder import embed_documents
from opensearch_schema import (
    ensure_hybrid_pipeline,
    ensure_index,
    get_client,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def setup() -> None:
    client = get_client()
    ensure_index(client)
    ensure_hybrid_pipeline(client)


def ingest_act(short: str, obowiazuje_od: str | None = None) -> tuple[int, int]:
    """Pobiera akt, chunkuje, embeduje i indeksuje. Zwraca (ok, błędy)."""
    act = ACTS[short]
    client = get_client()
    ensure_index(client)

    eid, text, meta = fetch_act(act)
    eff = obowiazuje_od or meta.get("legalStatusDate")

    chunks = chunk_act(
        text,
        akt_short=act["short"],
        eli_id=eid,
        citation_suffix=act["citation_suffix"],
    )
    logger.info("Akt %s (t.j. %s) → %d chunków, stan prawny %s.", short, eid, len(chunks), eff)
    if not chunks:
        logger.warning("Brak chunków dla %s — sprawdź parsowanie tekstu.", short)
        return 0, 0

    vecs = embed_documents([c.content_text for c in chunks])

    actions = []
    for c, vec in zip(chunks, vecs, strict=False):
        src = c.to_source()
        if eff:
            src["obowiazuje_od"] = eff
        # obowiazuje_do pozostawiamy puste = przepis nadal obowiązuje.
        actions.append(
            {
                "_index": OPENSEARCH_INDEX,
                "_id": c.doc_id(),
                "_source": {**src, "embedding": vec},
            }
        )

    ok, errors = bulk(client, actions, raise_on_error=False)
    client.indices.refresh(index=OPENSEARCH_INDEX)
    logger.info("Zaindeksowano %s: ok=%d, błędy=%d.", short, ok, len(errors))
    return ok, len(errors)


def ingest_all(obowiazuje_od: str | None = None) -> None:
    setup()
    for short in ACTS:
        ingest_act(short, obowiazuje_od=obowiazuje_od)


def main() -> None:
    p = argparse.ArgumentParser(description="Ingest aktów podatkowych do OpenSearch.")
    p.add_argument("--setup", action="store_true", help="utwórz indeks + pipeline")
    p.add_argument("--act", help=f"jeden akt: {', '.join(ACTS)}")
    p.add_argument("--all", action="store_true", help="wszystkie akty z config.ACTS")
    p.add_argument(
        "--od", dest="od", help="obowiazuje_od (YYYY-MM-DD); domyślnie z metadanych t.j."
    )
    args = p.parse_args()

    if args.setup:
        setup()
    if args.all:
        ingest_all(obowiazuje_od=args.od)
    elif args.act:
        ingest_act(args.act, obowiazuje_od=args.od)
    elif not args.setup:
        p.print_help()


if __name__ == "__main__":
    main()
