"""Embeddings for semantic retrieval, cached per ``(paper, model_version)``.

An :class:`Embedder` turns texts into L2-normalized vectors and names itself
with a ``model_version``. :func:`ensure_embeddings` embeds only what the store
does not already hold:

- a paper is **never re-embedded under the same model_version** while its
  embedding text is unchanged;
- if the paper's text changes (a source corrected its abstract, or a merge
  brought in a better one), it is embedded again, once;
- a different ``model_version`` gets its own embeddings side by side, so a run
  can always say which model produced its similarities.

Two embedders ship:

``SentenceTransformerEmbedder``
    The real semantic model (``pip install "carousel-research[semantic]"``).
    Default ``sentence-transformers/all-MiniLM-L6-v2``; pass ``revision`` to
    pin the exact weights, which then becomes part of ``model_version``.
``HashingEmbedder``
    Dependency-free feature hashing of words and word pairs. It is *not*
    semantic (it can't know "tumour" ~ "neoplasm"), but it is deterministic,
    so tests, CI, and installs without the extra still run the full pipeline.
"""

from __future__ import annotations

import abc
import array
import dataclasses
import datetime as dt
import hashlib
import math
import sys
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Sequence, Tuple

from carousel.core.paper import CanonicalPaper
from carousel.core.text import search_tokens
from carousel.search.lexical import paper_text

if TYPE_CHECKING:  # pragma: no cover
    from carousel.database.local import PaperStore

EMBED_TEXT_RECIPE = "title+abstract/1"
"""Version of how a paper's embedding text is built; part of the cache's text hash."""

Vector = List[float]


class Embedder(abc.ABC):
    model_version: str
    dim: int

    @abc.abstractmethod
    def embed(self, texts: Sequence[str]) -> List[Vector]:
        """One L2-normalized vector per text."""


def _normalize(v: Sequence[float]) -> Vector:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else list(v)


class HashingEmbedder(Embedder):
    """Deterministic bag of words and word pairs, hashed into ``dim`` signed buckets."""

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim
        self.model_version = f"carousel/hashing-v1-d{dim}"

    def _bucket(self, feature: str) -> Tuple[int, float]:
        h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "little")
        return h % self.dim, (1.0 if (h >> 63) & 1 else -1.0)

    def embed(self, texts: Sequence[str]) -> List[Vector]:
        out = []
        for text in texts:
            toks = search_tokens(text)
            counts: Dict[str, int] = {}
            for f in toks + [f"{a} {b}" for a, b in zip(toks, toks[1:])]:
                counts[f] = counts.get(f, 0) + 1
            v = [0.0] * self.dim
            for f, c in counts.items():
                i, sign = self._bucket(f)
                v[i] += sign * (1.0 + math.log(c))
            out.append(_normalize(v))
        return out


class SentenceTransformerEmbedder(Embedder):
    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2", revision: Optional[str] = None,
                 batch_size: int = 32, device: Optional[str] = None) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - depends on the optional extra
            raise ImportError(
                'Semantic search needs the optional extra: pip install "carousel-research[semantic]" '
                "(or use HashingEmbedder for a dependency-free, non-semantic fallback)"
            ) from exc
        self.model = SentenceTransformer(model_name, revision=revision, device=device)
        self.batch_size = batch_size
        # Renamed in newer sentence-transformers; support both spellings.
        get_dim = getattr(self.model, "get_embedding_dimension", None) or self.model.get_sentence_embedding_dimension
        self.dim = int(get_dim())
        self.model_version = model_name + (f"@{revision}" if revision else "")

    def embed(self, texts: Sequence[str]) -> List[Vector]:  # pragma: no cover - needs model weights
        vecs = self.model.encode(list(texts), batch_size=self.batch_size, normalize_embeddings=True,
                                 show_progress_bar=False, convert_to_numpy=True)
        return [[float(x) for x in v] for v in vecs]


# -------------------------
# Vector helpers
# -------------------------

def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two L2-normalized vectors."""
    return sum(x * y for x, y in zip(a, b))


def pack(v: Sequence[float]) -> bytes:
    arr = array.array("f", v)
    if sys.byteorder == "big":  # pragma: no cover
        arr.byteswap()
    return arr.tobytes()


def unpack(blob: bytes) -> Vector:
    arr = array.array("f")
    arr.frombytes(blob)
    if sys.byteorder == "big":  # pragma: no cover
        arr.byteswap()
    return arr.tolist()


def embedding_text(paper: CanonicalPaper) -> str:
    return paper_text(paper)


def text_hash(text: str) -> str:
    return hashlib.sha256(f"{EMBED_TEXT_RECIPE}\n{text}".encode("utf-8")).hexdigest()


# -------------------------
# Cache-aware embedding
# -------------------------

@dataclasses.dataclass(frozen=True)
class EmbedStats:
    model_version: str
    embedded: int
    reused: int
    skipped_no_text: int


def ensure_embeddings(
    store: "PaperStore",
    papers: Iterable[CanonicalPaper],
    embedder: Embedder,
    batch_size: int = 64,
    at: Optional[dt.datetime] = None,
) -> Tuple[Dict[str, Vector], EmbedStats]:
    """Vectors for ``papers`` under ``embedder``, embedding only what the store doesn't already hold."""
    papers = list(papers)
    texts = {p.paper_id: embedding_text(p) for p in papers}
    hashes = {pid: text_hash(t) for pid, t in texts.items() if t}
    cached = store.get_embeddings(list(hashes), embedder.model_version)
    vectors: Dict[str, Vector] = {}
    todo: List[str] = []
    for pid, h in hashes.items():
        hit = cached.get(pid)
        if hit is not None and hit[0] == h:
            vectors[pid] = hit[1]
        else:
            todo.append(pid)
    for i in range(0, len(todo), batch_size):
        batch = todo[i : i + batch_size]
        vecs = embedder.embed([texts[pid] for pid in batch])
        if len(vecs) != len(batch) or any(len(v) != embedder.dim for v in vecs):
            raise ValueError(f"{embedder.model_version} returned vectors of the wrong number or size")
        store.put_embeddings([(pid, hashes[pid], v) for pid, v in zip(batch, vecs)], embedder.model_version, at)
        vectors.update(zip(batch, vecs))
    stats = EmbedStats(embedder.model_version, embedded=len(todo), reused=len(hashes) - len(todo),
                       skipped_no_text=len(papers) - len(hashes))
    return vectors, stats
