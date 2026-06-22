"""
kis_client — klient publicznego API EUREKA (interpretacje KIS).

Bez logowania i bez tokenu:
    GET https://eureka.mf.gov.pl/api/public/v1/informacje/{id}
zwraca dokument jako JSON. Treść i metadane są w `dokument.fields` jako lista
obiektów {dataType, key, value}; stąd wyciągamy sygnaturę (SYG), tezę (TEZA),
datę wydania (DT_WYD), status, kategorię i pełną treść HTML (TRESC_INTERESARIUSZ).
"""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

EUREKA_API = "https://eureka.mf.gov.pl/api/public/v1"
_HEADERS = {"User-Agent": "TaxPilot/1.0", "Accept": "application/json"}
DEFAULT_TIMEOUT = 90  # EUREKA potrafi odpowiadać wolno


def _build_session() -> requests.Session:
    """Sesja z ponawianiem: timeouty/zerwania + 5xx/429, wykładniczy backoff."""
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=2,  # przerwy 0, 2, 4, 8 s
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),  # POST też (zapytanie idempotentne)
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s = requests.Session()
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update(_HEADERS)
    return s


_SESSION = _build_session()


def _fields_map(doc: dict) -> dict:
    """dokument.fields (lista {key, value}) → płaski słownik {key: value}."""
    out: dict = {}
    for f in (doc.get("dokument") or {}).get("fields", []) or []:
        if isinstance(f, dict) and "key" in f:
            out[f["key"]] = f.get("value")
    return out


def fetch_interpretacja(info_id: int | str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Pobiera interpretację po ID informacji EUREKA.

    Zwraca {id, sygnatura, teza, data_wyd (YYYY-MM-DD), status, kategoria,
    nazwa, tresc_html}. Rzuca ValueError, gdy brak treści.
    """
    r = _SESSION.get(f"{EUREKA_API}/informacje/{info_id}", timeout=timeout)
    r.raise_for_status()
    doc = r.json()
    fm = _fields_map(doc)

    tresc = fm.get("TRESC_INTERESARIUSZ") or ""
    if not tresc.strip():
        raise ValueError(f"Brak treści (TRESC_INTERESARIUSZ) dla informacji {info_id}.")

    return {
        "id": str(fm.get("ID_INFORMACJI") or info_id),
        "sygnatura": (fm.get("SYG") or "").strip(),
        "teza": (fm.get("TEZA") or "").strip(),
        "data_wyd": (fm.get("DT_WYD") or "")[:10],  # ISO → YYYY-MM-DD
        "status": str(fm.get("STATUS_INFORMACJI") or ""),
        "kategoria": str(fm.get("KATEGORIA_INFORMACJI") or ""),
        "nazwa": doc.get("nazwa") or "Interpretacja indywidualna",
        "tresc_html": tresc,
    }


_SEARCH_COLUMNS = [
    "ID_INFORMACJI", "SYG", "DT_WYD", "TEZA",
    "STATUS_INFORMACJI", "KATEGORIA_INFORMACJI",
]


def _first(v):
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def _search_row(row: dict) -> dict:
    return {
        "id": str(row.get("ID_INFORMACJI") or ""),
        "sygnatura": _first(row.get("SYG")),
        "data": _first(row.get("DT_WYD")),
        "teza": _first(row.get("TEZA")),
        "status": _first(row.get("STATUS_INFORMACJI")),
        "kategoria": _first(row.get("KATEGORIA_INFORMACJI")),
    }


def search_interpretacje(
    przepisy_ids,
    *,
    kategoria_id: int | None = 1,    # KATEGORIA_INFORMACJI: 1 = Interpretacja indywidualna
    status_id: int | None = 27,      # STATUS_INFORMACJI: 27 = Aktualna
    od_daty: str | None = None,      # DT_WYD_start, format YYYY-MM-DD
    do_daty: str | None = None,      # DT_WYD_end, format YYYY-MM-DD
    limit: int = 50,
    page_size: int = 25,
    max_pages: int = 40,
    timeout: int = DEFAULT_TIMEOUT,
) -> list[dict]:
    """Wyszukuje interpretacje po ID przepisów (filtr PRZEPISY), z filtrowaniem
    serwerowym po kategorii, statusie i dacie wydania. Zwraca najnowsze `limit`
    jako listę {id, sygnatura, data, teza, status, kategoria}.

    Kody filtrów (potwierdzone w HAR): kategoria 1 = „Interpretacja indywidualna",
    status 27 = „Aktualna". Przekaż None, by danego filtra nie nakładać.
    """
    flt: dict = {"PRZEPISY": [int(x) for x in przepisy_ids]}
    if kategoria_id is not None:
        flt["KATEGORIA_INFORMACJI"] = [int(kategoria_id)]
    if status_id is not None:
        flt["STATUS_INFORMACJI"] = [int(status_id)]
    if od_daty:
        flt["DT_WYD_start"] = od_daty
    if do_daty:
        flt["DT_WYD_end"] = do_daty

    body = {"filter": flt, "columns": _SEARCH_COLUMNS}
    collected: list[dict] = []
    for page in range(max_pages):
        params = {"size": page_size, "page": page, "sort": "parametryPozycjonowania,asc"}
        r = _SESSION.post(
            f"{EUREKA_API}/wyszukiwarka/informacje/",
            params=params, json=body, timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        rows = data.get("results") or []
        if not rows:
            break
        collected.extend(_search_row(raw) for raw in rows)
        total = data.get("totalHits") or 0
        if len(collected) >= limit or (page + 1) * page_size >= total:
            break
    collected.sort(key=lambda x: x["data"], reverse=True)  # najnowsze pierwsze
    return collected[:limit]
