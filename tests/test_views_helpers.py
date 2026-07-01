"""Testy helperów prezentacyjnych widoków (Django skonfigurowane przez pytest-django).

Nie dotykają bazy — sprawdzają czyste funkcje formatujące źródła i SSE.
"""

from __future__ import annotations

import json

from ulgi.views import _eli_gov_url, _source_view, _split_cit, _sse


def test_split_cit_separates_known_suffix():
    base, suf = _split_cit("art. 18d ust. 2 ustawy o CIT")
    assert base == "art. 18d ust. 2"
    assert suf == "ustawy o CIT"


def test_split_cit_unknown_suffix_returns_whole():
    base, suf = _split_cit("Interpretacja 0114-KDIP")
    assert base == "Interpretacja 0114-KDIP"
    assert suf == ""


def test_eli_gov_url():
    assert _eli_gov_url("DU/1992/86") == "https://eli.gov.pl/eli/DU/1992/86"
    assert _eli_gov_url("") == ""


def test_source_view_maps_ulga_and_link():
    d = {
        "citation": "art. 18d ust. 2 ustawy o CIT",
        "ulga": "BR",
        "eli_id": "DU/1992/86",
        "content_text": "  Koszty kwalifikowane  ",
    }
    view = _source_view(d)
    assert view["cit"] == "art. 18d ust. 2"
    assert view["suf"] == "ustawy o CIT"
    assert view["ulga_cls"] == "br"
    assert view["ulga_label"] == "B+R"
    assert view["url"] == "https://eli.gov.pl/eli/DU/1992/86"
    assert view["text"] == "Koszty kwalifikowane"


def test_source_view_prefers_explicit_zrodlo_url():
    d = {"sygnatura": "0114-KDIP", "zrodlo_url": "https://eureka.mf.gov.pl/x", "eli_id": ""}
    view = _source_view(d)
    assert view["url"] == "https://eureka.mf.gov.pl/x"


def test_sse_formats_event_and_json_payload():
    out = _sse("token", {"t": "ąćź"})
    assert out.startswith("event: token\ndata: ")
    assert out.endswith("\n\n")
    data_line = out.split("data: ", 1)[1].strip()
    assert json.loads(data_line) == {"t": "ąćź"}


# ── Ankieta kwalifikacji ──────────────────────────────────────────────────


def test_ankieta_text_sklada_tylko_udzielone_odpowiedzi():
    from ulgi.views import _ankieta_text

    post = {
        "ank_forma": "uop",
        "ank_br_syst": "tak",
        "ank_br_ewid": "niewiem",
        # ank_br_tworcze — brak odpowiedzi → pomijane
    }
    out = _ankieta_text(post, ["BR"])
    assert "- Forma współpracy: umowa o pracę" in out
    assert "systematycznie" in out and ": tak" in out
    assert "ewidencja czasu/kosztów prac B+R: nie wiem" in out
    assert "twórczy charakter" not in out


def test_ankieta_text_filtruje_po_wybranych_ulgach():
    from ulgi.views import _ankieta_text

    # Odpowiedź PKUP w POST (ukryta sekcja też submituje), ale ulga niewybrana.
    post = {"ank_kup_hon": "tak", "ank_ip_kip": "tak"}
    out = _ankieta_text(post, ["IPBOX"])
    assert "kwalifikowane IP" in out
    assert "Honorarium" not in out


def test_ankieta_text_pusta_gdy_brak_deklaracji():
    from ulgi.views import _ankieta_text

    assert _ankieta_text({}, ["BR", "IPBOX", "PKUP"]) == ""
    # Nieznana wartość (spoza tak/nie/niewiem) też nie wchodzi do ankiety.
    assert _ankieta_text({"ank_br_syst": "moze"}, ["BR"]) == ""
