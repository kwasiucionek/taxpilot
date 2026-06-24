"""
Management command: ingest aktów bez Celery (oszczędność RAM na małym VPS).

  python manage.py ingest_acts --all --od 2024-01-01
  python manage.py ingest_acts --act CIT --od 2024-01-01
  python manage.py ingest_acts --act CIT --async      # przez Celery
"""

from django.core.management.base import BaseCommand, CommandError

from config import ACTS
from ulgi.ingest_core import ingest_act_to_stores


class Command(BaseCommand):
    help = "Ingest aktów podatkowych do PostgreSQL + OpenSearch."

    def add_arguments(self, parser):
        parser.add_argument("--act", help=f"jeden akt: {', '.join(ACTS)}")
        parser.add_argument("--all", action="store_true", help="wszystkie akty z config.ACTS")
        parser.add_argument("--od", dest="od", help="obowiazuje_od (YYYY-MM-DD)")
        parser.add_argument(
            "--async",
            action="store_true",
            dest="run_async",
            help="uruchom przez Celery zamiast synchronicznie",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="wymuś pełne przeliczenie embeddingów (pomija inkrementalny skip po hashu)",
        )

    def handle(self, *args, **opts):
        kody = list(ACTS) if opts["all"] else ([opts["act"]] if opts["act"] else [])
        if not kody:
            raise CommandError("Podaj --act <KOD> albo --all")

        for kod in kody:
            if kod not in ACTS:
                raise CommandError(f"Nieznany akt: {kod}")
            if opts["run_async"]:
                from ulgi.tasks import ingest_act_task

                res = ingest_act_task.delay(kod, opts["od"], force=opts["force"])
                self.stdout.write(self.style.SUCCESS(f"{kod}: zlecono Celery (task {res.id})"))
            else:
                self.stdout.write(f"Ingest {kod}...")
                out = ingest_act_to_stores(kod, obowiazuje_od=opts["od"], force=opts["force"])
                self.stdout.write(
                    self.style.SUCCESS(
                        f"{kod}: policzono={out['embedded']}, pominięto={out['skipped']}, "
                        f"usunięto={out['removed']}, błędy={out['errors']}"
                    )
                )
