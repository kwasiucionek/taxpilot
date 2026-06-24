"""
Management command: ingest objaśnień podatkowych MF (PDF z gov.pl).

  python manage.py ingest_objasnienia --all
  python manage.py ingest_objasnienia --kod OBJ-IPBOX-2019
"""

from django.core.management.base import BaseCommand, CommandError

from config import OBJASNIENIA
from ulgi.ingest_docs import ingest_objasnienie_to_stores


class Command(BaseCommand):
    help = "Ingest objaśnień podatkowych MF do PostgreSQL + OpenSearch."

    def add_arguments(self, parser):
        parser.add_argument("--kod", help=f"jeden dokument: {', '.join(OBJASNIENIA) or '(brak)'}")
        parser.add_argument("--all", action="store_true", help="wszystkie z config.OBJASNIENIA")

    def handle(self, *args, **opts):
        kody = list(OBJASNIENIA) if opts["all"] else ([opts["kod"]] if opts["kod"] else [])
        if not kody:
            raise CommandError("Podaj --kod <KOD> albo --all")

        for kod in kody:
            if kod not in OBJASNIENIA:
                raise CommandError(f"Nieznany dokument: {kod}")
            self.stdout.write(f"Ingest {kod}...")
            out = ingest_objasnienie_to_stores(kod)
            self.stdout.write(self.style.SUCCESS(f"{kod}: ok={out['ok']}, błędy={out['errors']}"))
