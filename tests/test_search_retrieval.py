"""Testy logiki retrievalu z search.py — bez wywołań sieciowych.

detect_ulga     — heurystyka rozpoznawania ulgi z treści pytania (zawęża tylko
                  przy jednoznacznym trafieniu; wiele ulg / brak trafień → None).
retrieve_mixed  — kwota per typ źródła: kolejność ustawa → objaśnienia →
                  interpretacja, deduplikacja, pomijanie źródeł bez kwoty.
                  `search.retrieve` jest podmieniane monkeypatchem, więc testy
                  nie dotykają OpenSearcha ani embeddera.
"""

from __future__ import annotations

import config
import search
from search import detect_ulga, retrieve_mixed

# ─────────────────────────── detect_ulga ───────────────────────────


def test_detect_ulga_jednoznaczne_trafienia():
    assert detect_ulga("Jakie koszty kwalifikowane obejmuje ulga B+R?") == "BR"
    assert detect_ulga("Jak obliczyć wskaźnik nexus?") == "IPBOX"
    assert detect_ulga("Jak rozliczyć honorarium autorskie?") == "PKUP"


def test_detect_ulga_rozpoznaje_numery_artykulow():
    assert detect_ulga("Co obejmuje art. 18d ustawy o CIT?") == "BR"
    assert detect_ulga("Zasady z art. 30ca ustawy o PIT") == "IPBOX"
    assert detect_ulga("Warunki z art. 22 ust. 9 pkt 3") == "PKUP"


def test_detect_ulga_ignoruje_wielkosc_liter():
    assert detect_ulga("ULGA B+R A OBOWIĄZKI EWIDENCYJNE") == "BR"


def test_detect_ulga_wiele_ulg_nie_zaweza():
    # Pytanie międzyulgowe ma widzieć cały korpus — zawężenie byłoby błędem.
    assert detect_ulga("Czy działalność B+R może korzystać z IP Box?") is None
    # Termin wspólny (koszty kwalifikowane → B+R) + nazwa innej ulgi → też None.
    assert detect_ulga("Jakie koszty kwalifikowane liczy się w IP Box?") is None


def test_detect_ulga_brak_trafien_zwraca_none():
    assert detect_ulga("Jak rozliczyć podatek od najmu mieszkania?") is None
    assert detect_ulga("") is None


# ─────────────────────────── retrieve_mixed ───────────────────────────

_ORDER = (config.SOURCE_USTAWA, config.SOURCE_OBJASNIENIA, config.SOURCE_INTERPRETACJA)


def _fake_retrieve(calls, docs_for=None):
    """Atrapa search.retrieve: rejestruje wywołania i zwraca k dokumentów
    danego typu — albo dokładnie to, co wskazuje docs_for[typ]."""

    def fake(query, *, k, ulga=None, source_types=None, on_date=None, use_hybrid=True):
        assert source_types is not None and len(source_types) == 1
        st = source_types[0]
        calls.append({"source": st, "k": k, "ulga": ulga, "on_date": on_date})
        if docs_for is not None:
            return docs_for.get(st, [])
        return [
            {"citation": f"{st}-{i}", "source_type": st, "content_text": f"treść {st} {i}"}
            for i in range(k)
        ]

    return fake


def test_retrieve_mixed_domyslne_kwoty_i_kolejnosc(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(search, "retrieve", _fake_retrieve(calls))

    docs = retrieve_mixed("dowolne pytanie")

    # Kolejność i liczność wprost z RETRIEVE_MIX — test nie pęka przy strojeniu kwot.
    expected_types = [st for st in _ORDER for _ in range(config.RETRIEVE_MIX[st])]
    assert [d["source_type"] for d in docs] == expected_types
    # Każde źródło odpytane osobno, z własnym k.
    assert [c["source"] for c in calls] == list(_ORDER)
    assert [c["k"] for c in calls] == [config.RETRIEVE_MIX[st] for st in _ORDER]


def test_retrieve_mixed_per_source_pomija_zrodla_bez_kwoty(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(search, "retrieve", _fake_retrieve(calls))

    plan = {config.SOURCE_USTAWA: 2, config.SOURCE_INTERPRETACJA: 1}  # objaśnień brak → 0
    docs = retrieve_mixed("pytanie", per_source=plan)

    assert [c["source"] for c in calls] == [config.SOURCE_USTAWA, config.SOURCE_INTERPRETACJA]
    assert [d["source_type"] for d in docs] == [
        config.SOURCE_USTAWA,
        config.SOURCE_USTAWA,
        config.SOURCE_INTERPRETACJA,
    ]


def test_retrieve_mixed_deduplikuje_po_cytacie_ustawa_wygrywa(monkeypatch):
    # Ten sam cytat we wszystkich źródłach → zostaje jeden dokument,
    # z pierwszego źródła w kolejności (ustawa).
    wspolny = {"citation": "art. 18d ust. 1 ustawy o CIT"}
    docs_for = {
        st: [{**wspolny, "source_type": st, "content_text": f"wersja {st}"}] for st in _ORDER
    }
    monkeypatch.setattr(search, "retrieve", _fake_retrieve([], docs_for=docs_for))

    docs = retrieve_mixed("pytanie")

    assert len(docs) == 1
    assert docs[0]["source_type"] == config.SOURCE_USTAWA


def test_retrieve_mixed_dedup_po_tresci_gdy_brak_cytatu(monkeypatch):
    # Bez cytatu kluczem deduplikacji jest początek treści (80 znaków).
    ta_sama = "x" * 100
    docs_for = {
        config.SOURCE_USTAWA: [{"source_type": config.SOURCE_USTAWA, "content_text": ta_sama}],
        config.SOURCE_OBJASNIENIA: [
            {"source_type": config.SOURCE_OBJASNIENIA, "content_text": ta_sama}
        ],
        config.SOURCE_INTERPRETACJA: [
            {"source_type": config.SOURCE_INTERPRETACJA, "content_text": "zupełnie inna treść"}
        ],
    }
    monkeypatch.setattr(search, "retrieve", _fake_retrieve([], docs_for=docs_for))

    docs = retrieve_mixed("pytanie")

    assert len(docs) == 2
    assert [d["source_type"] for d in docs] == [
        config.SOURCE_USTAWA,
        config.SOURCE_INTERPRETACJA,
    ]


def test_retrieve_mixed_puste_zrodlo_nie_przerywa(monkeypatch):
    # Źródło bez wyników (np. niezaindeksowane objaśnienia) nie psuje reszty.
    docs_for = {
        config.SOURCE_USTAWA: [
            {"citation": "u1", "source_type": config.SOURCE_USTAWA, "content_text": "a"}
        ],
        config.SOURCE_OBJASNIENIA: [],
        config.SOURCE_INTERPRETACJA: [
            {"citation": "i1", "source_type": config.SOURCE_INTERPRETACJA, "content_text": "b"}
        ],
    }
    monkeypatch.setattr(search, "retrieve", _fake_retrieve([], docs_for=docs_for))

    docs = retrieve_mixed("pytanie")

    assert [d["citation"] for d in docs] == ["u1", "i1"]


def test_retrieve_mixed_przekazuje_ulge_i_date(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(search, "retrieve", _fake_retrieve(calls))

    retrieve_mixed("pytanie", ulga="BR", on_date="2026-01-01")

    assert all(c["ulga"] == "BR" for c in calls)
    assert all(c["on_date"] == "2026-01-01" for c in calls)
