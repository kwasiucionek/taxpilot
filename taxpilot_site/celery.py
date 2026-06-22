"""Konfiguracja Celery dla TaxPilot."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "taxpilot_site.settings")

app = Celery("taxpilot")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
