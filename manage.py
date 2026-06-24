#!/usr/bin/env python3
"""Django management entry point dla TaxPilot."""

import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "taxpilot_site.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError("Nie znaleziono Django. Aktywuj venv i zainstaluj zależności.") from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
