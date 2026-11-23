"""Cautious duplicate detection for papers without a shared identifier.

The store merges by exact identifier first (DOI, then PMID, PMCID, OpenAlex,
arXiv). This module handles what is left: two papers whose IDs don't overlap
but whose title, authors, and year suggest they may be the same work.

:func:`compare` returns one of three decisions:

``merge``
    Only when *every* gate passes: near-identical titles
    (similarity >= :data:`AUTO_MERGE_TITLE`), the same first author, years at
    most one apart, the same kind of work (preprint vs. published), and no
    conflicting identifiers, and no source listing both papers separately.
    Automatic merges are logged and can be undone.
``review``
    Plausibly the same work, but at least one gate fails, so it goes to the
    review queue. A preprint and its published version always land here:
    versions are **linked, not collapsed** (verified version links arrive in
    Phase 2).
``distinct``
    Different works. Hard blockers include a correction/retraction notice vs.
    the article it is about, different part or volume numbers ("Part I" vs.
    "Part II"), titles too generic to compare ("Editorial"), and years more
    than two apart.

Everything is stdlib, deterministic, and reports the features it used, so a
reviewer can see why a pair was flagged.
"""

from __future__ import annotations

import dataclasses
import difflib
import re
from typing import Dict, List, Optional, Sequence, Set, Tuple

from carousel.core.identifiers import SCHEMES
from carousel.core.paper import CanonicalPaper, partial_date_bounds
from carousel.core.text import STOPWORDS
from carousel.core.text import normalize_text as normalize_title

AUTO_MERGE_TITLE = 0.97
"""Title similarity required (with all other gates) for an automatic merge."""
REVIEW_TITLE = 0.85
"""Below this title similarity, a pair is distinct."""
MAX_REVIEW_YEAR_GAP = 2
MIN_TITLE_TOKENS = 4

_NOTICE = re.compile(
    r"^(?:erratum|errata|corrigendum|corrigenda|correction|author correction|publisher correction|"
    r"retraction|retracted|retraction note|notice of retraction|expression of concern|"
    r"editorial expression of concern|withdrawal|withdrawn|comment on|commentary on|reply to|"
    r"response to|addendum)\b"
)
_NOTICE_TYPES = frozenset({
    "published erratum", "erratum", "retraction of publication", "retraction", "comment", "correction",
    "expression of concern",
})
_GENERIC_TITLES = frozenset({
    "editorial", "introduction", "preface", "foreword", "correction", "erratum", "reply", "response",
    "commentary", "book review", "news", "letter to the editor", "in this issue", "abstracts", "contents",
    "index", "obituary", "acknowledgements", "acknowledgments", "reviewer acknowledgement", "front matter",
})
_ROMAN = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}
_SERIES_WORDS = {"part", "phase", "paper", "series", "study", "volume", "vol", "chapter", "wave", "trial"}
_COLLECTIVE = re.compile(r"\b(consortium|group|collaboration|network|investigators|committee|team|initiative)\b", re.I)


# -------------------------
# Normalization
# -------------------------

def title_tokens(title: Optional[str]) -> List[str]:
    """Significant tokens for candidate blocking (unique, in order)."""
    seen: Dict[str, None] = {}
    for tok in normalize_title(title).split():
        if len(tok) >= 3 and tok not in STOPWORDS:
            seen.setdefault(tok, None)
    return list(seen)


def _series_markers(norm: str) -> Tuple[str, ...]:
    toks = norm.split()
    markers = [t for t in toks if t.isdigit()]
    markers += [f"{a} {b}" for a, b in zip(toks, toks[1:]) if a in _SERIES_WORDS and (b in _ROMAN or b.isdigit())]
    return tuple(sorted(markers))


def family_name(name: str) -> Optional[str]:
    """Best-effort family name: 'Ada Lovelace', 'Lovelace A', and 'Lovelace, Ada' all give 'lovelace'."""
    if not name or _COLLECTIVE.search(name):
        return None
    if "," in name:
        part = name.split(",", 1)[0]
    else:
        toks = name.split()
        if len(toks) > 1 and re.fullmatch(r"[A-Z]{1,3}\.?", toks[-1].replace(".", "")):
            part = " ".join(toks[:-1])
        else:
            part = toks[-1] if toks else ""
    # Last word only, so particles and hyphenation don't matter ("van der Berg" / "Berg", "Smith-Jones" / "Jones").
    words = normalize_title(part).split()
    return words[-1] if words else None


def _families(paper: CanonicalPaper) -> List[str]:
    return [f for f in (family_name(a.name) for a in paper.authors[:25]) if f]


def _year(paper: CanonicalPaper) -> Optional[int]:
    date = paper.earliest_public_date
    return partial_date_bounds(date)[0].year if date else None


def is_preprint(paper: CanonicalPaper) -> bool:
    types = {t.lower() for t in paper.publication_types}
    return bool(paper.dates.preprint_posted) or bool(types & {"preprint", "posted-content"})


