"""
discovery.py — odkrywanie kandydatów na akty przez ELI /acts/search po słowach
kluczowych ISAP (np. "podatek dochodowy od osób prawnych") lub fragmencie tytułu
(np. "ceny transferowe").

ROLA W ARCHITEKTURZE: rdzeń korpusu (ustawy CIT/PIT/ORD) jest PINOWANY w
config.ACTS i pobierany jako najnowszy tekst jednolity (eli_client). Ten moduł
służy do ODKRYWANIA powiązanych aktów (głównie rozporządzeń wykonawczych) do
ręcznej, kuratorskiej decyzji — NIE do automatycznego ingestu.

Kategorie wyniku (nic nie jest ukrywane — wszystko z etykietą):
  • bazowy      — ustawa/rozporządzenie merytoryczne (główny kandydat do ingestu)
  • jednolity   — Obwieszczenie = TEKST JEDNOLITY powiązanego aktu (gotowa wersja
                  skonsolidowana; każdy t.j. jest obwieszczeniem!)
  • zmieniajacy — nowelizacja ("o zmianie" / "zmieniające") — diff już złożony
                  w tekście jednolitym, NIE do ingestu

Uwaga: dla aktów już pinowanych (CIT/PIT/ORD) ich własny t.j. też pojawi się tu
w kategorii „jednolity" — to oczekiwana redundancja z resolverem, można pominąć.

Wskazówka: słowo kluczowe musi pasować DOKŁADNIE do słownika ISAP (zob. endpoint
/keywords). Pod konkretne tematy pewniejsze bywa --title (fragment tytułu).

CLI:
  python discovery.py --keyword "podatek dochodowy od osób prawnych"
  python discovery.py --title "ceny transferowe" --type Rozporządzenie
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

import requests

from config import ELI_API_BASE

logger = logging.getLogger(__name__)
_JSON = {"User-Agent": "taxpilot/0.1", "Accept": "application/json"}

# Markery nowelizacji: ustawy „o zmianie ...", rozporządzenia „... zmieniające ...".
_AMEND_MARKS = ("o zmianie", "zmieniając")


def search_acts(
    *,
    keyword: str | None = None,
    title: str | None = None,
    act_type: str | None = None,
    publisher: str | None = "DU",
    in_force: bool = True,
    year: int | None = None,
    limit: int = 100,
    sort_by: str = "position",
    sort_dir: str = "desc",
) -> list[dict]:
    """ELI /acts/search → lista ActInfo (dict z ELI, title, type, status...)."""
    params: dict[str, Any] = {"limit": limit, "sortBy": sort_by, "sortDir": sort_dir}
    if keyword:
        params["keyword"] = keyword
    if title:
        params["title"] = title
    if act_type:
        params["type"] = act_type
    if publisher:
        params["publisher"] = publisher
    if in_force:
        params["inForce"] = "1"
    if year:
        params["year"] = year

    r = requests.get(f"{ELI_API_BASE}/acts/search", headers=_JSON, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("items", []) if isinstance(data, dict) else (data or [])


def _categorize(act: dict) -> str:
    """'jednolity' (Obwieszczenie = t.j.) | 'zmieniajacy' (nowelizacja) | 'bazowy'."""
    if act.get("type") == "Obwieszczenie":
        return "jednolity"
    title_l = act.get("title", "").lower()
    if any(m in title_l for m in _AMEND_MARKS):
        return "zmieniajacy"
    return "bazowy"


def discover(
    keyword: str | None = None,
    *,
    title: str | None = None,
    act_type: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Kandydaci po słowie kluczowym i/lub fragmencie tytułu, każdy z kategorią.
    Zwraca [{eli, type, status, category, title}, ...] — nic nie ukrywa."""
    out: list[dict] = []
    for a in search_acts(
        keyword=keyword, title=title, act_type=act_type, in_force=True, limit=limit
    ):
        out.append(
            {
                "eli": a.get("ELI"),
                "type": a.get("type", ""),
                "status": a.get("status", ""),
                "category": _categorize(a),
                "title": a.get("title", ""),
            }
        )
    return out


def _print_candidates(
    keyword: str | None, title: str | None, act_type: str | None, limit: int
) -> None:
    rows = discover(keyword, title=title, act_type=act_type, limit=limit)
    bazowe = [r for r in rows if r["category"] == "bazowy"]
    jednolite = [r for r in rows if r["category"] == "jednolity"]
    zmieniajace = [r for r in rows if r["category"] == "zmieniajacy"]

    crit = " / ".join(
        filter(
            None, [f"keyword={keyword!r}" if keyword else "", f"title={title!r}" if title else ""]
        )
    )
    print(
        f"Kryterium: {crit} → {len(rows)} aktów "
        f"(bazowe {len(bazowe)}, teksty jednolite {len(jednolite)}, "
        f"zmieniające {len(zmieniajace)})\n"
    )

    print("AKTY BAZOWE (główni kandydaci do ingestu):")
    for r in bazowe:
        print(f"  [{r['type']:14}] {r['eli']:14} {r['title'][:88]}")

    if jednolite:
        print("\nTEKSTY JEDNOLITE powiązanych aktów (gotowe wersje skonsolidowane):")
        for r in jednolite:
            print(f"  [{r['type']:14}] {r['eli']:14} {r['title'][:88]}")

    if zmieniajace:
        print("\nNOWELIZACJE (NIE do ingestu — diffy złożone w tekstach jednolitych):")
        for r in zmieniajace[:25]:
            print(f"  · {r['eli']:14} {r['title'][:80]}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Odkrywanie aktów po słowie kluczowym/tytule ELI.")
    p.add_argument("--keyword", default=None, help="słowo kluczowe ISAP (DOKŁADNE; zob. /keywords)")
    p.add_argument("--title", default=None, help="fragment tytułu (pewniejsze pod tematy)")
    p.add_argument("--type", dest="act_type", default=None, help="np. Ustawa / Rozporządzenie")
    p.add_argument("--limit", type=int, default=100)
    args = p.parse_args()

    if not args.keyword and not args.title:
        p.error("podaj --keyword lub --title (albo oba)")
    _print_candidates(args.keyword, args.title, args.act_type, args.limit)


if __name__ == "__main__":
    main()
