#!/usr/bin/env python3
"""
TaxPilot — Agentic RAG nad prawem podatkowym ulg (B+R, IP Box, koszty autorskie).

UI/UX zaadaptowane z LexSearch (onboarding, sidebar z filtrami, czat ze
streamingiem, panel źródeł, agentic tool-calling), podpięte pod backend
TaxPilota: OpenSearch (hybryda BM25+kNN), embedder stella-pl-mini, Ollama Cloud.

Dwa tryby:
  - Czat (Agentic RAG): model sam decyduje, czego szukać w bazie.
  - Asystent kwalifikacji: opis działalności → ocena B+R / IP Box z podstawą prawną.

Uruchomienie:
  streamlit run app.py --server.port 8503
"""

import json
import re
from typing import Any

import requests
import streamlit as st

from config import (
    ACTS,
    DEFAULT_OLLAMA_MODEL,
    OLLAMA_CLOUD_API_KEY,
    OLLAMA_URL,
    SOURCE_INTERPRETACJA,
    SOURCE_OBJASNIENIA,
    SOURCE_ORZECZENIE,
    SOURCE_USTAWA,
    TOP_K,
    ULGI,
)

# ===================== STAŁE UI =====================

ULGA_EMOJI = {"BR": "🔬", "IPBOX": "💡", "PKUP": "✍️", "": "📄"}
SOURCE_LABELS = {
    SOURCE_USTAWA: "Ustawa",
    SOURCE_OBJASNIENIA: "Objaśnienia MF",
    SOURCE_INTERPRETACJA: "Interpretacja",
    SOURCE_ORZECZENIE: "Orzeczenie",
}

st.set_page_config(
    page_title="TaxPilot — Asystent ulg podatkowych",
    page_icon="🧭",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ===================== ROZGRZEWKA EMBEDDERA =====================


@st.cache_resource
def warm_embedder():
    """Ładuje stella-pl-mini raz na proces (cache Streamlit)."""
    from embedder import get_embedder

    return get_embedder()


# ===================== ONBOARDING =====================


def render_onboarding():
    with st.expander("🚀 **Szybki start z TaxPilot**", expanded=True):
        st.caption("Kliknij przykładowe pytanie lub wpisz własne:")
        examples = [
            ("🔬", "Jakie koszty są kwalifikowane w uldze B+R?"),
            ("💡", "Jakie prawa własności intelektualnej kwalifikują się do IP Box?"),
            ("✍️", "Komu przysługują 50% koszty uzyskania przychodu?"),
            ("🔬", "Czy mogę odliczyć wynagrodzenia programistów w uldze B+R?"),
            ("💡", "Jak ustala się dochód z kwalifikowanego IP (wskaźnik nexus)?"),
            ("📑", "Czym jest działalność badawczo-rozwojowa wg ustawy o CIT?"),
        ]
        cols = st.columns(2)
        for idx, (emoji, q) in enumerate(examples):
            if cols[idx % 2].button(f"{emoji} {q}", key=f"ex_{idx}", use_container_width=True):
                st.session_state["_example_query"] = q


# ===================== RETRIEVAL (OpenSearch) =====================


def run_retrieve(query: str, filters: dict, k: int) -> list[dict[str, Any]]:
    """Hybrydowe wyszukiwanie w OpenSearch z filtrami z sidebaru."""
    from search import retrieve

    cache = st.session_state.setdefault("search_cache", {})
    cache_key = f"{query}|{k}|{json.dumps(filters, sort_keys=True)}"
    if cache_key in cache:
        return cache[cache_key]

    docs = retrieve(
        query,
        k=k,
        akt=filters.get("akt"),
        ulga=filters.get("ulga"),
        source_types=filters.get("source_types"),
        on_date=filters.get("on_date"),
    )
    cache[cache_key] = docs
    return docs


def format_tool_results(query: str, docs: list[dict]) -> str:
    """Formatuje wyniki jako kontekst dla modelu (z naciskiem na cytowanie)."""
    if not docs:
        return (
            f"Wyniki dla '{query}': brak w bazie. Poinformuj, że nie znaleziono "
            "podstawy prawnej dla tego zagadnienia."
        )
    parts = [
        f"Wyniki dla '{query}' ({len(docs)} fragmentów). "
        "Cytuj WYŁĄCZNIE te podstawy prawne, nie wymyślaj artykułów ani sygnatur."
    ]
    for i, d in enumerate(docs, 1):
        cite = d.get("citation") or d.get("sygnatura") or d.get("eli_id", "?")
        parts.append(f"[{i}] {cite}:\n{d.get('content_text', '')[:600]}")
    return "\n\n".join(parts)


# ===================== AGENTIC RAG (Ollama Cloud) =====================

_SYSTEM = (
    "Jesteś asystentem prawno-podatkowym wspierającym doradcę. Specjalizujesz się w "
    "uldze B+R (art. 18d CIT / 26e PIT), IP Box (art. 24d CIT / 30ca PIT) i kosztach "
    "autorskich (50% KUP). Masz dostęp do bazy przez funkcję search_database.\n\n"
    "ZASADY:\n"
    "1. Odpowiadaj WYŁĄCZNIE na podstawie wyników z search_database.\n"
    "2. ZAWSZE podawaj konkretną podstawę prawną z wyników (np. art. 18d ust. 2 ustawy o CIT) "
    "oraz sygnatury interpretacji/objaśnień, jeśli są.\n"
    "3. NIE wymyślaj artykułów, sygnatur ani treści przepisów spoza wyników.\n"
    "4. Jeśli wyniki nie zawierają odpowiedzi — powiedz to wprost.\n"
    "5. Dodaj krótkie zastrzeżenie, że to wsparcie informacyjne, nie wiążąca porada podatkowa.\n"
    "Odpowiadaj po polsku. Nie używaj tagów <think>."
)

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_database",
            "description": (
                "Wyszukuje przepisy, objaśnienia i interpretacje podatkowe dot. ulg "
                "(B+R, IP Box, koszty autorskie). Wywołaj wielokrotnie z różnymi zapytaniami."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Zapytanie treściowe po polsku",
                    }
                },
                "required": ["query"],
            },
        },
    }
]


