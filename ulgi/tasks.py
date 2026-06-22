"""Zadania Celery — ingest aktów w tle (wzorzec async jobs)."""

from celery import shared_task

from .ingest_core import ingest_act_to_stores


@shared_task(bind=True)
def ingest_act_task(self, kod: str, obowiazuje_od: str | None = None) -> dict:
    return ingest_act_to_stores(kod, obowiazuje_od=obowiazuje_od, task_id=self.request.id or "")
