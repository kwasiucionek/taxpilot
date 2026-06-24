"""Testy czystej logiki kluczy cache (bez Redisa, sieci ani embeddera)."""

from __future__ import annotations

from ulgi.cache import _exact_key, _normalize, _sig


def test_normalize_lowercases_strips_punctuation_and_sorts():
    # "jakie" to stopword (usuwany), reszta zostaje, posortowana alfabetycznie.
    out = _normalize("Jakie KOSZTY kwalifikowane?")
    assert out == "koszty kwalifikowane"
    assert "," not in out and "?" not in out


def test_normalize_is_order_insensitive():
    # Słowa są sortowane, więc kolejność w pytaniu nie zmienia klucza.
    assert _normalize("koszty kwalifikowane B+R") == _normalize("B+R kwalifikowane koszty")


def test_normalize_drops_stopwords_and_single_chars():
    out = _normalize("co to jest ulga")
    assert "to" not in out.split()
    assert "co" not in out.split()
    assert "ulga" in out.split()


def test_sig_is_deterministic_and_filter_sensitive():
    a = _sig({"ulga": "BR"}, None)
    b = _sig({"ulga": "BR"}, None)
    c = _sig({"ulga": "IPBOX"}, None)
    assert a == b
    assert a != c
    assert len(a) == 16


def test_sig_distinguishes_model():
    assert _sig({}, "modelA") != _sig({}, "modelB")


def test_exact_key_stable_across_equivalent_queries():
    # Te same słowa (różna kolejność/wielkość) + te same filtry → ten sam klucz.
    k1 = _exact_key("Ulga B+R koszty", {"ulga": "BR"}, None)
    k2 = _exact_key("koszty b+r ULGA", {"ulga": "BR"}, None)
    assert k1 == k2
    assert k1.startswith("taxpilot:cache:exact:")


def test_exact_key_differs_with_filters():
    assert _exact_key("x y z", {"ulga": "BR"}, None) != _exact_key("x y z", {"ulga": "IPBOX"}, None)
