"""
embedder.py — model embeddingowy sdadas/stella-pl-retrieval-mini-8k.

Dokumenty embedowane BEZ prefiksu, zapytania Z prefiksem instrukcji
(wymóg modelu). Patch xformers→PyTorch dla CPU przeniesiony z uodo_rag
(stella-mini ignoruje attn_implementation="eager").
"""

from __future__ import annotations

from config import EMBED_BATCH_SIZE, EMBED_MAX_SEQ, EMBED_MODEL

_QUERY_PREFIX = (
    "Instruct: Given a web search query, retrieve relevant passages "
    "that answer the query.\nQuery: "
)

_loaded_embedder = None


def _disable_xformers_cpu(model) -> int:
    """Wyłącza xformers w warstwach uwagi przy pracy na CPU.

    stella-mini (oparta na stella_en_400M_v5) wywołuje bezpośrednio
    xformers.memory_efficient_attention (wymaga CUDA). Trzy kroki:
    1) flaga use_memory_efficient_attention=False na warstwach,
    2) te same flagi + unpad_inputs=False w config modelu,
    3) patch get_extended_attention_mask (transformers ≥ 5.0 usunął param
       'device', a custom modeling.py go przekazuje).
    """
    import inspect

    count = 0
    patched_classes: set = set()

    for module in model.modules():
        if getattr(module, "use_memory_efficient_attention", False):
            module.use_memory_efficient_attention = False
            count += 1
        cfg = getattr(module, "config", None)
        if cfg is not None:
            if getattr(cfg, "use_memory_efficient_attention", False):
                cfg.use_memory_efficient_attention = False
            if getattr(cfg, "unpad_inputs", False):
                cfg.unpad_inputs = False
        cls = type(module)
        if cls not in patched_classes and hasattr(cls, "get_extended_attention_mask"):
            orig = cls.get_extended_attention_mask
            if "device" not in inspect.signature(orig).parameters:

                def _make_wrapper(original):
                    def _patched(self, attention_mask, input_shape, device=None, **kwargs):
                        return original(self, attention_mask, input_shape, **kwargs)

                    return _patched

                cls.get_extended_attention_mask = _make_wrapper(orig)
                patched_classes.add(cls)

    return count


def get_embedder():
    global _loaded_embedder
    if _loaded_embedder is not None:
        return _loaded_embedder

    import torch
    from sentence_transformers import SentenceTransformer

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Ładowanie embeddera: {EMBED_MODEL} (device={device})")
    model = SentenceTransformer(EMBED_MODEL, trust_remote_code=True, device=device)

    # Przycinamy długość sekwencji — kluczowe na CPU bez xformers (atencja S×S).
    # Chunki (ustęp/artykuł, ~1200 znaków) mieszczą się; tnie tylko skrajnie długie.
    model.max_seq_length = EMBED_MAX_SEQ

    if device == "cpu":
        n = _disable_xformers_cpu(model)
        if n:
            print(f"  CPU: wyłączono xformers w {n} warstwach uwagi.")
    else:
        try:
            model = model.half()
        except Exception:
            pass

    _loaded_embedder = model
    return _loaded_embedder


def embed_query(text: str) -> list[float]:
    return (
        get_embedder()
        .encode(_QUERY_PREFIX + text, normalize_embeddings=True)
        .tolist()
    )


def embed_documents(texts: list[str], batch_size: int = EMBED_BATCH_SIZE) -> list[list[float]]:
    """Dokumenty — bez prefiksu, w batchach."""
    embedder = get_embedder()
    vecs: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        out = embedder.encode(batch, normalize_embeddings=True, show_progress_bar=False)
        vecs.extend(out.tolist())
        print(f"  Embeddingi: {min(i + batch_size, len(texts))}/{len(texts)}", end="\r")
    print()
    return vecs
