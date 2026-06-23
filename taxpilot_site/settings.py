"""
Ustawienia Django dla TaxPilot.

Konfiguracja RAG (OpenSearch, embedder, Ollama, ELI) żyje w core `config.py`
i jest czytana przez moduły rdzenia (search, qualification, ingest_core).
Tutaj tylko warstwa Django: baza, Celery/Redis, aplikacje, szablony.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = [
    h.strip()
    for h in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if h.strip()
]
CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if o.strip()
]

# Za Cloudflare + nginx (TLS terminowany wyżej) Django musi rozpoznać HTTPS po
# nagłówku, inaczej request.is_secure() = False i CSRF na POST-ach (origin https)
# leci 403. nginx przekazuje oryginalny X-Forwarded-Proto z Cloudflare.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "ulgi",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "taxpilot_site.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "taxpilot_site.wsgi.application"
ASGI_APPLICATION = "taxpilot_site.asgi.application"

# ── PostgreSQL — system of record ─────────────────────────────────
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "taxpilot"),
        "USER": os.getenv("POSTGRES_USER", "taxpilot"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", "taxpilot"),
        "HOST": os.getenv("POSTGRES_HOST", "localhost"),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
    }
}

# ── Celery / Redis — kolejka ingestu ──────────────────────────────
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 60 * 60  # ingest może trwać
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"

# Harmonogram Beat — cotygodniowe odświeżanie korpusu (pon. 04:00 Europe/Warsaw).
# Domyślnie tylko akty ELI (najnowszy t.j. + nowele). Interpretacje KIS są
# wyłączone (sieć + embedding) — włącz kwargs {"with_interpretacje": True}, gdy
# chcesz też je dociągać. Alternatywa pod mały RAM: timer systemd + komenda
# `manage.py refresh_corpus` (patrz deploy/), bez always-on workera.
from celery.schedules import crontab  # noqa: E402

CELERY_BEAT_SCHEDULE = {
    "refresh-corpus-weekly": {
        "task": "ulgi.tasks.refresh_corpus_task",
        "schedule": crontab(day_of_week=1, hour=4, minute=0),
        "kwargs": {"with_interpretacje": False},
    },
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

LANGUAGE_CODE = "pl"
TIME_ZONE = "Europe/Warsaw"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
