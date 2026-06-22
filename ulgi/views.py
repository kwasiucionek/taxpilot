"""
Widoki TaxPilot.

Tryb „Pytanie" — streaming RAG: retrieve → tokeny z Ollamy (SSE) → źródła z
linkami do oficjalnego tekstu na eli.gov.pl. Cache semantyczny (Redis) na wejściu:
trafienie zwraca odpowiedź natychmiast.

Tryb „Kwalifikacja" — HTMX: opis działalności → qualification.assess() → karty
werdyktów (B+R / IP Box) z podstawą prawną i listą braków.
"""

from __future__ import annotations

import json
import logging

import requests
from django.http import HttpResponseBadRequest, StreamingHttpResponse
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
        "url": _eli_gov_url(d.get("eli_id", "")),
    }


# ── Strona główna ─────────────────────────────────────────────────────────
def chat(request):
    ctx = {"corpus_note": "korpus: CIT · PIT · ORD"}
    # Pieczęć „stan prawny" z ostatniego udanego ingestu (degraduje, gdy brak DB/danych).
    try:
        akt = (
            Akt.objects.exclude(last_ingested_at=None)
            .order_by("-last_ingested_at")
            .first()
        )
        if akt:
            job = (
                IngestJob.objects.filter(akt=akt, status="success")
                .order_by("-finished_at")
                .first()
            )
            d = job.obowiazuje_od if job else None
            ctx["stan_prawny"] = d.strftime("%d.%m.%Y") if d else None
            ctx["seal_status"] = akt.eli_id or "ELI"
            ctx["seal_title"] = f"Stan prawny z tekstu jednolitego {akt.eli_id}"
    except Exception:  # noqa: BLE001 — brak bazy/danych nie może wywalić strony
        pass
    return render(request, "ulgi/chat.html", ctx)


# ── SSE helpers ───────────────────────────────────────────────────────────
def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _stream_ollama(query: str, docs: list[dict], model: str | None = None):
    """Strumień tokenów odpowiedzi z Ollamy (reużywa promptu i kontekstu z search)."""
    from config import DEFAULT_OLLAMA_MODEL, OLLAMA_URL
    from search import _SYSTEM, _build_context, _ollama_headers

    payload = {
        "model": model or DEFAULT_OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": f"Pytanie: {query}\n\nKontekst (podstawy prawne):\n{_build_context(docs)}",
            },
        ],
        "stream": True,
    }
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
    ulga = (request.POST.get("ulga") or "").strip() or None
    if not q:
        return HttpResponseBadRequest("puste pytanie")
    filters = {"ulga": ulga} if ulga else {}

    if not request.session.session_key:
        request.session.create()
    skey = request.session.session_key

    def gen():
        from search import retrieve

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

        # 2. Retrieval hybrydowy.
        try:
            docs = retrieve(q, **filters)
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

    try:
        out = services.qualify(opis, ulgi=ulgi)
    except Exception as e:  # noqa: BLE001
        return render(request, "ulgi/_qualification.html", {"error": f"Ocena niedostępna ({e})."})

    ocena = out.get("ocena", {})
    oceny = [
        {**o, "cls": VERDICT_CLS.get(o.get("werdykt", ""), "brak")}
        for o in ocena.get("oceny", [])
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
