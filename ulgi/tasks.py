"""Zadania Celery — ingest aktów, interpretacji oraz orkiestracja odświeżania."""

from celery import shared_task

from .ingest_core import ingest_act_to_stores, ingest_interpretacje_for_ulga


@shared_task(bind=True)
def ingest_act_task(self, kod: str, obowiazuje_od: str | None = None, force: bool = False) -> dict:
    """Ingest jednego aktu (jednostka pracy — własny limit czasu).

    Inkrementalny: embeduje tylko nowe/zmienione chunki (hash treści).
    `force=True` wymusza pełne przeliczenie (np. po zmianie modelu embeddera).
    """
    return ingest_act_to_stores(
        kod, obowiazuje_od=obowiazuje_od, task_id=self.request.id or "", force=force
    )


@shared_task(bind=True)
def ingest_interpretacje_task(self, ulga: str, limit: int = 20, od_daty: str | None = None) -> dict:
    """Ingest najnowszych interpretacji KIS dla jednej ulgi (jednostka pracy)."""
    return ingest_interpretacje_for_ulga(ulga, limit=limit, od_daty=od_daty)


@shared_task(bind=True)
def refresh_corpus_task(
    self,
    acts: list[str] | None = None,
    with_interpretacje: bool = False,
    interp_limit: int = 20,
    od_daty: str | None = None,
    force: bool = False,
) -> dict:
    """Orkiestrator odświeżania korpusu (Celery Beat).

    Nie liczy nic sam — rozsyła OSOBNE zadanie per akt (i per ulga), więc każda
    jednostka pracy ma własny limit czasu, a timeout/błąd jednej nie przewraca
    pozostałych. Dzięki inkrementalnemu ingestowi akt bez zmian kończy się w
    sekundy (0 embeddingów). Sam dispatcher kończy się natychmiast.
    """
    from config import ACTS, PRZEPISY_BY_ULGA

    kody = acts or list(ACTS)
    dispatched: dict = {"acts": {}, "interpretacje": {}}

    for kod in kody:
        res = ingest_act_task.delay(kod, obowiazuje_od=od_daty, force=force)
        dispatched["acts"][kod] = res.id

    if with_interpretacje:
        for ulga in PRZEPISY_BY_ULGA:
            res = ingest_interpretacje_task.delay(ulga, limit=interp_limit, od_daty=od_daty)
            dispatched["interpretacje"][ulga] = res.id

    return dispatched
