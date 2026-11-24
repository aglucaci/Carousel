"""Splitting a paper's text into citable passages (sentences) with stable IDs."""

from __future__ import annotations

import html
import re
from typing import Iterable, List, Optional, Sequence, Tuple

from carousel.summarization.schema import Passage

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
# Split after . ! ? followed by space and an uppercase letter, digit, or opening bracket; not after common
# abbreviations ("e.g.", "et al.", "Fig."). Single capitals end sentences ("antigenic site B. A limitation ...").
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\[])")
_ABBREV = re.compile(r"\b(?:e\.g|i\.e|et al|Fig|Figs|Ref|vs|approx|ca|cf|No|Dr|Mr|Ms|Inc|Ltd|St)\.$")
_HEADING = re.compile(r"^(BACKGROUND|INTRODUCTION|OBJECTIVES?|AIMS?|METHODS?|MATERIALS AND METHODS|RESULTS?|FINDINGS|"
                      r"CONCLUSIONS?|INTERPRETATION|DISCUSSION|SIGNIFICANCE|IMPORTANCE)\s*[:.]\s*", re.I)


def clean(text: Optional[str]) -> str:
    """Tags removed, entities decoded, whitespace collapsed (what quotes are matched against)."""
    return _SPACE.sub(" ", html.unescape(_TAG.sub(" ", text or ""))).strip()


def sentences(text: str) -> List[str]:
    parts: List[str] = []
    for piece in _SPLIT.split(clean(text)):
        if parts and _ABBREV.search(parts[-1]):
            parts[-1] = parts[-1] + " " + piece
        else:
            parts.append(piece)
    return [p.strip() for p in parts if p.strip()]


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:24] or "section"


def from_sections(sections: Iterable[Tuple[str, str]], *, prefix: str, text_class: str = "public") -> List[Passage]:
    """``[(section title, text)]`` -> passages ``{prefix}{n}.{slug}:{m}``, one per sentence."""
    out: List[Passage] = []
    for n, (title, text) in enumerate(sections, 1):
        for m, s in enumerate(sentences(text), 1):
            heading = _HEADING.match(s)
            section = heading.group(1).title() if heading else title
            out.append(Passage(id=f"{prefix}{n}.{_slug(title)}:{m}", section=section or title, text=s,
                               text_class=text_class))  # type: ignore[arg-type]
    return out


def from_abstract(abstract: Optional[str], title: Optional[str] = None) -> List[Passage]:
    """The title (``title:1``) and the abstract's sentences (``abstract:1`` ...)."""
    out = [Passage(id="title:1", section="Title", text=clean(title))] if clean(title) else []
    for m, s in enumerate(sentences(abstract or ""), 1):
        heading = _HEADING.match(s)
        out.append(Passage(id=f"abstract:{m}", section=f"Abstract / {heading.group(1).title()}" if heading else "Abstract",
                           text=s))
    return out


def from_user_text(text: str) -> List[Passage]:
    """Text the user supplied (private): paragraphs as sections."""
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: List[Passage] = []
    for n, para in enumerate(paras, 1):
        for m, s in enumerate(sentences(para), 1):
            out.append(Passage(id=f"user:{n}.{m}", section=f"Your text, paragraph {n}", text=s, text_class="private"))
    return out


def normalized(text: str) -> str:
    """For quote matching: collapsed whitespace, unified quotes and dashes, case kept."""
    t = clean(text)
    for a, b in (("‘", "'"), ("’", "'"), ("“", '"'), ("”", '"'), ("–", "-"), ("—", "-"),
                 ("−", "-"), (" ", " ")):
        t = t.replace(a, b)
    return t


def by_id(passages: Sequence[Passage]) -> dict:
    return {p.id: p for p in passages}


__all__ = ["by_id", "clean", "from_abstract", "from_sections", "from_user_text", "normalized", "sentences"]
