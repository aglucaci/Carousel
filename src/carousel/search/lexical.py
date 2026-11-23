"""Lexical retrieval: Okapi BM25 over titles and abstracts (pure Python).

Each paper is indexed as its title twice plus its abstract, a cheap
approximation of a field-weighted BM25 that lets a title match count for more
than an abstract match. Tokens come from :func:`carousel.core.text.search_tokens`
(normalized, stopwords removed, lightly stemmed), so "SARS-CoV-2 Variants"
matches "sars cov 2 variant".

The index is built per scan over the candidate pool; at Phase 1A sizes
(thousands of papers) that takes milliseconds. Phase 1B moves this to
Postgres full-text search.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from carousel.core.paper import CanonicalPaper
from carousel.core.text import normalize_text, search_tokens

K1 = 1.5
B = 0.75
TITLE_REPEAT = 2


def paper_text(paper: CanonicalPaper, title_repeat: int = 1) -> str:
    """The text a paper is searched and embedded by: title (optionally repeated) and abstract."""
    title = paper.title or ""
    return " ".join([title] * title_repeat + [paper.abstract or ""]).strip()


def phrase_in(phrase: str, text_norm: str) -> bool:
    """Whole-word phrase match on normalized text (with light stemming on both sides)."""
    p = " ".join(search_tokens(phrase)) or normalize_text(phrase)
    t = " ".join(search_tokens(text_norm))
    return bool(p) and f" {p} " in f" {t} "


class BM25Index:
    def __init__(self, docs: Iterable[Tuple[str, str]], k1: float = K1, b: float = B) -> None:
        """``docs``: (doc_id, text) pairs."""
        self.k1, self.b = k1, b
        self.tf: Dict[str, Counter] = {}
        self.length: Dict[str, int] = {}
        df: Counter = Counter()
        for doc_id, text in docs:
            toks = search_tokens(text)
            self.tf[doc_id] = Counter(toks)
            self.length[doc_id] = len(toks)
            df.update(set(toks))
        self.n = len(self.tf)
        self.avgdl = (sum(self.length.values()) / self.n) if self.n else 0.0
        # BM25+ style floor keeps IDF positive for terms in more than half the documents.
        self.idf = {t: math.log(1 + (self.n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    @classmethod
    def from_papers(cls, papers: Iterable[CanonicalPaper]) -> "BM25Index":
        return cls((p.paper_id, paper_text(p, TITLE_REPEAT)) for p in papers)

    def score(self, query: str) -> Dict[str, float]:
        """BM25 score of every document with at least one query token (others are absent, i.e. 0)."""
        q = set(search_tokens(query))
        out: Dict[str, float] = {}
        if not self.n:
            return out
        for doc_id, tf in self.tf.items():
            dl = self.length[doc_id]
            s = 0.0
            for t in q:
                f = tf.get(t)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1)))
            if s > 0:
                out[doc_id] = s
        return out

    def search(self, queries: Sequence[str], top_k: Optional[int] = None) -> List[Tuple[str, float]]:
        """Score against each query (radar term) and sum, so a paper matching several terms ranks higher.

        Ties break on doc_id, so results are deterministic.
        """
        total: Dict[str, float] = {}
        for q in queries:
            for doc_id, s in self.score(q).items():
                total[doc_id] = total.get(doc_id, 0.0) + s
        ranked = sorted(total.items(), key=lambda kv: (-kv[1], kv[0]))
        return ranked[:top_k] if top_k else ranked


def matched_terms(paper: CanonicalPaper, terms: Sequence[str]) -> List[str]:
    """Which radar terms appear (as whole phrases) in the title or abstract, for explanations."""
    text = normalize_text(paper_text(paper))
    return [t for t in terms if phrase_in(t, text)]
