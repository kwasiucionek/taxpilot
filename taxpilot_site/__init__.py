# Załaduj Celery razem z Django, żeby @shared_task działało.
from .celery import app as celery_app

__all__ = ("celery_app",)
