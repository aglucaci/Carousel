"""Stages B-E of the recommendation engine (CAROUSEL_PLAN.md §4), ``ranking_version`` 0.2.0.

Input: a :class:`~carousel.search.candidates.CandidateSet` and the radar.
Output: a :class:`Ranking` whose every item carries the features that put it
where it is.

Signals (each in [0, 1]):

``relevance``
    ``R = α·sim(e_p, e_u) + β·BM25_norm``. ``e_u`` is the radar profile
    vector (name, description, research question, queries) moved toward seed
    papers and saved/relevant papers and away from irrelevant ones
    (:func:`rocchio`). ``BM25_norm`` is BM25
    divided by the run's maximum. Without embeddings, ``R = BM25_norm``.
    With citation data for the radar's anchors (seeds, saved and relevant
    papers; :mod:`carousel.search.citations`), the graph term joins it:
    ``R = (R₀ + γ·g) / (1 + γ)``, where ``g`` is 1 for a direct citation link to
    an anchor and up to 0.5 for shared references. Without that data ``R = R₀``.
``novelty``
    ``N = 1 − mean cosine similarity to the k nearest papers in H_t``, where
    ``H_t`` holds only papers CAROUSEL observed **strictly before** the
    candidate's own ``first_seen``. :func:`check_no_leakage` enforces this
    on every computation. ``N`` is then turned into a percentile within the
    candidate's publication kind (article / review / preprint / notice) when
    that group is large enough. Field normalization arrives with topic
    clusters (Phase 3). Novelty is **credited in proportion to relevance**
    (``novelty_gate`` = R / the run's highest R), so an off-topic paper can't
    rank on novelty alone.
``method_match``
    Share of the radar's method terms (``methods_profile``, or the
    ``signal_profile.methods`` terms) found in the title/abstract, saturating
    at three matches.

Version history: ``0.1.0`` relevance, novelty, method match, MMR, modes;
``0.2.0`` adds the graph term (task 32) and is otherwise identical: a radar
without anchors with citation data ranks exactly as under 0.1.0.

Momentum, discovery, and evidence are reported inactive with a reason, and
the remaining weights are rescaled. Integrity problems are flags (retracted
papers are already excluded upstream; ``corrected`` and
``expression_of_concern`` come from update notices, e.g. Crossref), never
score terms.

Selection: ``S = Σ w_k · signal_k`` (novelty times its gate) with the
radar's weights times the mode's multipliers (:data:`MODES`), then maximal
marginal relevance, ``λ·S − (1−λ)·max_sim(p, selected)``, with λ from the
mode. MMR runs first over papers with at least :data:`MIN_SELECTION_GATE` of
the top relevance; the rest follow only if ``top_n`` isn't filled.

Everything is a pure function of its inputs, sorted by ID before use, with
fixed tie-breaks, so the same stored inputs give the same ranking.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Sequence, Tuple

from carousel.core.paper import CORRECTING_UPDATES, CanonicalPaper
from carousel.core.text import search_tokens
from carousel.feedback import Feedback
from carousel.ingestion.dedup import is_notice, is_preprint
from carousel.search.candidates import Candidate, CandidateSet, profile_text
from carousel.search.embeddings import Embedder, Vector, _normalize, cosine, ensure_embeddings
from carousel.search.lexical import matched_terms, paper_text

if TYPE_CHECKING:  # pragma: no cover
    from carousel.core.config import RadarConfig
    from carousel.database.local import PaperStore

RANKING_VERSION = "0.2.0"

MODES: Dict[str, Dict[str, Any]] = {
    "focused": {"lambda": 0.85, "multipliers": {"relevance": 1.5, "novelty": 0.5, "method_match": 1.0}},
    "balanced": {"lambda": 0.70, "multipliers": {"relevance": 1.0, "novelty": 1.0, "method_match": 1.0}},
    "exploratory": {"lambda": 0.50, "multipliers": {"relevance": 0.75, "novelty": 1.75, "method_match": 1.25}},
}
"""Mode presets: MMR λ (higher = less diversity pressure) and weight multipliers."""

ALPHA = 0.6  # relevance: embedding similarity
BETA = 0.4   # relevance: BM25
GAMMA = 0.25  # relevance: citation graph term, when active
K_NOVELTY = 5
MIN_HISTORY = 3
MIN_GROUP_FOR_PERCENTILE = 5
METHOD_SATURATION = 3
NEUTRAL_NOVELTY = 0.5
MIN_SELECTION_GATE = 0.25
"""Papers below this share of the run's top relevance are selected only after all others (flag ``low_relevance``)."""

