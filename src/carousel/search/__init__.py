"""Search: lexical (BM25), semantic (cached embeddings), and candidate generation (plan task 7)."""

from carousel.search.candidates import Candidate, CandidateSet, candidates_for_radar, generate_candidates, profile_text
from carousel.search.embeddings import (
    Embedder,
    EmbedStats,
    HashingEmbedder,
    SentenceTransformerEmbedder,
    cosine,
    ensure_embeddings,
)
from carousel.search.lexical import BM25Index, matched_terms

__all__ = [
    "BM25Index",
    "Candidate",
    "CandidateSet",
    "EmbedStats",
    "Embedder",
    "HashingEmbedder",
    "SentenceTransformerEmbedder",
    "candidates_for_radar",
    "cosine",
    "ensure_embeddings",
    "generate_candidates",
    "matched_terms",
    "profile_text",
]
