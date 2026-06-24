"""Konfiguracja środowiska testowego (ładowana przez pytest przed Django setup).

Ustawiamy bezpieczne wartości ENV dla testów, zanim zaimportuje się
`taxpilot_site.settings` — inaczej produkcyjny strażnik (DEBUG=0 + domyślny
SECRET_KEY) przerwałby konfigurację Django.
"""

from __future__ import annotations

import os

os.environ.setdefault("DJANGO_DEBUG", "1")
os.environ.setdefault("DJANGO_SECRET_KEY", "test-insecure-secret-not-for-prod")