INACTIVE_REASONS = {
    "momentum": "needs run history and trend models (Phase 3)",
    "discovery": "needs Method Transfer (Phase 4, task 35)",
    "evidence": "needs evidence-grounded extraction (Phase 2)",
}

PHASE1_SIGNALS = ("relevance", "novelty", "method_match")


EXCLUDED_BY_FEEDBACK = {"save": "saved", "dismiss": "dismissed", "relevant": "marked_relevant",
                        "irrelevant": "marked_irrelevant"}
ROCCHIO = (1.0, 0.75, 0.25)
"""Profile = 1.0·radar text + 0.75·mean(seeds, saved, relevant) − 0.25·mean(irrelevant)."""


def rocchio(v_profile: Vector, positive: Sequence[Vector], negative: Sequence[Vector]) -> Vector:
    a, b, c = ROCCHIO
    out = [a * x for x in v_profile]
    for vecs, w in ((positive, b), (negative, -c)):
        if vecs:
            mean = [sum(col) / len(vecs) for col in zip(*vecs)]
            out = [o + w * m for o, m in zip(out, mean)]
    return _normalize(out)


class LeakageError(AssertionError):
    """H_t contained a paper observed at or after the candidate's observation time."""


@dataclasses.dataclass(frozen=True)
class HistoryItem:
    paper_id: str
    first_seen: dt.datetime
    vector: Vector


def _history_before(history: Sequence[HistoryItem], t: dt.datetime) -> List[HistoryItem]:
    """H_t: papers first observed strictly before ``t``."""
    return [h for h in history if h.first_seen < t]


def check_no_leakage(h_t: Iterable[HistoryItem], t: dt.datetime) -> None:
    for h in h_t:
        if h.first_seen >= t:
            raise LeakageError(
                f"temporal leakage: {h.paper_id} (first seen {h.first_seen.isoformat()}) is in H_t for t={t.isoformat()}"
            )


def novelty_raw(vec: Vector, t: dt.datetime, history: Sequence[HistoryItem], exclude_id: str,
                k: int = K_NOVELTY) -> Tuple[Optional[float], List[str], int]:
    """(1 − mean kNN similarity to H_t, the neighbours' IDs, |H_t|); None when H_t is too small."""
    h_t = _history_before(history, t)
    check_no_leakage(h_t, t)
    h_t = [h for h in h_t if h.paper_id != exclude_id]
    if len(h_t) < MIN_HISTORY:
        return None, [], len(h_t)
    sims = sorted(((cosine(vec, h.vector), h.paper_id) for h in h_t), key=lambda x: (-x[0], x[1]))[:k]
    return 1.0 - sum(s for s, _ in sims) / len(sims), [pid for _, pid in sims], len(h_t)


def publication_kind(paper: CanonicalPaper) -> str:
    if is_notice(paper):
        return "notice"
    if is_preprint(paper):
        return "preprint"
    if any("review" in t.lower() for t in paper.publication_types):
        return "review"
    return "article"


def _percentiles(values: Dict[str, float]) -> Dict[str, float]:
    """Percentile in [0, 1] of each value within the group (ties share the average rank)."""
    n = len(values)
    vals = sorted(values.values())
    out = {}
    for pid, v in values.items():
        less = sum(1 for x in vals if x < v)
        equal = sum(1 for x in vals if x == v)
        out[pid] = (less + 0.5 * (equal - 1)) / (n - 1) if n > 1 else 0.5
    return out


def _clip01(x: float) -> float:
    return min(1.0, max(0.0, x))


def _r(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 6)


# -------------------------
# Output
# -------------------------

@dataclasses.dataclass(frozen=True)
class RankedPaper:
    rank: int
    paper_id: str
    title: Optional[str]
    score: float
    mmr: float
    redundancy: float
    components: Dict[str, Optional[float]]
    details: Dict[str, Any]
    flags: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rank": self.rank, "paper_id": self.paper_id, "title": self.title, "score": _r(self.score),
            "mmr": _r(self.mmr), "redundancy": _r(self.redundancy),
            "components": {k: _r(v) for k, v in self.components.items()},
            "details": self.details, "flags": list(self.flags),
        }


