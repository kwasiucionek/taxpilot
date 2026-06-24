"""Testy budowania kontekstu i payloadu czatu (bez wywołań sieciowych)."""

from __future__ import annotations

from search import _build_context, build_chat_payload

DOCS = [
    {"citation": "art. 18d ust. 2 ustawy o CIT", "content_text": "Koszty kwalifikowane..."},
    {"sygnatura": "0114-KDIP", "content_text": "Interpretacja..."},
]


def test_build_context_uses_citation_then_sygnatura():
    ctx = _build_context(DOCS)
    assert "[art. 18d ust. 2 ustawy o CIT]" in ctx
    assert "[0114-KDIP]" in ctx
    assert "---" in ctx  # separator między blokami


def test_build_chat_payload_structure():
    payload = build_chat_payload("Czy kwalifikuje się B+R?", DOCS, stream=True)
    assert payload["stream"] is True
    roles = [m["role"] for m in payload["messages"]]
    assert roles == ["system", "user"]
    assert "Pytanie: Czy kwalifikuje się B+R?" in payload["messages"][1]["content"]
    assert "Koszty kwalifikowane" in payload["messages"][1]["content"]


def test_build_chat_payload_model_override_and_default():
    default = build_chat_payload("q", DOCS)
    assert default["stream"] is False
    assert default["model"]  # niepusty model domyślny z config

    custom = build_chat_payload("q", DOCS, model="moj-model")
    assert custom["model"] == "moj-model"
