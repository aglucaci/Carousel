"""Method Transfer v1 (plan task 35): ranking stage D's ``discovery`` signal.

A **method transfer** is a paper that applies one of the radar's methods (``methods_profile``) to a problem in a
*distant* field: far from the radar's own papers on the shared topic map (task 34) and not linked to the radar's
anchors by citation (task 32). It is offered as a **hypothesis** (*method M is used for this problem in field F; it
may apply to your question*), never as a finding, and only with a quote from the paper's title or abstract that names
the method and passes the grounding check (:func:`carousel.summarization.grounding.check_claim`).

The radar's **home** is where its own papers sit on the shared map: the shared topics holding at least
:data:`HOME_SHARE` of the radar's papers observed before the scan. A candidate's **field distance** is the smallest
distance (task 34) from its shared topic to a home topic. Then, for a paper that uses a radar method::

    discovery = min(1, (distance − MIN_DISTANCE) / (FULL_DISTANCE − MIN_DISTANCE)) · (1 − citation proximity)

and 0, with a status saying why, when it uses no radar method, sits in or near the radar's field
(``distance < MIN_DISTANCE``), has no grounded quote, carries an integrity flag, or has no position on the map.
Citation proximity is the graph term from task 32 (0 when the radar has no citation data). Without a shared map
fitted before the scan, the signal stays inactive and ranking is unchanged.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

from carousel.core.text import normalize_text
from carousel.intelligence.shared_topics import SHARED_MAP
from carousel.intelligence.topics import TopicModel, _dot, _normalize
from carousel.search.lexical import phrase_in
from carousel.summarization.grounding import check_claim
from carousel.summarization.passages import from_abstract

if TYPE_CHECKING:  # pragma: no cover
    from carousel.core.paper import CanonicalPaper

TRANSFER_VERSION = "transfer-1"
HOME_SHARE = 0.10
MIN_HOME_PAPERS = 5
MIN_DISTANCE = 0.15
FULL_DISTANCE = 0.50
INTEGRITY_FLAGS = frozenset({"retracted", "corrected", "expression_of_concern", "correction_or_notice"})
HYPOTHESIS_FLAG = "method_transfer_hypothesis"


@dataclasses.dataclass(frozen=True)
class TransferContext:
    shared: TopicModel
    placed: Dict[str, Optional[str]]          # paper_id -> shared topic (None: unassigned)
    home: Dict[str, float]                    # shared topic -> share of the radar's papers
    radar_papers: int
    question: str

    def describe(self) -> Dict[str, Any]:
        labels = {t.topic_id: t.label for t in self.shared.topics}
        return {"transfer_version": TRANSFER_VERSION, "shared_version": self.shared.classifier_version,
                "radar_papers": self.radar_papers, "min_distance": MIN_DISTANCE, "full_distance": FULL_DISTANCE,
                "home": [{"topic": t, "label": labels.get(t), "share": round(s, 4)} for t, s in sorted(self.home.items())]}


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def transfer_context(store: Any, radar_id: str, question: str, at: dt.datetime) -> Tuple[Optional[TransferContext], str]:
    """The shared map and the radar's home on it, from what was known at ``at``; or (None, why not)."""
    shared = store.active_topic_model(SHARED_MAP)
    if shared is None:
        return None, "needs the shared topic map (carousel topicmap fit)"
    fitted = dt.datetime.fromisoformat(shared.fitted_at.replace("Z", "+00:00"))
    if fitted > at:
        return None, "the shared topic map was fitted after this scan"
    placed_rows = store.paper_topics(shared.classifier_version)
    cutoff = _iso(at)
    placed = {p: v["topic_id"] for p, v in placed_rows.items() if (v.get("assigned_at") or "") <= cutoff}
    counts: Dict[str, int] = {}
    n = 0
    for pid in store.radar_paper_ids(radar_id):
        p = store.get(pid)
        if p is None or _iso(p.first_seen) >= cutoff or pid not in placed:
            continue
        n += 1
        if placed[pid]:
            counts[placed[pid]] = counts.get(placed[pid], 0) + 1
    if n < MIN_HOME_PAPERS:
        return None, f"fewer than {MIN_HOME_PAPERS} of the radar's papers are on the shared topic map"
    home = {t: c / n for t, c in counts.items() if c / n >= HOME_SHARE}
    if not home:
        return None, "the radar's papers are spread too thinly over the shared topic map to say where its field is"
    return TransferContext(shared=shared, placed=placed, home=home, radar_papers=n, question=question), ""


def method_quote(paper: "CanonicalPaper", methods: Sequence[str]) -> Optional[Dict[str, str]]:
    """The first title or abstract sentence naming one of ``methods`` that passes the grounding check."""
    passages = from_abstract(paper.abstract, paper.title)
    for m in methods:
        for ps in passages:
            if phrase_in(m, normalize_text(ps.text)):
                claim = {"text": f"The paper uses {m}.", "evidence": [{"passage_id": ps.id, "quote": ps.text}]}
                if not check_claim(claim, passages):
                    return {"method": m, "passage_id": ps.id, "section": ps.section, "quote": ps.text}
    return None


def discovery(ctx: TransferContext, paper: "CanonicalPaper", vector: Optional[Sequence[float]], methods: List[str],
              proximity: Optional[float], flags: Sequence[str]) -> Tuple[float, Dict[str, Any]]:
    """The discovery score of one candidate and what it rests on."""
    if not methods:
        return 0.0, {"status": "no_radar_method"}
    if INTEGRITY_FLAGS & set(flags):
        return 0.0, {"status": "integrity_flag"}
    topic, basis = ctx.placed.get(paper.paper_id), "assigned"
    if topic is None and vector is not None and ctx.shared.topics:
        v = _normalize(vector)
        topic = max(ctx.shared.topics, key=lambda t: (_dot(v, t.centroid), t.topic_id)).topic_id
        basis = "nearest"
    if topic is None:
        return 0.0, {"status": "no_position"}
    t = ctx.shared.topic(topic)
    dist = min(0.0 if h == topic else max(0.0, 1.0 - _dot(t.centroid, ctx.shared.topic(h).centroid))  # type: ignore[union-attr]
               for h in ctx.home)
    detail: Dict[str, Any] = {"field_topic": topic, "field": t.label, "position": basis,  # type: ignore[union-attr]
                              "distance": round(dist, 6), "citation_proximity": proximity}
    if dist < MIN_DISTANCE:
        return 0.0, {"status": "near_field", **detail}
    quote = method_quote(paper, methods)
    if quote is None:
        return 0.0, {"status": "no_grounded_quote", **detail}
    far = min(1.0, (dist - MIN_DISTANCE) / (FULL_DISTANCE - MIN_DISTANCE))
    score = round(far * (1.0 - (proximity or 0.0)), 6)
    return score, {"status": "transfer" if score > 0 else "linked_by_citation", **detail, **quote,
                   "question": ctx.question}


__all__ = ["FULL_DISTANCE", "HOME_SHARE", "HYPOTHESIS_FLAG", "MIN_DISTANCE", "TRANSFER_VERSION", "TransferContext",
           "discovery", "method_quote", "transfer_context"]
