"""
Semantyczny cache odpowiedzi na Redisie.

Dwie warstwy (jak w LexSearchu, ale bez Qdranta):
  - exact:    hash znormalizowanego pytania + filtry/model → natychmiastowy hit
  - semantic: kosinus wektora pytania vs zapamiętane wektory → hit gdy podobne

Wektory liczy ten sam model co wyszukiwanie (embed_query), więc cache jest
spójny z retrievalem. Trwałość w Redisie; szybkie porównanie semantyczne
brute-force w pamięci procesu (zakładamy gunicorn --workers 1; przy wielu
workerach warstwa exact i tak jest współdzielona przez Redis, a semantyczna
działa best-effort per worker).

Próg semantyczny celowo wysoki (0.95) — w prawie podatkowym lepiej policzyć
od nowa niż podać podobną-ale-nie-tę odpowiedź.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import cast

import numpy as np
import redis

logger = logging.getLogger(__name__)

CACHE_REDIS_URL = os.getenv("CACHE_REDIS_URL", "redis://localhost:6379/2")
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", str(7 * 24 * 3600)))
SEM_THRESHOLD = float(os.getenv("CACHE_SEMANTIC_THRESHOLD", "0.95"))
MAX_ENTRIES = int(os.getenv("CACHE_MAX_ENTRIES", "1000"))
MIN_ANSWER_LEN = 30  # nie cache'uj śmieciowych/pustych odpowiedzi

_PREFIX = "taxpilot:cache"
_EXACT = f"{_PREFIX}:exact:"  # + hash → JSON {response, sources}
_META = f"{_PREFIX}:meta:"  # + sid  → JSON {query, response, sources, sig}
_VEC = f"{_PREFIX}:vec:"  # + sid  → bytes float32 (wektor znormalizowany)
_ORDER = f"{_PREFIX}:order"  # zset sid → ts (kolejność wstawiania, do eksmisji)

_STOP = {
    "o",
    "w",
    "z",
    "i",
    "a",
    "na",
    "do",
    "po",
    "dla",
    "od",
    "ze",
    "czy",
    "jak",
    "co",
    "to",
    "się",
    "nie",
    "jest",
    "być",
    "tego",
    "tym",
    "ten",
    "ta",
    "te",
    "ich",
    "jej",
    "jego",
    "jakie",
    "jaki",
    "jaka",
    "które",
    "który",
    "która",
}


def _normalize(query: str) -> str:
    text = re.sub(r"[^\w\s]", " ", query.lower().strip())
    words = sorted(w for w in text.split() if w not in _STOP and len(w) > 1)
    return " ".join(words)


def _sig(filters: dict, model: str | None) -> str:
    payload = {"f": filters or {}, "m": model or ""}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def _exact_key(query: str, filters: dict, model: str | None) -> str:
    raw = f"{_normalize(query)}|{_sig(filters, model)}"
    return _EXACT + hashlib.sha256(raw.encode()).hexdigest()


class SemanticResponseCache:
    def __init__(self) -> None:
        self._r: redis.Redis | None = None
        self._loaded = False
        self._ids: list[str] = []
        self._sigs: list[str] = []
        self._vecs: list[np.ndarray] = []
        self._mat: np.ndarray | None = None
        self._dirty = True

    # ---- Redis (leniwe połączenie) ----
    @property
    def r(self) -> redis.Redis:
        if self._r is None:
            self._r = redis.Redis.from_url(CACHE_REDIS_URL, decode_responses=False)
        return self._r

    # ---- Embedding znormalizowany (kosinus = dot) ----
    @staticmethod
    def _embed(text: str) -> np.ndarray:
        from embedder import embed_query

        v = np.asarray(embed_query(text), dtype=np.float32)
        n = float(np.linalg.norm(v))
        return v / n if n else v

    def _load(self) -> None:
        """Wczytuje wektory z Redisa do pamięci procesu (raz)."""
        self._ids, self._sigs, self._vecs = [], [], []
        try:
            # decode_responses=False → Redis zwraca bajty (stub typuje unię szerzej).
            ids = [b.decode() for b in cast("list[bytes]", self.r.zrange(_ORDER, 0, -1))]
            if ids:
                pipe = self.r.pipeline()
                for sid in ids:
                    pipe.get(_VEC + sid)
                    pipe.get(_META + sid)
                res = pipe.execute()
                for i, sid in enumerate(ids):
                    vec_b, meta_b = res[2 * i], res[2 * i + 1]
                    if not vec_b or not meta_b:
                        # wpis wygasł (TTL) — posprzątaj osierocony zset
                        self.r.zrem(_ORDER, sid)
                        continue
                    self._ids.append(sid)
                    self._sigs.append(json.loads(meta_b).get("sig", ""))
                    self._vecs.append(np.frombuffer(vec_b, dtype=np.float32))
        except Exception:  # noqa: BLE001 — brak/awaria Redisa nie może wywalić odpowiedzi
            logger.debug("Cache: nie udało się wczytać wektorów z Redisa.", exc_info=True)
        self._dirty = True
        self._loaded = True

    def _matrix(self) -> np.ndarray | None:
        if self._dirty:
            self._mat = np.vstack(self._vecs) if self._vecs else None
            self._dirty = False
        return self._mat

    # ---- API ----
    def get(self, query: str, filters: dict, model: str | None = None) -> tuple | None:
        """Zwraca (response, sources, warstwa) albo None."""
        sig = _sig(filters, model)

        # 1) exact
        try:
            raw = self.r.get(_exact_key(query, filters, model))
            if raw:
                d = json.loads(raw)
                return d["response"], d["sources"], "exact"
        except Exception:  # noqa: BLE001
            logger.debug("Cache: odczyt warstwy exact nieudany.", exc_info=True)

        # 2) semantyczny
        if not self._loaded:
            self._load()
        mat = self._matrix()
        if mat is None:
            return None
        try:
            qv = self._embed(query)
        except Exception:  # noqa: BLE001
            logger.debug("Cache: embedding zapytania nieudany (get).", exc_info=True)
            return None
        sims = mat @ qv
        idx = int(np.argmax(sims))
        if sims[idx] >= SEM_THRESHOLD and self._sigs[idx] == sig:
            sid = self._ids[idx]
            try:
                meta_b = self.r.get(_META + sid)
                if meta_b:
                    d = json.loads(meta_b)
                    # awansuj do warstwy exact, by kolejny taki sam był natychmiastowy
                    self.r.setex(
                        _exact_key(query, filters, model),
                        CACHE_TTL,
                        json.dumps({"response": d["response"], "sources": d["sources"]}),
                    )
                    return d["response"], d["sources"], f"semantic:{sims[idx]:.3f}"
            except Exception:  # noqa: BLE001
                logger.debug("Cache: odczyt warstwy semantycznej nieudany.", exc_info=True)
        return None

    def set(
        self, query: str, filters: dict, model: str | None, response: str, sources: list[dict]
    ) -> None:
        if not response or len(response) < MIN_ANSWER_LEN:
            return
        sig = _sig(filters, model)
        sid = hashlib.sha1(f"{query}|{sig}".encode()).hexdigest()[:16]

        try:
            qv = self._embed(query)
        except Exception:  # noqa: BLE001
            logger.debug("Cache: embedding zapytania nieudany (set).", exc_info=True)
            return

        try:
            self.r.setex(
                _exact_key(query, filters, model),
                CACHE_TTL,
                json.dumps({"response": response, "sources": sources}),
            )
            self.r.setex(
                _META + sid,
                CACHE_TTL,
                json.dumps({"query": query, "response": response, "sources": sources, "sig": sig}),
            )
            self.r.setex(_VEC + sid, CACHE_TTL, qv.tobytes())
            self.r.zadd(_ORDER, {sid: time.time()})
        except Exception:  # noqa: BLE001
            logger.debug("Cache: zapis do Redisa nieudany.", exc_info=True)
            return

        # pamięć procesu
        if not self._loaded:
            self._load()
        else:
            self._ids.append(sid)
            self._sigs.append(sig)
            self._vecs.append(qv)
            self._dirty = True

        self._evict()

    def _evict(self) -> None:
        """Trzyma rozmiar <= MAX_ENTRIES, usuwając najstarsze."""
        try:
            over = self.r.zcard(_ORDER) - MAX_ENTRIES
            if over <= 0:
                return
            oldest = [b.decode() for b in cast("list[bytes]", self.r.zrange(_ORDER, 0, over - 1))]
            pipe = self.r.pipeline()
            for sid in oldest:
                pipe.delete(_META + sid, _VEC + sid)
                pipe.zrem(_ORDER, sid)
            pipe.execute()
        except Exception:  # noqa: BLE001
            logger.debug("Cache: eksmisja najstarszych wpisów nieudana.", exc_info=True)
        # najprościej: wymuś przeładowanie indeksu w pamięci przy następnym get
        self._loaded = False

    # ---- pomocnicze (debug / admin) ----
    def stats(self) -> dict:
        try:
            return {
                "entries": self.r.zcard(_ORDER),
                "in_memory": len(self._ids),
                "threshold": SEM_THRESHOLD,
                "max": MAX_ENTRIES,
            }
        except Exception:  # noqa: BLE001
            logger.debug("Cache: odczyt statystyk nieudany.", exc_info=True)
            return {"entries": 0, "in_memory": len(self._ids)}

    def clear(self) -> int:
        n = 0
        try:
            keys = list(self.r.scan_iter(match=f"{_PREFIX}:*"))
            if keys:
                n = self.r.delete(*keys)
        except Exception:  # noqa: BLE001
            logger.debug("Cache: czyszczenie nieudane.", exc_info=True)
        self._ids, self._sigs, self._vecs, self._mat = [], [], [], None
        self._loaded, self._dirty = False, True
        return n


_CACHE: SemanticResponseCache | None = None


def get_cache() -> SemanticResponseCache:
    global _CACHE
    if _CACHE is None:
        _CACHE = SemanticResponseCache()
    return _CACHE