@dataclasses.dataclass(frozen=True)
class Ranking:
    ranking_version: str
    mode: str
    mmr_lambda: float
    weights: Dict[str, float]
    active_signals: Tuple[str, ...]
    inactive_signals: Dict[str, str]
    model_version: Optional[str]
    items: List[RankedPaper]
    stats: Dict[str, Any]

    def ids(self) -> List[str]:
        return [i.paper_id for i in self.items]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ranking_version": self.ranking_version, "mode": self.mode, "mmr_lambda": self.mmr_lambda,
            "weights": {k: _r(v) for k, v in self.weights.items()}, "active_signals": list(self.active_signals),
            "inactive_signals": self.inactive_signals, "model_version": self.model_version,
            "items": [i.to_dict() for i in self.items], "stats": self.stats,
        }


# -------------------------
# Ranking
# -------------------------

def effective_weights(radar: "RadarConfig", mode: str, active: Sequence[str]) -> Dict[str, float]:
    base = radar.ranking.as_dict()
    mult = MODES[mode]["multipliers"]
    raw = {k: base[k] * mult.get(k, 1.0) for k in active}
    total = sum(raw.values())
    if total <= 0:
        raise ValueError(f"every active signal ({', '.join(active)}) has weight 0 in this radar's `ranking:`")
    return {k: raw[k] / total for k in active}


