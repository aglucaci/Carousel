"""Recommendation explanations, written at ranking time and stored with the run (plan task 19).

An explanation is a short list of reasons, each in plain words and each
traceable to a stored feature, plus a one-line summary::

    {"version": "1",
     "summary": "Matches immune escape, antigenic drift; raised by your feedback",
     "reasons": [{"kind": "relevance", "text": "...", "share": 0.62}, ...]}

Explanations are **versioned**: the wording and the choice of reasons may
change between versions, but a stored run always shows the explanation it was
recommended with, never one regenerated later. :data:`EXPLANATION_VERSION`
changes whenever what is said, or how it is derived, changes.

Version history:

``1``
    Relevance (matched terms, keyword and meaning scores, the source that found
    it), feedback (how much it moved this paper's relevance and rank, and the
    feedback paper it is most like), novelty, method match, signal terms,
    integrity flags, and linked preprint/published versions.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

EXPLANATION_VERSION = "1"

VERIFYING_SOURCES = frozenset({"crossref"})
"""Sources that check what the searches found but never search, so never "found" a paper."""

FEEDBACK_NOTICEABLE = 0.01
"""A relevance change smaller than this is not reported as feedback's doing."""

_ACTION_WORDS = {"save": "you saved", "relevant": "you marked relevant", "irrelevant": "you marked not relevant"}
_SOURCE_NAMES = {"pubmed": "PubMed", "openalex": "OpenAlex", "europepmc": "Europe PMC", "crossref": "Crossref"}
_FLAG_TEXT = {
    "retracted": "A source marks it as retracted.",
    "corrected": "A correction or erratum has been published for it.",
    "expression_of_concern": "An expression of concern has been issued about it.",
    "correction_or_notice": "It is a correction or notice rather than a research paper.",
    "low_relevance": "It is well below this run's best relevance; shown only to fill the list.",
}


def _fmt(x: Optional[float]) -> str:
    return "—" if x is None else f"{x:.2f}"


def _list(items: List[str], n: int = 4) -> str:
    items = list(dict.fromkeys(items))
    return ", ".join(items[:n]) + (f" and {len(items) - n} more" if len(items) > n else "")


def _share(item: Mapping[str, Any], weights: Mapping[str, float], signal: str) -> Optional[float]:
    comp = item.get("components", {}) or {}
    score = item.get("score") or 0.0
    if signal not in weights or comp.get(signal) is None or score <= 0:
        return None
    gate = (item.get("details", {}) or {}).get("novelty_gate") if signal == "novelty" else 1.0
    return round(weights[signal] * comp[signal] * (gate if gate is not None else 1.0) / score, 3)


def explain(item: Mapping[str, Any], weights: Mapping[str, float], inactive: Mapping[str, str]) -> Dict[str, Any]:
    """The explanation for one ranked item (its ``to_dict()``, with flags and linked versions added)."""
    d = item.get("details", {}) or {}
    comp = item.get("components", {}) or {}
    reasons: List[Dict[str, Any]] = []
    summary: List[str] = []

    # ---- relevance ----
    terms = d.get("matched_terms") or []
    parts = []
    if terms:
        parts.append(f"Matches the radar's terms {_list(terms)} (keyword score {_fmt(d.get('bm25_norm'))} of this run's best)")
        summary.append(f"Matches {_list(terms, 3)}")
    else:
        parts.append("Matches none of the radar's terms by keyword")
    if d.get("similarity") is not None:
        parts.append(f"meaning similarity to the radar's profile {_fmt(d.get('similarity'))}")
    if "source_query" in (d.get("retrieved_by") or []):
        srcs = [_SOURCE_NAMES.get(s, s) for s in (d.get("sources") or []) if s not in VERIFYING_SOURCES]
        parts.append(f"found by the search of {_list(srcs)}" if srcs else "found by a source's own search")
    reasons.append({"kind": "relevance", "text": "; ".join(parts) + ".", "share": _share(item, weights, "relevance")})

    # ---- feedback ----
    fb = d.get("feedback")
    if fb:
        change = fb.get("relevance_change") or 0.0
        before, now = fb.get("rank_without_feedback"), item.get("rank")
        if abs(change) >= FEEDBACK_NOTICEABLE:
            direction = "raised" if change > 0 else "lowered"
            text = f"Your feedback {direction} its relevance by {abs(change):.2f}"
            if before is None:
                text += " and brought it into this list (it would not be here without your feedback)"
            elif before != now:
                text += f" (it would rank #{before} without your feedback)"
            near = fb.get("nearest_positive") if change > 0 else fb.get("nearest_negative")
            if near and near.get("title"):
                text += (f". It is most like a paper {_ACTION_WORDS.get(near.get('action') or '', 'you gave feedback on')}:"
                         f" “{near['title']}” (similarity {_fmt(near.get('similarity'))})")
            reasons.append({"kind": "feedback", "text": text + ".", "share": None, "change": round(change, 4)})
            summary.append(f"{direction} by your feedback")
        else:
            reasons.append({"kind": "feedback", "text": "Your feedback barely changed its relevance.", "share": None,
                            "change": round(change, 4)})

    # ---- novelty ----
    if "novelty" in comp:
        nov = d.get("novelty") or {}
        if "novelty_insufficient_history" in (item.get("flags") or []):
            reasons.append({"kind": "novelty", "text": "Novelty not judged: too few earlier papers to compare with.",
                            "share": _share(item, weights, "novelty")})
        else:
            text = (f"Novelty {_fmt(comp['novelty'])} against {nov.get('history_size', 0)} papers seen before it"
                    f" (credited at {_fmt(d.get('novelty_gate'))} of its value, in proportion to relevance).")
            reasons.append({"kind": "novelty", "text": text, "share": _share(item, weights, "novelty")})
            if (comp.get("novelty") or 0) >= 0.75:
                summary.append("unlike earlier papers")
    elif "novelty" in inactive:
        reasons.append({"kind": "novelty", "text": f"Novelty not used: {inactive['novelty']}.", "share": None})

    # ---- methods and signals ----
    if d.get("methods_matched"):
        reasons.append({"kind": "method_match", "text": f"Uses the radar's methods: {_list(d['methods_matched'])}.",
                        "share": _share(item, weights, "method_match")})
        summary.append(f"uses {_list(d['methods_matched'], 2)}")
    for name, hits in sorted((d.get("signals_matched") or {}).items()):
        reasons.append({"kind": "signal", "text": f"Mentions {name.replace('_', ' ')}: {_list(hits)}.", "share": None})

    # ---- integrity and versions ----
    for flag in item.get("flags") or []:
        if flag in _FLAG_TEXT:
            reasons.append({"kind": "integrity" if flag != "low_relevance" else "selection", "text": _FLAG_TEXT[flag],
                            "share": None})
    for v in d.get("versions") or []:
        how = _list([_SOURCE_NAMES.get(m, "a reviewer" if m == "manual" else m) for m in v.get("linked_by", [])])
        if v.get("role") == "published":
            where = f" in {v['venue']}" if v.get("venue") else ""
            reasons.append({"kind": "version", "text": f"This preprint has since been published{where} (linked by {how}).",
                            "share": None})
            summary.append("since published")
        else:
            reasons.append({"kind": "version", "text": f"An earlier preprint of it exists (linked by {how}).", "share": None})

    text = "; ".join(summary) if summary else "Ranked on relevance to the radar's profile"
    return {"version": EXPLANATION_VERSION, "summary": text[:1].upper() + text[1:], "reasons": reasons}


__all__ = ["EXPLANATION_VERSION", "explain"]
