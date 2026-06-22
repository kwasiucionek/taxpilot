"""
TaxPilot — FastAPI backend (drop-in w slot uodo_rag).

Endpointy:
  GET  /health                 — health check
  GET  /api/taxonomy           — opcje filtrów (ulgi, akty, typy źródeł)
  POST /api/search             — wyszukiwanie hybrydowe z filtrami
  POST /api/answer/stream      — streaming odpowiedzi RAG (SSE)
  POST /api/qualify            — asystent kwalifikacji (B+R / IP Box)

Uruchomienie (jak uodo_rag):
  uvicorn api:app --host 127.0.0.1 --port 8503 --workers 1
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager

os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

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

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Rozgrzewka: załaduj embedder raz przy starcie (jak w uodo_rag).
    logger.info("Rozgrzewka embeddera...")
    try:
        from embedder import get_embedder

        get_embedder()
        logger.info("Embedder gotowy.")
    except Exception as e:  # noqa: BLE001
        logger.warning("Nie udało się rozgrzać embeddera: %s", e)
    yield


app = FastAPI(title="TaxPilot", description="RAG prawa podatkowego ulg", lifespan=lifespan)

_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────── MODELE ──────────────────────────────


class SearchRequest(BaseModel):
    query: str
    k: int = TOP_K
    akt: str | None = None
    ulga: str | None = None
    source_types: list[str] | None = None
    on_date: str | None = Field(default=None, description="stan prawny na dzień YYYY-MM-DD")


class AnswerRequest(SearchRequest):
    model: str | None = None


class QualifyRequest(BaseModel):
    opis: str
    ulgi: list[str] | None = None
    on_date: str | None = None
    model: str | None = None


# ─────────────────────────── ENDPOINTY ───────────────────────────


@app.get("/health")
async def health():
    return {"status": "ok", "service": "taxpilot"}


@app.get("/api/taxonomy")
async def taxonomy():
    return {
        "ulgi": [{"kod": k, "nazwa": v["name"]} for k, v in ULGI.items()],
        "akty": [{"kod": k, "nazwa": v["title"]} for k, v in ACTS.items()],
        "source_types": [
            SOURCE_USTAWA,
            SOURCE_OBJASNIENIA,
            SOURCE_INTERPRETACJA,
            SOURCE_ORZECZENIE,
        ],
    }


@app.post("/api/search")
async def search(req: SearchRequest):
    from search import retrieve

    try:
        docs = retrieve(
            req.query,
            k=req.k,
            akt=req.akt,
            ulga=req.ulga,
            source_types=req.source_types,
            on_date=req.on_date,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))
    return {"results": docs, "count": len(docs)}


@app.post("/api/answer/stream")
async def answer_stream(req: AnswerRequest):
    from search import _build_context, _ollama_headers, retrieve

    docs = retrieve(
        req.query,
        k=req.k,
        akt=req.akt,
        ulga=req.ulga,
        source_types=req.source_types,
        on_date=req.on_date,
    )

    async def event_generator():
        # Najpierw wysyłamy źródła, potem strumień tekstu.
        sources = [
            {"citation": d.get("citation"), "ulga": d.get("ulga"), "score": d.get("_score")}
            for d in docs
        ]
        yield f"event: sources\ndata: {json.dumps(sources, ensure_ascii=False)}\n\n"

        if not docs:
            yield 'event: done\ndata: {"answer": "Brak materiału w bazie."}\n\n'
            return

        from search import _SYSTEM

        payload = {
            "model": req.model or DEFAULT_OLLAMA_MODEL,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": f"Pytanie: {req.query}\n\nKontekst:\n{_build_context(docs)}",
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
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token = chunk.get("message", {}).get("content", "")
                if token:
                    yield f"data: {json.dumps({'token': token}, ensure_ascii=False)}\n\n"
                if chunk.get("done"):
                    yield "event: done\ndata: {}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/qualify")
async def qualify(req: QualifyRequest):
    from qualification import assess

    try:
        return assess(req.opis, ulgi=req.ulgi, on_date=req.on_date, model=req.model)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))