def is_notice(paper: CanonicalPaper) -> bool:
    types = {t.lower() for t in paper.publication_types}
    return bool(_NOTICE.match(normalize_title(paper.title))) or bool(types & _NOTICE_TYPES)


def _identifier_values(paper: CanonicalPaper, scheme: str) -> Set[str]:
    return {v for r in paper.sources for v in [getattr(r.identifiers, scheme)] if v}


def title_similarity(a: str, b: str) -> float:
    """Character-level similarity of normalized titles, allowing for a dropped subtitle (capped below auto-merge)."""
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    short, long_ = sorted((a, b), key=len)
    if long_.startswith(short + " ") and len(short.split()) >= 6:
        ratio = max(ratio, 0.90)
    return round(ratio, 4)


# -------------------------
# Decision
# -------------------------

@dataclasses.dataclass(frozen=True)
class MatchDecision:
    decision: str  # merge | review | distinct
    score: float
    reasons: Tuple[str, ...]
    features: Dict[str, object]

    @property
    def is_merge(self) -> bool:
        return self.decision == "merge"


def compare(a: CanonicalPaper, b: CanonicalPaper) -> MatchDecision:
    """Decide whether two papers (without a shared identifier) are the same work."""
    ta, tb = normalize_title(a.title), normalize_title(b.title)
    sim = title_similarity(ta, tb)
    fa, fb = _families(a), _families(b)
    first_author = None if not fa or not fb else fa[0] == fb[0]
    overlap = None if not fa or not fb else len(set(fa) & set(fb)) / min(len(set(fa)), len(set(fb)))
    ya, yb = _year(a), _year(b)
    year_gap = None if ya is None or yb is None else abs(ya - yb)
    id_conflicts = [s for s in SCHEMES if (va := _identifier_values(a, s)) and (vb := _identifier_values(b, s)) and not va & vb]
    features: Dict[str, object] = {
        "title_similarity": sim,
        "first_author_match": first_author,
        "author_overlap": None if overlap is None else round(overlap, 3),
        "year_gap": year_gap,
        "identifier_conflicts": id_conflicts,
        "preprint": [is_preprint(a), is_preprint(b)],
        "notice": [is_notice(a), is_notice(b)],
        "shared_sources": sorted({r.source.value for r in a.sources} & {r.source.value for r in b.sources}),
        "numbers_match": _series_markers(ta) == _series_markers(tb),
        "title_generic": min(len(ta.split()), len(tb.split())) < MIN_TITLE_TOKENS
        or ta in _GENERIC_TITLES or tb in _GENERIC_TITLES,
    }
    author_part = overlap if overlap is not None else 0.0
    year_part = {0: 1.0, 1: 0.5}.get(year_gap, 0.0) if year_gap is not None else 0.0
    score = round(0.7 * sim + 0.2 * author_part + 0.1 * year_part, 4)

    def out(decision: str, *reasons: str) -> MatchDecision:
        return MatchDecision(decision, score, reasons, features)

    if is_notice(a) != is_notice(b):
        return out("distinct", "notice_vs_article")
    if features["title_generic"]:
        return out("distinct", "title_too_generic")
    if not features["numbers_match"]:
        return out("distinct", "number_or_part_differs")
    if sim < REVIEW_TITLE:
        return out("distinct", "title_dissimilar")
    if year_gap is not None and year_gap > MAX_REVIEW_YEAR_GAP:
        return out("distinct", "years_too_far_apart")
    if is_preprint(a) != is_preprint(b):
        return out("review", "possible_preprint_version")
    if id_conflicts:
        if first_author:
            return out("review", "identifier_conflict")
        return out("distinct", "identifier_conflict")
    if first_author is False:
        if (overlap or 0.0) < 0.5:
            return out("distinct", "different_authors")
        return out("review", "first_author_differs")
    if first_author is None:
        return out("review", "authors_missing")
    if year_gap is None:
        return out("review", "year_missing")
    if features["shared_sources"]:
        # One source listing the same work twice under different IDs is possible but rare; a human decides.
        return out("review", "same_source_twice")
    if sim >= AUTO_MERGE_TITLE and year_gap <= 1:
        return out("merge", "all_gates_passed")
    return out("review", "below_auto_merge_threshold")


def best_match(paper: CanonicalPaper, candidates: Sequence[CanonicalPaper]) -> List[Tuple[CanonicalPaper, MatchDecision]]:
    """Non-distinct decisions against ``candidates``, best first (merges before reviews, then by score)."""
    scored = [(c, compare(paper, c)) for c in candidates if c.paper_id != paper.paper_id]
    scored = [(c, d) for c, d in scored if d.decision != "distinct"]
    return sorted(scored, key=lambda cd: (cd[1].decision != "merge", -cd[1].score, cd[0].paper_id))
