"""Testy podziału aktu na chunki (czysta logika, bez sieci/embeddera)."""

from __future__ import annotations

from chunking import _detect_ulga, _split_long, chunk_act


def test_detect_ulga_matches_anchor():
    assert _detect_ulga("CIT", "18d") == "BR"
    assert _detect_ulga("CIT", "24d") == "IPBOX"
    assert _detect_ulga("PIT", "30ca") == "IPBOX"
    assert _detect_ulga("CIT", "7") == ""  # artykuł spoza kotwic


def test_split_long_respects_max_chars():
    text = "\n".join(f"Linia numer {i} z jakąś treścią." for i in range(200))
    parts = _split_long(text, max_chars=300)
    assert len(parts) > 1
    assert all(len(p) <= 300 for p in parts)
    # Nic nie ginie — suma treści (bez separatorów) zachowuje wszystkie linie.
    assert "Linia numer 199" in "".join(parts)


def test_split_long_short_text_single_part():
    assert _split_long("krótki tekst", max_chars=1000) == ["krótki tekst"]


SAMPLE_ACT = """
Art. 18d. 1. Podatnik uzyskujący przychody odlicza od podstawy opodatkowania
koszty kwalifikowane poniesione na działalność badawczo-rozwojową.
2. Za koszty kwalifikowane uznaje się wynagrodzenia pracowników.

Art. 7. 1. Przedmiotem opodatkowania podatkiem dochodowym jest dochód.
"""


def test_chunk_act_splits_articles_and_tags_ulga():
    chunks = chunk_act(
        SAMPLE_ACT,
        akt_short="CIT",
        eli_id="DU/1992/86",
        citation_suffix="ustawy o CIT",
    )
    assert chunks, "powinien powstać co najmniej jeden chunk"

    by_article = {c.article_num for c in chunks}
    assert "18d" in by_article
    assert "7" in by_article

    art18d = [c for c in chunks if c.article_num == "18d"]
    assert all(c.ulga == "BR" for c in art18d)
    assert all("ustawy o CIT" in c.citation for c in art18d)

    art7 = [c for c in chunks if c.article_num == "7"]
    assert all(c.ulga == "" for c in art7)


def test_chunk_act_assigns_sequential_index_and_total():
    chunks = chunk_act(
        SAMPLE_ACT,
        akt_short="CIT",
        eli_id="DU/1992/86",
        citation_suffix="ustawy o CIT",
    )
    total = len(chunks)
    assert [c.chunk_index for c in chunks] == list(range(total))
    assert all(c.chunk_total == total for c in chunks)


def test_chunk_act_doc_id_is_deterministic():
    a = chunk_act(SAMPLE_ACT, akt_short="CIT", eli_id="DU/1992/86", citation_suffix="ustawy o CIT")
    b = chunk_act(SAMPLE_ACT, akt_short="CIT", eli_id="DU/1992/86", citation_suffix="ustawy o CIT")
    assert [c.doc_id() for c in a] == [c.doc_id() for c in b]
