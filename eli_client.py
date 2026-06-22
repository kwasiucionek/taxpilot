"""
eli_client.py — pobieranie tekstów aktów z ELI API (api.sejm.gov.pl).

Kluczowa idea: w config.ACTS trzymamy adres aktu PIERWOTNEGO (np. ustawa o CIT
= DU/1992/86), a AKTUALNY tekst jednolity ustalamy w locie z referencji ELI.
Dzięki temu po ogłoszeniu nowego t.j. ingest sam pobierze najnowszą wersję —
bez ręcznego podbijania pozycji w configu.

Dodatkowo wykrywamy „Nowelizacje po tekście jednolitym" — ustawy zmieniające
uchwalone PO ogłoszeniu t.j. (zmiany jeszcze nieujęte w tekście). To sygnał
aktualności: pozwala ostrzec, że stan prawny może nie obejmować najnowszych zmian.

Endpointy ELI:
  GET /acts/{pub}/{year}/{pos}                      → metadane (textHTML/textPDF, texts[], legalStatusDate)
  GET /acts/{pub}/{year}/{pos}/references           → mapa typ → [referencje]
  GET /acts/{pub}/{year}/{pos}/text.html            → tekst (HTML)
  GET /acts/{pub}/{year}/{pos}/text/{type}/{file}   → konkretny plik (O/I/T)
  GET /acts/{pub}/{year}/{pos}/text.pdf             → tekst (PDF, generyczny)

Świeże teksty jednolite (obwieszczenia) mają textHTML=false — istnieją tylko
jako PDF. W metadanych `texts` jest osobny plik typu "T" = tekst jednolity
(czystszy niż "O" = ogłoszony, z preambułą), więc gdy nie ma HTML, pobieramy
właśnie ten plik.
"""

from __future__ import annotations

import io
import logging
import re

import requests
from bs4 import BeautifulSoup

from config import ELI_API_BASE

logger = logging.getLogger(__name__)

_UA = {"User-Agent": "taxpilot/0.1"}
_JSON = {**_UA, "Accept": "application/json"}
_MIN_TEXT_LEN = 200
# Priorytet plików PDF: tekst jednolity > ogłoszony > pozostałe.
_PDF_TYPE_PRIORITY = ("T", "O", "I")


def _split_eli(eli: str) -> tuple[str, int, int]:
    """'DU/1992/86' → ('DU', 1992, 86)."""
    pub, year, pos = eli.split("/")
    return pub, int(year), int(pos)


def eli_id(publisher: str, year: int, position: int) -> str:
    return f"{publisher}/{year}/{position}"


def _ref_entry(entry: dict) -> tuple[str | None, str | None, str]:
    """Z wpisu referencji wyciąga (eli, data, tytuł) niezależnie od kształtu —
    ELI zwraca albo {'id','date'}, albo {'act': {...}}."""
    if isinstance(entry.get("act"), dict):
        a = entry["act"]
        return (
            a.get("ELI") or a.get("id"),
            entry.get("date") or a.get("promulgation"),
            a.get("title", ""),
        )
    return entry.get("id"), entry.get("date"), entry.get("title", "")


# ─────────────────────────── METADANE / REFERENCJE ───────────────────────────

