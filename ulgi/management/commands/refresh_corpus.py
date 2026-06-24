"""
Management command: cykliczne odświeżanie korpusu.

Re-ingest aktów ELI (resolver bierze najnowszy tekst jednolity i przelicza
nowelizacje po t.j.), opcjonalnie dociąga najnowsze interpretacje KIS.

Ta sama logika co zadanie Celery `refresh_corpus_task`, tylko jako krótko
żyjący proces — idealne pod timer systemd (bez always-on workera trzymającego
drugą kopię embeddera w RAM).

  python manage.py refresh_corpus
  python manage.py refresh_corpus --act CIT --act PIT
  python manage.py refresh_corpus --interpretacje --interp-limit 20 --od-daty 2024-01-01
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Odświeża korpus: re-ingest aktów ELI (+ opcjonalnie interpretacje KIS)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--act",
            action="append",
            help="ogranicz do wskazanych aktów (można podać wielokrotnie); domyślnie wszystkie",
        )
        parser.add_argument(
            "--interpretacje",
            action="store_true",
            help="dociągnij też najnowsze interpretacje KIS per ulga",
        )
        parser.add_argument(
            "--interp-limit", type=int, default=20, help="max interpretacji na ulgę"
        )
        parser.add_argument("--od-daty", dest="od_daty", help="stan prawny / data od (YYYY-MM-DD)")
        parser.add_argument(
            "--force",
            action="store_true",
            help="wymuś pełne przeliczenie embeddingów (pomija inkrementalny skip po hashu)",
        )

    def handle(self, *args, **opts):
        from ulgi.ingest_core import refresh_corpus

        self.stdout.write("Odświeżanie korpusu...")
        summary = refresh_corpus(
            acts=opts["act"],
            with_interpretacje=opts["interpretacje"],
            interp_limit=opts["interp_limit"],
            od_daty=opts["od_daty"],
            force=opts["force"],
            log=self.stdout.write,
        )

        self.stdout.write("")
        for kod, r in summary["acts"].items():
            if "error" in r:
                self.stderr.write(self.style.ERROR(f"  akt {kod}: BŁĄD — {r['error']}"))
            else:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  akt {kod}: policzono={r['embedded']}, pominięto={r['skipped']}, "
                        f"usunięto={r['removed']}, błędy={r['errors']}, "
                        f"nowele po t.j.={r['nowele_po_tj']}, stan={r['stan_prawny']}"
                    )
                )
        for ulga, r in summary["interpretacje"].items():
            if "error" in r:
                self.stderr.write(self.style.ERROR(f"  interpretacje {ulga}: BŁĄD — {r['error']}"))
            else:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  interpretacje {ulga}: {r['zaindeksowano']}/{r['znaleziono']}"
                    )
                )
        self.stdout.write(self.style.SUCCESS("Gotowe."))
