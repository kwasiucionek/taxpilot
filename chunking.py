"""
chunking.py — podział aktu prawnego na jednostki redakcyjne.

Jednostka cytowania w prawie podatkowym to ARTYKUŁ (np. „art. 18d CIT"),
dlatego podstawowym chunkiem jest cały artykuł. Długie artykuły (wiele
ustępów) dzielimy po USTĘPACH, zachowując nagłówek artykułu w treści
chunka — embedding ma wtedy kontekst, a cytat jest precyzyjny
(„art. 18d ust. 2 ustawy o CIT").

Segmenty dłuższe niż CHUNK_MAX_CHARS (np. art. 16 z rozbudowaną listą,
albo pojedynczy długi ustęp) są dodatkowo cięte na części po granicach
naturalnych — linie → zdania → w ostateczności twardo po znakach. Dzięki
temu żaden chunk nie przekracza limitu długości embeddera i nic nie jest
obcinane przy indeksacji.

Każdy chunk niesie metadane: akt, numer artykułu, ustęp, znacznik ulgi,
typ źródła oraz gotowy łańcuch cytatu.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field

from config import (
    CHUNK_MAX_CHARS,
    SOURCE_USTAWA,
    ULGI,
)

# Początek artykułu: "Art. 18d." / "Art. 18da." / "Art. 7." (numer + opcjonalne litery)
_RE_ARTICLE = re.compile(r"(?m)^\s*Art\.\s*(\d+[a-z]*)\.\s*")

# Początek ustępu wewnątrz artykułu: linia zaczynająca się od "1. ", "2. " ...
# (z separatorem, żeby nie łapać "1)" punktów ani dat).
_RE_USTEP = re.compile(r"(?m)^\s*(\d+)\.\s+")

# Granica zdania/jednostki przy cięciu długich segmentów (po kropce/średniku/dwukropku).
_RE_SENTENCE = re.compile(r"(?<=[.;:])\s+")


@dataclass
class Chunk:
    content_text: str
    citation: str
    akt: str  # short, np. "CIT"
    article_num: str  # np. "18d"
    ustep: str  # "" gdy chunk = cały artykuł
    ulga: str  # "BR" / "IPBOX" / "PKUP" / ""
    source_type: str
    eli_id: str
    chunk_index: int = 0
    chunk_total: int = 0
    extra: dict = field(default_factory=dict)

    def doc_id(self) -> str:
        key = f"{self.eli_id}:art{self.article_num}:u{self.ustep or '0'}:c{self.chunk_index}"
        return str(uuid.UUID(bytes=hashlib.md5(key.encode()).digest()))

    def to_source(self) -> dict:
        return {
            "content_text": self.content_text,
            "citation": self.citation,
            "akt": self.akt,
            "article_num": self.article_num,
            "ustep": self.ustep,
            "ulga": self.ulga,
            "source_type": self.source_type,
            "eli_id": self.eli_id,
            "chunk_index": self.chunk_index,
            "chunk_total": self.chunk_total,
            **self.extra,
        }


def _detect_ulga(akt_short: str, article_num: str) -> str:
    for code, spec in ULGI.items():
        if article_num in spec["anchors"].get(akt_short, []):
            return code
    return ""


def _split_articles(text: str) -> list[tuple[str, str]]:
    """Zwraca [(numer_artykułu, treść_artykułu_z_nagłówkiem), ...]."""
    matches = list(_RE_ARTICLE.finditer(text))
    out: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        out.append((m.group(1), body))
    return out


def _split_ustepy(article_body: str) -> list[tuple[str, str]]:
    """Dzieli treść artykułu na ustępy. Zwraca [(numer_ustępu, treść), ...].

    Najpierw zdejmujemy nagłówek 'Art. N.', bo ustęp 1 stoi zwykle w tej
    samej linii co numer artykułu ('Art. 18d. 1. ...') i bez tego nie
    zostałby wykryty jako osobny ustęp. Jeśli artykuł nie ma numeracji
    ustępów, zwraca [("", cała_treść_bez_nagłówka)].
    """
    m_art = _RE_ARTICLE.match(article_body)
    rest = article_body[m_art.end() :] if m_art else article_body

    matches = list(_RE_USTEP.finditer(rest))
    if len(matches) < 2:
        return [("", rest.strip())]

    out: list[tuple[str, str]] = []
    pre = rest[: matches[0].start()].strip()  # zwykle pusty po zdjęciu nagłówka
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(rest)
        seg = rest[start:end].strip()
        num = m.group(1)
        if i == 0 and pre:
            seg = f"{pre}\n{seg}"
        out.append((num, seg))
    return out


def _atomize(text: str, max_chars: int) -> list[str]:
    """Rozbija tekst na najmniejsze sensowne jednostki ≤ max_chars.

    Kolejność degradacji: całe linie → zdania (po kropce/średniku) →
    twarde cięcie po znakach (ostateczność dla jednego monstrualnego ciągu).
    """
    step = max(1, max_chars)
    units: list[str] = []
    for line in text.split("\n"):
        line = line.rstrip()
        if not line.strip():
            continue
        if len(line) <= max_chars:
            units.append(line)
            continue
        for sent in _RE_SENTENCE.split(line):
            if not sent.strip():
                continue
            if len(sent) <= max_chars:
                units.append(sent)
            else:
                for i in range(0, len(sent), step):
                    units.append(sent[i : i + step])
    return units


def _split_long(text: str, max_chars: int) -> list[str]:
    """Tnie długi tekst na części ≤ max_chars, pakując jednostki zachłannie."""
    if len(text) <= max_chars:
        return [text]
    out: list[str] = []
    cur = ""
    for unit in _atomize(text, max_chars):
        cand = unit if not cur else f"{cur}\n{unit}"
        if len(cand) <= max_chars:
            cur = cand
        else:
            if cur:
                out.append(cur)
            cur = unit
    if cur:
        out.append(cur)
    return out


def chunk_act(
    text: str,
    *,
    akt_short: str,
    eli_id: str,
    citation_suffix: str,
    source_type: str = SOURCE_USTAWA,
    max_chars: int = CHUNK_MAX_CHARS,
) -> list[Chunk]:
    """Tnie cały tekst aktu na chunki na poziomie artykułu/ustępu."""
    chunks: list[Chunk] = []
    for article_num, body in _split_articles(text):
        ulga = _detect_ulga(akt_short, article_num)
        art_cite = f"art. {article_num} {citation_suffix}"

        # Krótki artykuł — jeden chunk (bez zmian).
        if len(body) <= max_chars:
            chunks.append(
                Chunk(
                    content_text=body,
                    citation=art_cite,
                    akt=akt_short,
                    article_num=article_num,
                    ustep="",
                    ulga=ulga,
                    source_type=source_type,
                    eli_id=eli_id,
                )
            )
            continue

        ustepy = _split_ustepy(body)

        # Długi artykuł bez numeracji ustępów — tniemy treść na części.
        # Pierwsza część zawiera nagłówek 'Art. N.' inline; kolejne dostają
        # tag kontekstowy, żeby embedding wiedział, do czego należą.
        if len(ustepy) == 1:
            prefix = f"[{art_cite}]\n"
            budget = max(200, max_chars - len(prefix))
            for j, part in enumerate(_split_long(body, budget)):
                content = part if j == 0 else f"{prefix}{part}"
                chunks.append(
                    Chunk(
                        content_text=content,
                        citation=art_cite,
                        akt=akt_short,
                        article_num=article_num,
                        ustep="",
                        ulga=ulga,
                        source_type=source_type,
                        eli_id=eli_id,
                    )
                )
            continue

        # Długi artykuł z ustępami — chunk na ustęp, a długie ustępy dodatkowo
        # tniemy, zachowując prefiks kontekstowy [art. N ...] w każdej części.
        prefix = f"[{art_cite}]\n"
        budget = max(200, max_chars - len(prefix))
        for num, seg in ustepy:
            cite = f"art. {article_num} ust. {num} {citation_suffix}" if num else art_cite
            for part in _split_long(seg, budget):
                chunks.append(
                    Chunk(
                        content_text=f"{prefix}{part}",
                        citation=cite,
                        akt=akt_short,
                        article_num=article_num,
                        ustep=num,
                        ulga=ulga,
                        source_type=source_type,
                        eli_id=eli_id,
                    )
                )

    # Numeracja porządkowa.
    total = len(chunks)
    for i, c in enumerate(chunks):
        c.chunk_index = i
        c.chunk_total = total
    return chunks


def chunk_document(
    text: str,
    *,
    kod: str,
    citation: str,
    ulga: str = "",
    source_type: str = SOURCE_USTAWA,
    zrodlo_url: str = "",
    max_chars: int = CHUNK_MAX_CHARS,
) -> list[Chunk]:
    """Chunkuje dokument prozą (objaśnienia MF / interpretacje KIS) — bez struktury
    „Art. N / ustęp". Tnie po granicach naturalnych na części ≤ max_chars (reużywa
    _split_long). `kod` trafia do article_num jako dyskryminator doc_id (eli_id
    zostaje pusty, bo to nie akt ELI), a link do źródła przenosimy w
    extra['zrodlo_url'] — stąd trafia do wyników i do linku w UI.
    """
    chunks: list[Chunk] = []
    for part in _split_long(text, max_chars):
        if not part.strip():
            continue
        chunks.append(
            Chunk(
                content_text=part,
                citation=citation,
                akt=kod,
                article_num=kod,  # dyskryminator doc_id (niewyświetlany)
                ustep="",
                ulga=ulga,
                source_type=source_type,
                eli_id="",
                extra={"zrodlo_url": zrodlo_url} if zrodlo_url else {},
            )
        )
    total = len(chunks)
    for i, c in enumerate(chunks):
        c.chunk_index = i
        c.chunk_total = total
    return chunks