def fetch_metadata(publisher: str, year: int, position: int) -> dict:
    url = f"{ELI_API_BASE}/acts/{publisher}/{year}/{position}"
    r = requests.get(url, headers=_JSON, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_references(publisher: str, year: int, position: int) -> dict:
    url = f"{ELI_API_BASE}/acts/{publisher}/{year}/{position}/references"
    r = requests.get(url, headers=_JSON, timeout=30)
    r.raise_for_status()
    return r.json()


def resolve_consolidated(original_eli: str) -> dict:
    """Dla aktu pierwotnego zwraca metadane (skrót z referencji) NAJNOWSZEGO,
    obowiązującego tekstu jednolitego.

    W referencjach aktu pierwotnego klucz „Inf. o tekście jednolitym" zawiera
    listę obwieszczeń (typ „Obwieszczenie") posortowaną od najnowszego. Wybieramy
    wpis o statusie „obowiązujący" i najpóźniejszej dacie ogłoszenia.
    """
    pub, year, pos = _split_eli(original_eli)
    refs = fetch_references(pub, year, pos)

    candidates: list[dict] = []
    for key, items in refs.items():
        if "jednolit" not in key.lower() or not isinstance(items, list):
            continue
        for it in items:
            act = it.get("act", it) if isinstance(it, dict) else {}
            if act.get("type") == "Obwieszczenie" and act.get("ELI"):
                candidates.append(act)

    if not candidates:
        raise ValueError(
            f"Brak referencji 'tekst jednolity' (Obwieszczenie) dla {original_eli}"
        )

    def _rank(a: dict) -> tuple:
        return (
            1 if a.get("status") == "obowiązujący" else 0,
            a.get("promulgation") or a.get("announcementDate") or "",
        )

    best = max(candidates, key=_rank)
    logger.info(
        "Tekst jednolity %s → %s (%s, ogł. %s)",
        original_eli, best["ELI"], best.get("status"), best.get("promulgation"),
    )
    return best


def post_jednolity_amendments(tj_eli: str) -> list[dict]:
    """Nowelizacje uchwalone PO tekście jednolitym (zmiany jeszcze nieujęte w t.j.).

    Czytane z referencji t.j. — klucz „Nowelizacje po tekście jednolitym".
    Zwraca [{eli, date, title}, ...] posortowane od najnowszej; pusta lista =
    tekst jednolity w pełni aktualny.
    """
    pub, year, pos = _split_eli(tj_eli)
    refs = fetch_references(pub, year, pos)

    out: list[dict] = []
    for key, vals in refs.items():
        if "nowelizacje po tek" not in key.lower() or not isinstance(vals, list):
            continue
        for v in vals:
            if not isinstance(v, dict):
                continue
            eli, dt, title = _ref_entry(v)
            out.append({"eli": eli, "date": dt, "title": title})

    out.sort(key=lambda x: x.get("date") or "", reverse=True)
    return out


# ─────────────────────────── EKSTRAKCJA TEKSTU ───────────────────────────

def html_to_text(html: str) -> str:
    """Strip HTML → tekst; bloki łamiemy nową linią (regexy chunkingu liczą na
    „Art. N." / ustępy na początku linii)."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "div", "li", "tr", "h1", "h2", "h3", "h4"]):
        block.append("\n")
    lines = [ln.strip() for ln in soup.get_text().splitlines()]
    return "\n".join(ln for ln in lines if ln)


# Nagłówki/stopki stron ISAP do odfiltrowania (cała linia = element redakcyjny strony).
_RE_PDF_NOISE = re.compile(
    r"^(?:"
    r"Dz\.?\s*U\.?\s*\d{4}\s+poz\.\s*\d+"       # nagłówek strony: „Dz. U. 2026 poz. 554"
    r"|.*Kancelaria Sejmu.*"                     # stopka redakcyjna ISAP
    r"|[–-]\s*\d+\s*[–-]"                        # numer strony: „– 5 –"
    r")\s*$"
)
# Wyraz przeniesiony z podziałem sylab na końcu wiersza: „opodat-\nkowania".
_RE_HYPHEN_WRAP = re.compile(r"([a-ząćęłńóśźż])-\n([a-ząćęłńóśźż])")


def pdf_to_text(data: bytes) -> str:
    """Tekst z PDF (pdfplumber): usuwa nagłówki/stopki stron ISAP i skleja
    wyrazy rozbite myślnikiem na końcu wiersza."""
    import pdfplumber

    pages: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")

    lines = []
    for ln in "\n".join(pages).splitlines():
        s = ln.strip()
        if not s or _RE_PDF_NOISE.match(s):
            continue
        lines.append(s)
    text = "\n".join(lines)

    # Sklejamy „opodat-\nkowania" → „opodatkowania". Po usunięciu nagłówków/stopek
    # działa też przez granicę strony. Złożenia z dywizem w jednym wierszu
    # („badawczo-rozwojową") zostają nietknięte — nie mają \n po myślniku.
    text = _RE_HYPHEN_WRAP.sub(r"\1\2", text)
    return text


def _pick_pdf_url(base: str, texts: list | None) -> str:
    """URL PDF: preferuj tekst jednolity (typ T), potem ogłoszony, w ostateczności
    generyczny text.pdf."""
    if texts:
        by_type: dict[str, str] = {}
        for t in texts:
            typ, fn = t.get("type"), t.get("fileName")
            if typ and fn:
                by_type.setdefault(typ, fn)
        for typ in _PDF_TYPE_PRIORITY:
            if typ in by_type:
                return f"{base}/text/{typ}/{by_type[typ]}"
    return f"{base}/text.pdf"


def fetch_text(
    publisher: str, year: int, position: int, *, meta: dict | None = None
) -> str:
    """Tekst aktu: HTML jeśli dostępny, inaczej PDF (preferując tekst jednolity).
    `meta` można podać, by nie pobierać metadanych drugi raz."""
    base = f"{ELI_API_BASE}/acts/{publisher}/{year}/{position}"

    if meta is None:
        try:
            meta = fetch_metadata(publisher, year, position)
        except requests.RequestException as e:
            logger.warning("Metadane nieosiągalne (%s)", e)
            meta = {}

    # 1. HTML — tylko gdy realnie istnieje (albo brak metadanych, więc próbujemy).
    if meta.get("textHTML") or not meta:
        try:
            r = requests.get(f"{base}/text.html", headers=_UA, timeout=60)
            if r.ok and r.text.strip():
                text = html_to_text(r.text)
                if len(text) >= _MIN_TEXT_LEN:
                    logger.info("Tekst z HTML (%d znaków)", len(text))
                    return text
        except requests.RequestException as e:
            logger.warning("HTML nieosiągalny (%s)", e)

    # 2. PDF — preferuj plik typu T (tekst jednolity).
    url = _pick_pdf_url(base, meta.get("texts"))
    r = requests.get(url, headers=_UA, timeout=180)
    r.raise_for_status()
    text = pdf_to_text(r.content)
    logger.info("Tekst z PDF %s (%d znaków)", url.rsplit("/", 1)[-1], len(text))
    return text


def fetch_act(act: dict) -> tuple[str, str, dict]:
    """Z wpisu config (klucz `original`) rozwiązuje najnowszy tekst jednolity,
    pobiera jego pełne metadane i tekst.

    Zwraca (eli_tj, tekst, metadane_tj). Metadane zawierają m.in. `year`, `pos`,
    `legalStatusDate` (data stanu prawnego) oraz `_nowele_po_tj` — listę nowelizacji
    uchwalonych po t.j. (sygnał aktualności; pusta = pełna aktualność).
    """
    original = act["original"]
    cons = resolve_consolidated(original)
    pub, year, pos = _split_eli(cons["ELI"])
    meta = fetch_metadata(pub, year, pos)

    # Sygnał aktualności: nowelizacje uchwalone już po ogłoszeniu tego t.j.
    nowele = post_jednolity_amendments(cons["ELI"])
    meta["_nowele_po_tj"] = nowele
    if nowele:
        logger.warning(
            "%s: po tekście jednolitym %s weszło %d nowelizacji (najnowsza %s, %s) — "
            "stan prawny może nie obejmować najnowszych zmian.",
            act["short"], cons["ELI"], len(nowele),
            nowele[0].get("eli"), nowele[0].get("date"),
        )
    else:
        logger.info(
            "%s: tekst jednolity %s bez nowelizacji po publikacji (pełna aktualność).",
            act["short"], cons["ELI"],
        )

    logger.info(
        "Pobieram %s (t.j. %s, stan prawny %s)...",
        act["short"], cons["ELI"], meta.get("legalStatusDate"),
    )
    text = fetch_text(pub, year, pos, meta=meta)
    return cons["ELI"], text, meta
