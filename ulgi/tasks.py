"""Zadania Celery — ingest aktów oraz cykliczne odświeżanie korpusu."""

from celery import shared_task

from .ingest_core import ingest_act_to_stores, refresh_corpus


@shared_task(bind=True)
def ingest_act_task(self, kod: str, obowiazuje_od: str | None = None) -> dict:
    """Ingest pojedynczego aktu (on-demand, np. z panelu admina)."""
    return ingest_act_to_stores(kod, obowiazuje_od=obowiazuje_od, task_id=self.request.id or "")


@shared_task(bind=True)
def refresh_corpus_task(
    self,
    with_interpretacje: bool = False,
    interp_limit: int = 20,
    od_daty: str | None = None,
) -> dict:
    """Cykliczne odświeżanie korpusu (Celery Beat). Re-ingest aktów ELI
    (najnowszy tekst jednolity + nowele), opcjonalnie interpretacje KIS.
    """
    return refresh_corpus(
        with_interpretacje=with_interpretacje,
        interp_limit=interp_limit,
        od_daty=od_daty,
    )