def _ollama_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if OLLAMA_CLOUD_API_KEY:
        h["Authorization"] = f"Bearer {OLLAMA_CLOUD_API_KEY}"
    return h


def parse_tool_calls_from_text(text: str):
    """Fallback dla modeli bez natywnego tool-use: <tool_call>{...}</tool_call>."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    calls = []
    for m in re.findall(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text, re.DOTALL):
        try:
            data = json.loads(m)
            calls.append(
                {
                    "function": {
                        "name": data.get("name"),
                        "arguments": json.dumps(data.get("arguments", {})),
                    }
                }
            )
        except json.JSONDecodeError:
            pass
    return calls or None


def agentic_answer(
    query: str,
    model: str,
    filters: dict,
    k: int,
    chat_history: list[dict[str, str]],
    status=None,
) -> tuple[str, list[dict]]:
    """Pętla agentic z tool-callingiem na Ollama Cloud, streaming do Streamlit."""
    messages = [{"role": "system", "content": _SYSTEM}]
    if chat_history:
        for m in chat_history:
            c = m["content"]
            if m["role"] == "assistant" and len(c) > 500:
                c = c[:500] + "..."
            messages.append({"role": m["role"], "content": c})
    messages.append({"role": "user", "content": query})

    all_sources: list[dict] = []
    final = ""
    url = f"{OLLAMA_URL}/api/chat"

    for iteration in range(3):
        payload = {
            "model": model,
            "messages": messages,
            "tools": _TOOLS,
            "stream": True,
            "options": {"temperature": 0.2, "num_ctx": 16384},
        }
        try:
            r = requests.post(
                url, json=payload, headers=_ollama_headers(), stream=True, timeout=300
            )
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            st.error(f"Błąd Ollama: {e}")
            break

        content = ""
        tool_calls: list[dict] = []
        placeholder = st.empty()
        for line in r.iter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = chunk.get("message", {})
            tok = msg.get("content") or ""
            if tok:
                content += tok
                disp = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
                if disp:
                    placeholder.markdown(disp + "▌")
            if msg.get("tool_calls"):
                tool_calls.extend(msg["tool_calls"])
            if chunk.get("done"):
                break

        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        if content:
            placeholder.markdown(content)
            final = content
        else:
            placeholder.empty()

        if not tool_calls and content:
            tool_calls = parse_tool_calls_from_text(content) or []

        messages.append({"role": "assistant", "content": content})

        if not tool_calls:
            break

        for tc in tool_calls:
            args = tc["function"].get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"query": args}
            tq = args.get("query", query)
            if status:
                status.write(f"🔍 `{tq}` (iteracja {iteration + 1})")
            docs = run_retrieve(tq, filters, k)
            all_sources.extend(docs)
            messages.append({"role": "tool", "content": format_tool_results(tq, docs)})

    # Bezpiecznik: dołącz wyszukanie po oryginalnym pytaniu
    base = run_retrieve(query, filters, k)
    seen = {d.get("citation") for d in all_sources}
    for d in base:
        if d.get("citation") not in seen:
            all_sources.append(d)
            seen.add(d.get("citation"))

    if not final and all_sources:
        final = "Na podstawie znalezionych przepisów:\n\n" + "\n".join(
            f"- **{d.get('citation', '?')}**: {d.get('content_text', '')[:200]}..."
            for d in all_sources[:5]
        )
    return final, all_sources


# ===================== PANEL ŹRÓDEŁ =====================


def render_sources_panel(docs: list[dict[str, Any]]):
    if not docs:
        return
    seen, unique = set(), []
    for d in docs:
        key = d.get("citation", "")
        if key and key in seen:
            continue
        seen.add(key)
        unique.append(d)
    unique.sort(key=lambda d: -(d.get("_score") or 0))

    with st.expander("📚 Podstawa prawna użyta w odpowiedzi", expanded=False):
        for i, d in enumerate(unique, 1):
            ulga = d.get("ulga", "")
            emoji = ULGA_EMOJI.get(ulga, "📄")
            cite = d.get("citation") or d.get("sygnatura") or d.get("eli_id", "Dokument")
            st.markdown(f"**{emoji} {i}. {cite}**")
            cols = st.columns(3)
            cols[0].metric("Score", f"{(d.get('_score') or 0):.3f}")
            cols[1].metric("Ulga", ULGI.get(ulga, {}).get("name", "—") if ulga else "—")
            cols[2].metric("Źródło", SOURCE_LABELS.get(d.get("source_type", ""), "—"))
            content = d.get("content_text", "")
            if content:
                st.text(content[:800] + ("..." if len(content) > 800 else ""))
            st.markdown("---")


# ===================== SIDEBAR =====================


def render_sidebar() -> dict:
    with st.sidebar:
        st.header("⚙️ Ustawienia")

        st.subheader("🤖 Model")
        model = st.text_input("Model Ollama Cloud", value=DEFAULT_OLLAMA_MODEL)

        st.markdown("---")
        st.subheader("🎯 Ulga")
        ulga_opts = [("Wszystkie", None)] + [(v["name"], k) for k, v in ULGI.items()]
        ulga = st.selectbox("Filtruj po uldze", ulga_opts, format_func=lambda x: x[0])[1]

        st.subheader("📚 Akt prawny")
        akt_opts = [("Wszystkie", None)] + [(v["title"], k) for k, v in ACTS.items()]
        akt = st.selectbox("Filtruj po akcie", akt_opts, format_func=lambda x: x[0])[1]

        st.subheader("📄 Typ źródła")
        src_all = [SOURCE_USTAWA, SOURCE_OBJASNIENIA, SOURCE_INTERPRETACJA, SOURCE_ORZECZENIE]
        src = st.multiselect("Ogranicz do", src_all, format_func=lambda s: SOURCE_LABELS.get(s, s))

        st.subheader("📅 Stan prawny na dzień")
        use_date = st.checkbox("Filtruj po dacie obowiązywania")
        on_date = None
        if use_date:
            import datetime

            d = st.date_input("Dzień", value=datetime.date.today())
            on_date = d.isoformat()

        st.markdown("---")
        st.subheader("🔧 Wyszukiwanie")
        top_k = st.slider("Fragmentów w wynikach", 4, 30, TOP_K, 2)
        chat_turns = st.slider("Kontekst czatu (tur)", 0, 10, 5, 1)

        if st.button("🧹 Wyczyść czat"):
            st.session_state.pop("messages", None)
            st.session_state.pop("search_cache", None)
            st.rerun()

    return {
        "model": model,
        "filters": {"ulga": ulga, "akt": akt, "source_types": src or None, "on_date": on_date},
        "top_k": top_k,
        "chat_turns": chat_turns,
    }


def build_chat_history(max_turns: int) -> list[dict[str, str]]:
    msgs = st.session_state.get("messages", [])
    hist = [
        {"role": m["role"], "content": m["content"]}
        for m in msgs
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    return hist[-max_turns * 2 :] if max_turns else []


# ===================== TRYB: CZAT =====================


def view_chat(cfg: dict):
    if "messages" not in st.session_state:
        st.session_state.messages = [
            {
                "role": "assistant",
                "content": "Cześć! Jestem TaxPilot — pomagam w temacie ulg B+R, IP Box i kosztów "
                "autorskich, opierając się na przepisach, objaśnieniach i interpretacjach. "
                "Zadaj pytanie. 👇",
            }
        ]

    for m in st.session_state.messages:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
            if m.get("role") == "assistant" and m.get("sources"):
                render_sources_panel(m["sources"])

    user_query = st.chat_input("Zadaj pytanie o ulgę podatkową...")
    if not user_query and "_example_query" in st.session_state:
        user_query = st.session_state.pop("_example_query")
    if not user_query:
        return

    st.session_state.messages.append({"role": "user", "content": user_query})
    with st.chat_message("user"):
        st.markdown(user_query)

    with st.chat_message("assistant"):
        status = st.empty()
        history = build_chat_history(cfg["chat_turns"])
        with st.spinner("🧭 Analizuję i szukam podstawy prawnej..."):
            response, sources = agentic_answer(
                user_query, cfg["model"], cfg["filters"], cfg["top_k"], history, status
            )
        status.empty()
        render_sources_panel(sources)

    st.session_state.messages.append({"role": "assistant", "content": response, "sources": sources})


# ===================== TRYB: KWALIFIKACJA =====================


def view_qualify(cfg: dict):
    st.subheader("🎯 Asystent kwalifikacji ulg")
    st.caption(
        "Opisz działalność (np. czym zajmuje się zespół / programista), a ocenię "
        "kwalifikację do ulgi B+R i IP Box z podstawą prawną. To wsparcie informacyjne, "
        "nie wiążąca porada podatkowa."
    )
    opis = st.text_area(
        "Opis działalności", height=160, placeholder="Np. Tworzymy autorskie oprogramowanie..."
    )
    ulgi_sel = st.multiselect(
        "Rozpatrywane ulgi",
        ["BR", "IPBOX"],
        default=["BR", "IPBOX"],
        format_func=lambda c: ULGI[c]["name"],
    )

    if st.button("Oceń kwalifikację", type="primary", disabled=not opis.strip()):
        from qualification import assess

        with st.spinner("🧭 Oceniam na podstawie przepisów i objaśnień..."):
            out = assess(
                opis,
                ulgi=ulgi_sel or None,
                on_date=cfg["filters"].get("on_date"),
                model=cfg["model"],
            )
        ocena = out.get("ocena", {})
        for o in ocena.get("oceny", []):
            werdykt = o.get("werdykt", "?")
            color = {
                "kwalifikuje": "🟢",
                "częściowo": "🟡",
                "nie kwalifikuje": "🔴",
                "za mało danych": "⚪",
            }.get(werdykt, "⚪")
            st.markdown(f"### {color} {o.get('ulga', '?')} — {werdykt}")
            st.write(o.get("uzasadnienie", ""))
            if o.get("podstawa_prawna"):
                st.markdown("**Podstawa prawna:** " + "; ".join(o["podstawa_prawna"]))
            if o.get("czego_brakuje"):
                with st.expander("Czego brakuje do jednoznacznej oceny"):
                    for b in o["czego_brakuje"]:
                        st.markdown(f"- {b}")
            st.markdown("---")
        if ocena.get("zastrzezenie"):
            st.info(ocena["zastrzezenie"])
        render_sources_panel(
            [
                {
                    "citation": s.get("citation"),
                    "ulga": s.get("ulga"),
                    "_score": s.get("score"),
                    "content_text": "",
                }
                for s in out.get("sources", [])
            ]
        )


# ===================== MAIN =====================


def main():
    st.title("🧭 TaxPilot")
    st.markdown(
        "**Asystent ulg podatkowych** — B+R · IP Box · koszty autorskie | "
        "oparty na przepisach, objaśnieniach i interpretacjach"
    )

    try:
        warm_embedder()
    except Exception as e:  # noqa: BLE001
        st.warning(f"Embedder nie został rozgrzany: {e}")

    cfg = render_sidebar()
    with st.sidebar:
        st.markdown("---")
        mode = st.radio("🧭 Tryb", ["Czat (Agentic RAG)", "Asystent kwalifikacji"])

    if mode.startswith("Czat"):
        render_onboarding()
        view_chat(cfg)
    else:
        view_qualify(cfg)


if __name__ == "__main__":
    main()