def rank(
    candidates: CandidateSet,
    radar: "RadarConfig",
    *,
    store: Optional["PaperStore"] = None,
    embedder: Optional[Embedder] = None,
    history: Optional[Iterable[CanonicalPaper]] = None,
    top_n: int = 25,
    mode: Optional[str] = None,
    feedback: Optional[Feedback] = None,
    graph: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Ranking:
    """Score and select. ``history`` defaults to every paper in ``store``.

    Papers with feedback (saved, dismissed, judged) and seed papers are not
    recommended; ``save``/``relevant`` and seeds pull the profile toward
    them and ``irrelevant`` pushes it away.

    ``graph`` (from :func:`carousel.search.citations.graph_features`) adds the
    graph term to relevance; ``None`` leaves relevance as in 0.1.0.
    """
    mode = mode or radar.mode.value
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {', '.join(MODES)}")
    lam = MODES[mode]["lambda"]
    cands: List[Candidate] = sorted(candidates.candidates, key=lambda c: c.paper_id)
    papers = {c.paper_id: c.paper for c in cands}

    # ---- seeds and feedback ----
    feedback = feedback or Feedback.empty()
    seeds: Dict[str, CanonicalPaper] = {}
    if store is not None:
        for doi in radar.seed_papers:
            p = store.find("doi", doi)
            if p is not None:
                seeds[doi] = p
    seed_ids = {p.paper_id for p in seeds.values()}
    profile_info: Dict[str, Any] = {
        "seeds_used": sorted(seeds),
        "seeds_missing": sorted(set(radar.seed_papers) - set(seeds)),
        "feedback_positive": len(feedback.positive),
        "feedback_negative": len(feedback.negative),
        "feedback_unresolved": list(feedback.unresolved),
    }

    # ---- vectors: candidates, history, profile (Rocchio over seeds and feedback) ----
    vectors: Dict[str, Vector] = {}
    hist_items: List[HistoryItem] = []
    e_u: Optional[Vector] = None
    e_base: Optional[Vector] = None  # the profile before feedback (radar text and seeds), to measure feedback's effect
    fb_papers: Dict[str, CanonicalPaper] = {}
    if store is not None and embedder is not None:
        hist_papers = {p.paper_id: p for p in (history if history is not None else store.papers())}
        extra = {p.paper_id: p for p in seeds.values()}
        for pid in feedback.positive + feedback.negative:
            fp = store.get(pid)
            if fp is not None:
                extra[pid] = fp
                fb_papers[pid] = fp
        everything = {**extra, **hist_papers, **papers}
        vectors, _ = ensure_embeddings(store, [everything[k] for k in sorted(everything)], embedder)
        hist_items = [HistoryItem(pid, hist_papers[pid].first_seen, vectors[pid])
                      for pid in sorted(hist_papers) if pid in vectors]
        [v_profile] = embedder.embed([profile_text(radar)])
        pos = [vectors[pid] for pid in sorted(seed_ids | set(feedback.positive)) if pid in vectors]
        neg = [vectors[pid] for pid in feedback.negative if pid in vectors]
        e_u = rocchio(v_profile, pos, neg)
        if any(pid in vectors for pid in feedback.positive + feedback.negative):
            e_base = rocchio(v_profile, [vectors[pid] for pid in sorted(seed_ids) if pid in vectors], [])
            profile_info["shift"] = _r(1.0 - cosine(e_u, e_base))
            profile_info["feedback_used"] = {
                "positive": [pid for pid in feedback.positive if pid in vectors],
                "negative": [pid for pid in feedback.negative if pid in vectors]}

    # ---- relevance ----
    def with_graph(r0: float, pid: str, key: str) -> float:
        if graph is None:
            return r0
        return (r0 + GAMMA * float((graph.get(pid) or {}).get(key) or 0.0)) / (1.0 + GAMMA)

    max_bm25 = max((c.bm25 for c in cands), default=0.0)
    use_sim = e_u is not None or any(c.similarity is not None for c in cands)
    relevance: Dict[str, float] = {}
    relevance_base: Optional[Dict[str, float]] = {} if e_base is not None else None
    sim_of: Dict[str, Optional[float]] = {}
    sim_base: Dict[str, Optional[float]] = {}
    for c in cands:
        if e_u is not None:
            sim = cosine(vectors[c.paper_id], e_u) if c.paper_id in vectors else None
        else:
            sim = c.similarity
        sim_of[c.paper_id] = sim
        bm = c.bm25 / max_bm25 if max_bm25 > 0 else 0.0
        relevance[c.paper_id] = with_graph(ALPHA * _clip01(sim or 0.0) + BETA * bm if use_sim else bm, c.paper_id, "value")
        if relevance_base is not None and e_base is not None:
            sb = cosine(vectors[c.paper_id], e_base) if c.paper_id in vectors else None
            sim_base[c.paper_id] = sb
            relevance_base[c.paper_id] = with_graph(ALPHA * _clip01(sb or 0.0) + BETA * bm, c.paper_id, "value_seeds")

    def nearest_feedback(pid: str, ids: Sequence[str]) -> Optional[Dict[str, Any]]:
        """The feedback paper most like ``pid``, with the feedback given on it."""
        if pid not in vectors:
            return None
        sims = [(cosine(vectors[pid], vectors[f]), f) for f in ids if f in vectors]
        if not sims:
            return None
        sim, f = max(sims, key=lambda x: (x[0], _neg(x[1])))
        return {"paper_id": f, "title": fb_papers[f].title if f in fb_papers else None,
                "action": feedback.excluded.get(f), "similarity": _r(sim)}

    # ---- novelty (no temporal leakage) ----
    nov_raw: Dict[str, float] = {}
    nov_detail: Dict[str, Dict[str, Any]] = {}
    for c in cands:
        if c.paper_id not in vectors or not hist_items:
            continue
        value, knn, h_size = novelty_raw(vectors[c.paper_id], c.paper.first_seen, hist_items, c.paper_id)
        nov_detail[c.paper_id] = {"history_size": h_size, "nearest": knn}
        if value is not None:
            nov_raw[c.paper_id] = _clip01(value)
    kinds = {pid: publication_kind(p) for pid, p in papers.items()}
    novelty: Dict[str, float] = {}
    for kind in sorted(set(kinds.values())):
        group = {pid: v for pid, v in nov_raw.items() if kinds[pid] == kind}
        if len(group) >= MIN_GROUP_FOR_PERCENTILE:
            novelty.update(_percentiles(group))
            for pid in group:
                nov_detail[pid]["normalization"] = f"percentile_within:{kind}"
        else:
            novelty.update(group)
            for pid in group:
                nov_detail[pid]["normalization"] = "raw"

    # ---- method match ----
    method_terms = radar.method_terms()
    methods: Dict[str, Tuple[float, List[str]]] = {}
    for c in cands:
        hit = matched_terms(c.paper, method_terms) if method_terms else []
        denom = min(METHOD_SATURATION, len(method_terms)) or 1
        methods[c.paper_id] = (min(1.0, len(hit) / denom), hit)

    # ---- active signals and weights ----
    inactive = dict(INACTIVE_REASONS)
    active = ["relevance"]
    if novelty:
        active.append("novelty")
    else:
        inactive["novelty"] = ("no embeddings (lexical-only run)" if not vectors
                               else f"fewer than {MIN_HISTORY} earlier papers in history (first scans)")
    if method_terms:
        active.append("method_match")
    else:
        inactive["method_match"] = "radar has no methods_profile (or signal_profile.methods)"
    weights = effective_weights(radar, mode, active)

    # ---- score ----
    excluded: Dict[str, List[str]] = {}
    eligible: List[Candidate] = []
    for c in cands:
        pid = c.paper_id
        if pid in seed_ids:
            excluded.setdefault("seed_paper", []).append(pid)
        elif pid in feedback.excluded:
            excluded.setdefault(EXCLUDED_BY_FEEDBACK[feedback.excluded[pid]], []).append(pid)
        else:
            eligible.append(c)

    def score_all(rel: Dict[str, float]) -> Tuple[Dict[str, Tuple[float, Dict[str, Optional[float]], List[str]]],
                                                   Dict[str, float]]:
        max_rel = max(rel.values(), default=0.0)
        gate = {pid: (r / max_rel if max_rel > 0 else 0.0) for pid, r in rel.items()}
        scored: Dict[str, Tuple[float, Dict[str, Optional[float]], List[str]]] = {}
        for c in eligible:
            pid = c.paper_id
            flags: List[str] = []
            comp: Dict[str, Optional[float]] = {"relevance": rel[pid]}
            if "novelty" in active:
                if pid in novelty:
                    comp["novelty"] = novelty[pid]
                else:
                    comp["novelty"] = NEUTRAL_NOVELTY
                    flags.append("novelty_insufficient_history")
            if "method_match" in active:
                comp["method_match"] = methods[pid][0]
            if kinds[pid] == "notice":
                flags.append("correction_or_notice")
            if c.paper.is_retracted:
                flags.append("retracted")
            update_types = {u.type for u in c.paper.updates}
            if update_types & CORRECTING_UPDATES:
                flags.append("corrected")
            if "expression_of_concern" in update_types:
                flags.append("expression_of_concern")
            s = sum(weights[k] * (comp[k] or 0.0) * (gate[pid] if k == "novelty" else 1.0) for k in weights)
            scored[pid] = (s, comp, flags)
        return scored, gate

    scored, gate = score_all(relevance)

    # ---- MMR selection ----
    tokens = {pid: set(search_tokens(paper_text(papers[pid]))) for pid in scored}

    def pair_sim(a: str, b: str) -> float:
        if a in vectors and b in vectors:
            return cosine(vectors[a], vectors[b])
        ta, tb = tokens[a], tokens[b]
        return len(ta & tb) / len(ta | tb) if ta and tb else 0.0

    def select(scores: Dict[str, Tuple[float, Any, Any]], gates: Dict[str, float]) -> Tuple[List[Tuple[str, float, float]], List[str]]:
        # MMR runs over the relevance-qualified pool first, so diversity pressure can't pull an off-topic paper
        # above relevant ones; the rest follow, in the same way, only if top_n isn't filled.
        qualified = sorted(pid for pid in scores if gates[pid] >= MIN_SELECTION_GATE)
        low = sorted(pid for pid in scores if gates[pid] < MIN_SELECTION_GATE)
        low_ids = list(low)
        chosen: List[Tuple[str, float, float]] = []
        remaining = qualified
        while (remaining or low) and len(chosen) < top_n:
            if not remaining:
                remaining, low = low, []
            best = None
            for pid in remaining:
                red = max((max(0.0, pair_sim(pid, s)) for s, _, _ in chosen), default=0.0)
                m = lam * scores[pid][0] - (1 - lam) * red
                key = (m, scores[pid][0], _neg(pid))
                if best is None or key > best[0]:
                    best = (key, pid, m, red)
            _, pid, m, red = best  # type: ignore[misc]
            chosen.append((pid, m, red))
            remaining.remove(pid)
        return chosen, low_ids

    selected, low_ids = select(scored, gate)
    for pid in low_ids:
        scored[pid][2].append("low_relevance")

    # ---- what feedback changed: the same run, ranked with the profile before feedback ----
    rank_without: Dict[str, int] = {}
    feedback_effect: Optional[Dict[str, Any]] = None
    if relevance_base is not None:
        selected0, _ = select(*score_all(relevance_base))
        rank_without = {pid: i for i, (pid, _, _) in enumerate(selected0, 1)}
        now = [pid for pid, _, _ in selected]
        feedback_effect = {
            "entered": [pid for pid in now if pid not in rank_without],
            "left": [pid for pid in rank_without if pid not in set(now)],
            "moved_up": sum(1 for i, pid in enumerate(now, 1) if pid in rank_without and rank_without[pid] > i),
            "moved_down": sum(1 for i, pid in enumerate(now, 1) if pid in rank_without and rank_without[pid] < i),
            "profile_shift": profile_info.get("shift"),
        }

    by_cand = {c.paper_id: c for c in cands}
    signal_terms = {name: list(cat.terms) for name, cat in sorted(radar.signal_profile.items())}
    items = []
    for i, (pid, m, red) in enumerate(selected, 1):
        s, comp, flags = scored[pid]
        c = by_cand[pid]
        signals = {name: hit for name, terms in signal_terms.items() if (hit := matched_terms(c.paper, terms))}
        items.append(RankedPaper(
            rank=i, paper_id=pid, title=c.paper.title, score=s, mmr=m, redundancy=red, components=comp,
            details={
                "retrieved_by": list(c.retrieved_by),
                "similarity": _r(sim_of.get(pid)),
                "bm25": _r(c.bm25),
                "bm25_norm": _r(c.bm25 / max_bm25 if max_bm25 > 0 else 0.0),
                "matched_terms": list(c.matched_terms),
                **({"graph": graph.get(pid, {"status": "not_computed", "value": 0.0})} if graph is not None else {}),
                "novelty_raw": _r(nov_raw.get(pid)),
                "novelty_gate": _r(gate[pid]),
                "novelty": nov_detail.get(pid, {}),
                "methods_matched": methods[pid][1],
                "signals_matched": signals,
                "publication_kind": kinds[pid],
                "sources": sorted({r.source.value for r in c.paper.sources}),
                "first_seen": c.paper.first_seen.isoformat(),
                "earliest_public_date": c.paper.earliest_public_date,
                **({"feedback": {
                    "similarity_without_feedback": _r(sim_base.get(pid)),
                    "relevance_without_feedback": _r(relevance_base[pid]),
                    "relevance_change": _r(comp["relevance"] - relevance_base[pid]),  # type: ignore[operator]
                    "rank_without_feedback": rank_without.get(pid),
                    "nearest_positive": nearest_feedback(pid, feedback.positive),
                    "nearest_negative": nearest_feedback(pid, feedback.negative),
                }} if relevance_base is not None else {}),
            },
            flags=tuple(flags),
        ))

    base_formula = f"{ALPHA}*sim + {BETA}*bm25_norm" if use_sim else "bm25_norm"
    stats = {
        "candidates": len(cands),
        "scored": len(scored),
        "selected": len(items),
        "excluded": {k: sorted(v) for k, v in excluded.items()},
        "history_size": len(hist_items),
        "profile": profile_info,
        "feedback_effect": feedback_effect,
        "relevance_formula": (f"({base_formula} + {GAMMA}*graph) / {1 + GAMMA}" if graph is not None else base_formula),
        "graph": _graph_stats(graph) if graph is not None else None,
        "candidate_stats": candidates.stats,
    }
    return Ranking(
        ranking_version=RANKING_VERSION, mode=mode, mmr_lambda=lam, weights=weights, active_signals=tuple(active),
        inactive_signals=dict(sorted(inactive.items())), model_version=embedder.model_version if embedder and vectors else None,
        items=items, stats=stats,
    )


def _graph_stats(graph: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    vals = list(graph.values())
    by_status: Dict[str, int] = {}
    for v in vals:
        by_status[v.get("status", "?")] = by_status.get(v.get("status", "?"), 0) + 1
    return {"gamma": GAMMA, "papers": len(vals), "by_status": dict(sorted(by_status.items())),
            "direct_links": sum(1 for v in vals if v.get("cites") or v.get("cited_by")),
            "coupled": sum(1 for v in vals if not (v.get("cites") or v.get("cited_by")) and (v.get("shared_references") or 0) > 0)}


class _neg:
    """Inverts string ordering so that, among equal MMR and score, the smaller paper_id wins under max()."""

    __slots__ = ("s",)

    def __init__(self, s: str) -> None:
        self.s = s

    def __lt__(self, other: "_neg") -> bool:
        return self.s > other.s

    def __gt__(self, other: "_neg") -> bool:
        return self.s < other.s

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _neg) and self.s == other.s
