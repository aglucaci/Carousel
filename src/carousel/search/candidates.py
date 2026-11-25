"""Stage A of the recommendation engine: candidate generation (CAROUSEL_PLAN.md §4).

Given a pool of papers (typically what the radar's source queries returned
plus papers already in the store for that window), produce the candidates the
ranker will score:

1. **Hard filters**, applied first and recorded with a reason per paper:
   retracted (an exclusion, never a score penalty), outside the date window,
   wrong publication type, or an excluded term in the title/abstract.
2. **Lexical** retrieval: BM25 over title + abstract, summed over the radar's
   terms (top ``lexical_k``).
3. **Semantic** retrieval: cosine similarity between each paper's embedding
   and the radar's interest profile (description + queries), top
   ``semantic_k``. Embeddings come from the store's cache.
4. **Source queries**: papers the connectors returned for this radar are
   candidates by definition.
5. **Citations** (``CANDIDATES_VERSION`` 0.2.0): papers in the window that
   cite one of the radar's seed, saved, or relevant papers
   (:mod:`carousel.search.citations`) are candidates too.

The union is returned with every feature computed for every candidate
(BM25 score, similarity, ranks, matched terms, how it was retrieved), ordered
by reciprocal-rank fusion so the order is deterministic. Ranking proper
(relevance, novelty, method match, MMR) is stage B-E, plan task 8.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from carousel.core.paper import CanonicalPaper, partial_date_bounds
from carousel.core.text import normalize_text
from carousel.search.embeddings import EmbedStats, Embedder, cosine, ensure_embeddings
from carousel.search.lexical import BM25Index, matched_terms, paper_text, phrase_in

if TYPE_CHECKING:  # pragma: no cover
    from carousel.core.config import RadarConfig
    from carousel.database.local import PaperStore

RRF_K = 60
CANDIDATES_VERSION = "0.2.0"


@dataclasses.dataclass(frozen=True)
class Candidate:
    paper: CanonicalPaper
    retrieved_by: Tuple[str, ...]          # subset of ("lexical", "semantic", "source_query", "citation")
    bm25: float
    lexical_rank: Optional[int]
    similarity: Optional[float]
    semantic_rank: Optional[int]
    matched_terms: Tuple[str, ...]
    fused: float

    @property
    def paper_id(self) -> str:
        return self.paper.paper_id

    def features(self) -> Dict[str, Any]:
        """JSON-ready features, for persisting with a run and explaining a recommendation."""
        return {
            "paper_id": self.paper_id,
            "retrieved_by": list(self.retrieved_by),
            "bm25": round(self.bm25, 6),
            "lexical_rank": self.lexical_rank,
            "similarity": None if self.similarity is None else round(self.similarity, 6),
            "semantic_rank": self.semantic_rank,
            "matched_terms": list(self.matched_terms),
            "fused": round(self.fused, 8),
            "sources": sorted({r.source.value for r in self.paper.sources}),
        }


@dataclasses.dataclass(frozen=True)
class CandidateSet:
    candidates: List[Candidate]
    filtered: Dict[str, List[str]]          # reason -> paper_ids
    stats: Dict[str, Any]

    def ids(self) -> List[str]:
        return [c.paper_id for c in self.candidates]


def profile_text(radar: "RadarConfig") -> str:
    """The radar's interest profile as text: name, description, research question, queries.

    Seed papers and feedback join the profile as vectors in task 9.
    """
    parts = [radar.radar_name, radar.description or "", radar.research_question or "", ". ".join(radar.queries)]
    return " ".join(x for x in parts if x).strip()


def _in_window(paper: CanonicalPaper, start: dt.date, end: dt.date) -> Optional[bool]:
    """Does any public date of the paper fall in [start, end]? None if it has no public date."""
    dates = [d for d in (paper.dates.first_online, paper.dates.published, paper.dates.preprint_posted) if d]
    if not dates:
        return None
    return any(lo <= end and hi >= start for lo, hi in (partial_date_bounds(d) for d in dates))


def _hard_filter(
    paper: CanonicalPaper,
    exclude: Sequence[str],
    window: Optional[Tuple[dt.date, dt.date]],
    publication_types: Optional[Set[str]],
    exclude_retracted: bool,
) -> Optional[str]:
    if exclude_retracted and paper.is_retracted:
        return "retracted"
    if window is not None:
        inside = _in_window(paper, *window)
        if inside is None:
            return "no_publication_date"
        if not inside:
            return "outside_date_window"
    if publication_types is not None and not ({t.lower() for t in paper.publication_types} & publication_types):
        return "publication_type"
    if exclude:
        text = normalize_text(paper_text(paper))
        for term in exclude:
            if phrase_in(term, text):
                return f"excluded_term:{term}"
    return None


def generate_candidates(
    pool: Iterable[CanonicalPaper],
    terms: Sequence[str],
    *,
    profile: Optional[str] = None,
    exclude: Sequence[str] = (),
    window: Optional[Tuple[dt.date, dt.date]] = None,
    publication_types: Optional[Sequence[str]] = None,
    exclude_retracted: bool = True,
    source_hits: Iterable[str] = (),
    citation_hits: Iterable[str] = (),
    store: Optional["PaperStore"] = None,
    embedder: Optional[Embedder] = None,
    lexical_k: int = 200,
    semantic_k: int = 200,
    min_similarity: Optional[float] = 0.0,
) -> CandidateSet:
    """Filter ``pool`` and retrieve candidates lexically, semantically, and from source queries.

    Semantic retrieval runs when both ``store`` (the embedding cache) and
    ``embedder`` are given and ``profile`` is non-empty. Papers pointing away
    from the profile (similarity below ``min_similarity``) are not semantic
    hits, though their similarity is still reported if they are retrieved
    another way.
    """
    papers: Dict[str, CanonicalPaper] = {}
    for p in pool:
        papers.setdefault(p.paper_id, p)
    types = {t.lower() for t in publication_types} if publication_types is not None else None

    filtered: Dict[str, List[str]] = {}
    eligible: List[CanonicalPaper] = []
    for p in sorted(papers.values(), key=lambda p: p.paper_id):
        reason = _hard_filter(p, exclude, window, types, exclude_retracted)
        if reason:
            filtered.setdefault(reason, []).append(p.paper_id)
        else:
            eligible.append(p)

    # Lexical
    bm25_all = dict(BM25Index.from_papers(eligible).search(terms)) if eligible and terms else {}
    lex_ranked = sorted(bm25_all.items(), key=lambda kv: (-kv[1], kv[0]))
    lex_rank = {pid: i + 1 for i, (pid, _) in enumerate(lex_ranked)}
    lexical = {pid for pid, _ in lex_ranked[:lexical_k]}

    # Semantic
    sims: Dict[str, float] = {}
    embed_stats: Optional[EmbedStats] = None
    if store is not None and embedder is not None and profile and eligible:
        vectors, embed_stats = ensure_embeddings(store, eligible, embedder)
        [pvec] = embedder.embed([profile])
        sims = {pid: cosine(v, pvec) for pid, v in vectors.items()}
    sem_ranked = sorted(sims.items(), key=lambda kv: (-kv[1], kv[0]))
    sem_rank = {pid: i + 1 for i, (pid, _) in enumerate(sem_ranked)}
    semantic = {pid for pid, s in sem_ranked[:semantic_k] if min_similarity is None or s >= min_similarity}

    eligible_ids = {p.paper_id for p in eligible}
    from_source = set(source_hits) & eligible_ids
    from_citations = set(citation_hits) & eligible_ids

    candidates: List[Candidate] = []
    for pid in sorted(lexical | semantic | from_source | from_citations):
        by = tuple(name for name, hit in (("lexical", pid in lexical), ("semantic", pid in semantic),
                                          ("source_query", pid in from_source), ("citation", pid in from_citations)) if hit)
        fused = sum(1.0 / (RRF_K + r) for r in (lex_rank.get(pid), sem_rank.get(pid)) if r is not None)
        candidates.append(Candidate(
            paper=papers[pid],
            retrieved_by=by,
            bm25=bm25_all.get(pid, 0.0),
            lexical_rank=lex_rank.get(pid),
            similarity=sims.get(pid),
            semantic_rank=sem_rank.get(pid),
            matched_terms=tuple(matched_terms(papers[pid], terms)),
            fused=fused,
        ))
    candidates.sort(key=lambda c: (-c.fused, c.paper_id))

    stats: Dict[str, Any] = {
        "candidates_version": CANDIDATES_VERSION,
        "pool": len(papers),
        "eligible": len(eligible),
        "filtered": {k: len(v) for k, v in sorted(filtered.items())},
        "lexical_hits": len(lexical),
        "semantic_hits": len(semantic),
        "source_query_hits": len(from_source),
        **({"citation_hits": len(from_citations)} if from_citations else {}),
        "candidates": len(candidates),
        "lexical_k": lexical_k,
        "semantic_k": semantic_k,
        "model_version": embed_stats.model_version if embed_stats else None,
        "embedded": embed_stats.embedded if embed_stats else 0,
        "embeddings_reused": embed_stats.reused if embed_stats else 0,
    }
    return CandidateSet(candidates=candidates, filtered=filtered, stats=stats)


def candidates_for_radar(
    radar: "RadarConfig",
    pool: Iterable[CanonicalPaper],
    *,
    as_of: dt.date,
    store: Optional["PaperStore"] = None,
    embedder: Optional[Embedder] = None,
    source_hits: Iterable[str] = (),
    **kw: Any,
) -> CandidateSet:
    """:func:`generate_candidates` with the radar's terms, exclusions, profile, and date window."""
    start = as_of - dt.timedelta(days=radar.filters.days - 1)
    return generate_candidates(
        pool, list(radar.queries), profile=profile_text(radar), exclude=list(radar.exclude),
        window=(start, as_of), publication_types=list(radar.publication_types) or None,
        store=store, embedder=embedder, source_hits=source_hits, **kw,
    )
