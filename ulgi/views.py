"""
Widoki TaxPilot.

Tryb „Pytanie" — streaming RAG: retrieve → tokeny z Ollamy (SSE) → źródła z
linkami do oficjalnego tekstu na eli.gov.pl. Cache semantyczny (Redis) na wejściu:
trafienie zwraca odpowiedź natychmiast.

Tryb „Kwalifikacja" — HTMX: opis działalności → qualification.assess() → karty
werdyktów (B+R / IP Box / 50% KUP) z podstawą prawną i listą braków.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests
from django.http import HttpResponseBadRequest, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from config import ACTS

from . import services
from .models import Akt, ChatMessage, ChatSession, IngestJob, QualificationQuery

logger = logging.getLogger(__name__)

# ── Etykiety / klasy CSS ──────────────────────────────────────────────────
ULGA_LABELS = {"BR": "B+R", "IPBOX": "IP Box", "PKUP": "50% KUP"}
ULGA_CLS = {"BR": "br", "IPBOX": "ipbox", "PKUP": "pkup"}
VERDICT_CLS = {
    "kwalifikuje": "kw",
    "częściowo": "cz",
    "nie kwalifikuje": "nie",
    "za mało danych": "brak",
}

# ── Ankieta kwalifikacji ──────────────────────────────────────────────────
# Ustrukturyzowane pytania o przesłanki (radio tak/nie/nie wiem) — wolny opis
# rzadko je adresuje, przez co model odpowiadał „za mało danych". Odpowiedzi
# doklejane są do opisu jako sekcja „Ankieta (deklaracje)": zasilają zarówno
# prompt oceny, jak i retrieval (terminy typu „honorarium", „ewidencja").
ANKIETA_FORMA = {
    "b2b": "działalność gospodarcza (B2B)",
    "uop": "umowa o pracę",
    "cyw": "umowa zlecenie / o dzieło",
}
ANKIETA_PYTANIA: dict[str, list[tuple[str, str]]] = {
    "BR": [
        ("ank_br_syst", "Prace prowadzone są systematycznie (plan, harmonogram, projekty)"),
        ("ank_br_tworcze", "Rezultaty mają twórczy charakter (nowe rozwiązania, nie rutyna)"),
        ("ank_br_ewid", "Prowadzona jest ewidencja czasu/kosztów prac B+R"),
    ],
    "IPBOX": [
        ("ank_ip_kip", "Powstaje kwalifikowane IP (np. autorskie prawo do programu komputerowego)"),
        ("ank_ip_zbr", "IP jest wytwarzane/rozwijane w ramach własnej działalności B+R"),
        ("ank_ip_ewid", "Prowadzona jest odrębna ewidencja dochodów z IP (wskaźnik nexus)"),
    ],
    "PKUP": [
        ("ank_kup_utwor", "Efektem pracy są utwory w rozumieniu prawa autorskiego"),
        ("ank_kup_prawa", "Prawa autorskie są przenoszone na pracodawcę/zamawiającego"),
        ("ank_kup_hon", "Honorarium autorskie jest kwotowo wyodrębnione w umowie"),
        ("ank_kup_ewid", "Prowadzona jest ewidencja utworów"),
    ],
}
_ANKIETA_ODP = {"tak": "tak", "nie": "nie", "niewiem": "nie wiem"}


def _ankieta_text(post, ulgi: list[str]) -> str:
    """Składa sekcję „Ankieta (deklaracje)" z odpowiedzi formularza.

    Uwzględnia tylko pytania ulg wskazanych w `ulgi` (radio ukrytej sekcji też
    trafia do POST) i tylko udzielone odpowiedzi — brak odpowiedzi = brak
    deklaracji (model potraktuje jak brak danych). Zwraca "" gdy pusto.
    """
    lines: list[str] = []
    forma = ANKIETA_FORMA.get(post.get("ank_forma", ""))
    if forma:
        lines.append(f"- Forma współpracy: {forma}")
    for code in ulgi:
        for name, label in ANKIETA_PYTANIA.get(code, []):
            odp = _ANKIETA_ODP.get(post.get(name, ""))
            if odp:
                lines.append(f"- [{ULGA_LABELS.get(code, code)}] {label}: {odp}")
    return "\n".join(lines)


# Sufiksy cytatów (do rozbicia „art. 18d ust. 2 | ustawy o CIT").
_SUFFIXES = sorted({a["citation_suffix"] for a in ACTS.values()}, key=len, reverse=True)


def _split_cit(cit: str) -> tuple[str, str]:
    for suf in _SUFFIXES:
        if cit.endswith(suf):
            return cit[: -len(suf)].strip(), suf
    return cit, ""


def _eli_gov_url(eli_id: str) -> str:
    """ELI → kanoniczny adres strony aktu na eli.gov.pl."""
    return f"https://eli.gov.pl/eli/{eli_id}" if eli_id else ""


def _source_view(d: dict) -> dict:
    """Ujednolicony widok źródła: rozbity cytat + tag ulgi + link do tekstu."""
    raw = d.get("citation") or d.get("sygnatura") or d.get("eli_id", "")
    base, suf = _split_cit(raw)
    ulga = d.get("ulga") or ""
    return {
        "cit": base,
        "suf": suf,
        "ulga_cls": ULGA_CLS.get(ulga, ""),
        "ulga_label": ULGA_LABELS.get(ulga, ""),
        "url": d.get("zrodlo_url") or _eli_gov_url(d.get("eli_id", "")),
        "text": (d.get("content_text") or "").strip(),
    }


# ── Health-check ───────────────────────────────────────────────────────────
def healthz(request):
    """Lekki health-check dla nginx / systemd / monitoringu.

    Liveness zawsze 200, jeśli proces odpowiada. Readiness sprawdza tylko bazę
    (system of record) — OpenSearch/Redis/Ollama celowo pomijamy, by sonda nie
    robiła wywołań sieciowych przy każdym pingu (strona degraduje bez nich).
    """
    from django.db import connection

    checks: dict[str, str] = {}
    healthy = True
    try:
        connection.ensure_connection()
        checks["db"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["db"] = f"error: {e}"
        healthy = False

    return JsonResponse(
        {"status": "ok" if healthy else "degraded", "checks": checks},
        status=200 if healthy else 503,
    )


# ── Strona główna ─────────────────────────────────────────────────────────
def chat(request):
    ctx: dict[str, Any] = {
        "corpus_note": "korpus: ustawy CIT·PIT·ORD + objaśnienia MF + interpretacje KIS"
    }
    # Pieczęć „stan prawny" z ostatniego udanego ingestu (degraduje, gdy brak DB/danych).
    try:
        akt = (
            Akt.objects.exclude(last_ingested_at=None)
            .exclude(eli_id="")  # pieczęć tylko dla realnych aktów (nie objaśnień)
            .order_by("-last_ingested_at")
            .first()
        )
        if akt:
            job = (
                IngestJob.objects.filter(akt=akt, status="success").order_by("-finished_at").first()
            )
            d = job.obowiazuje_od if job else None
            ctx["stan_prawny"] = d.strftime("%d.%m.%Y") if d else None
            ctx["seal_status"] = akt.eli_id or "ELI"
            ctx["seal_title"] = f"Stan prawny z tekstu jednolitego {akt.eli_id}"
            ctx["nowele_po_tj"] = akt.nowele_po_tj
            if akt.nowele_po_tj:
                n = (akt.nowele or [{}])[0]
                ctx["nowele_title"] = (
                    f"{akt.nowele_po_tj} nowelizacji uchwalonych po tym tekście jednolitym; "
                    f"najnowsza: {n.get('eli', '')} z {n.get('date', '')}. "
                    "Stan prawny może nie obejmować najnowszych zmian."
                )
    except Exception:  # noqa: BLE001 — brak bazy/danych nie może wywalić strony
        pass
    return render(request, "ulgi/chat.html", ctx)


# ── SSE helpers ───────────────────────────────────────────────────────────
def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _stream_ollama(query: str, docs: list[dict], model: str | None = None):
    """Strumień tokenów odpowiedzi z Ollamy (reużywa promptu i kontekstu z search)."""
    from config import OLLAMA_URL
    from search import _ollama_headers, build_chat_payload

    payload = build_chat_payload(query, docs, model=model, stream=True)
    with requests.post(
        f"{OLLAMA_URL}/api/chat",
        headers=_ollama_headers(),
        json=payload,
        stream=True,
        timeout=180,
    ) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            delta = obj.get("message", {}).get("content", "")
            if delta:
                yield delta
            if obj.get("done"):
                break


def _save_bot(sess, text: str, sources: list[dict]) -> None:
    if not sess:
        return
    try:
        ChatMessage.objects.create(session=sess, role="assistant", content=text, sources=sources)
    except Exception:  # noqa: BLE001
        pass


@require_POST
def ask(request):
    q = (request.POST.get("q") or "").strip()
    if not q:
        return HttpResponseBadRequest("puste pytanie")
    from search import detect_ulga

    ulga = detect_ulga(q)  # automatyczne zawężenie do ulgi, gdy pytanie jednoznaczne
    filters: dict[str, Any] = {"ulga": ulga} if ulga else {}

    if not request.session.session_key:
        request.session.create()
    skey = request.session.session_key

    def gen():
        from search import retrieve_mixed

        from .cache import get_cache

        try:
            sess, _ = ChatSession.objects.get_or_create(session_key=skey)
            ChatMessage.objects.create(session=sess, role="user", content=q)
        except Exception:  # noqa: BLE001
            sess = None

        # 1. Cache semantyczny — trafienie zwraca odpowiedź natychmiast.
        cache = get_cache()
        try:
            hit = cache.get(q, filters, None)
        except Exception:  # noqa: BLE001
            hit = None
        if hit:
            text, sources, layer = hit
            yield _sse("meta", {"cache": layer})
            yield _sse("token", {"t": text})
            yield _sse("sources", {"items": sources})
            yield _sse("done", {})
            _save_bot(sess, text, sources)
            return

        # 2. Retrieval hybrydowy z kwotą per typ źródła (ustawa + objaśnienia + interpretacje).
        try:
            docs = retrieve_mixed(q, ulga=ulga)
        except Exception as e:  # noqa: BLE001
            yield _sse("error", {"m": f"Wyszukiwarka niedostępna ({e})."})
            yield _sse("done", {})
            return
        if not docs:
            yield _sse("token", {"t": "Brak materiału w bazie do odpowiedzi na to pytanie."})
            yield _sse("done", {})
            return

        # 3. Strumień tokenów z modelu.
        full: list[str] = []
        try:
            for delta in _stream_ollama(q, docs):
                full.append(delta)
                yield _sse("token", {"t": delta})
        except Exception as e:  # noqa: BLE001
            yield _sse("error", {"m": f"Model niedostępny ({e})."})
            yield _sse("done", {})
            return

        text = "".join(full)
        sources = [_source_view(d) for d in docs]
        try:
            cache.set(q, filters, None, text, sources)
        except Exception:  # noqa: BLE001
            pass
        yield _sse("sources", {"items": sources})
        yield _sse("done", {})
        _save_bot(sess, text, sources)

    resp = StreamingHttpResponse(gen(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"  # nginx: nie buforuj SSE
    return resp


# ── Kwalifikacja (HTMX) ───────────────────────────────────────────────────
@require_POST
def qualify(request):
    opis = (request.POST.get("opis") or "").strip()
    ulgi = request.POST.getlist("ulgi") or None
    if not opis:
        return render(request, "ulgi/_qualification.html", {"error": "Podaj opis działalności."})

    # Ankieta przesłanek (tak/nie/nie wiem) dokleja się do opisu — trafia i do
    # retrievalu, i do promptu oceny. Zapisujemy pełny tekst (audyt).
    ankieta = _ankieta_text(request.POST, ulgi or list(ANKIETA_PYTANIA))
    if ankieta:
        opis = f"{opis}\n\nAnkieta (deklaracje):\n{ankieta}"

    try:
        out = services.qualify(opis, ulgi=ulgi)
    except Exception as e:  # noqa: BLE001
        return render(request, "ulgi/_qualification.html", {"error": f"Ocena niedostępna ({e})."})

    ocena = out.get("ocena", {})
    oceny = [
        {**o, "cls": VERDICT_CLS.get(o.get("werdykt", ""), "brak")} for o in ocena.get("oceny", [])
    ]
    try:
        QualificationQuery.objects.create(opis=opis, ulgi=ulgi or [], result=ocena)
    except Exception:  # noqa: BLE001
        pass

    return render(
        request,
        "ulgi/_qualification.html",
        {
            "oceny": oceny,
            "zastrzezenie": ocena.get("zastrzezenie", ""),
            "sources": [_source_view(d) for d in out.get("sources", [])],
        },
    )
