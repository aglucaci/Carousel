"""Radar feedback, kept in a plain YAML file next to the radar and applied on every scan.

``examples/viral_evolution.yml`` -> ``examples/viral_evolution.feedback.yml``::

    # Feedback for the "viral-evolution" radar. Applied on the next `carousel scan`.
    save: [cp_3f2a9c1e5b7d4a60]
    dismiss: ["doi:10.1016/j.cell.2026.01.001"]
    relevant: ["pmid:41000201"]
    irrelevant: []

A paper is referred to by its CAROUSEL ``paper_id`` (shown in every result) or
by an identifier, ``doi:``, ``pmid:``, ``pmcid:``, ``openalex:``, ``arxiv:``,
or a bare DOI. The file is the source of truth: removing a line undoes it.
The store keeps an append-only history of every change.

Effects on the next scan:

- every paper with feedback leaves the recommendations (it has been seen);
- ``save`` and ``relevant`` pull the radar's interest profile toward them,
  ``irrelevant`` pushes it away (Rocchio); ``dismiss`` only hides.

Feedback that names a paper the store doesn't have yet stays pending and is
retried on later scans.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Tuple, Union

import yaml

from carousel.core.identifiers import SCHEMES, IdentifierError

if TYPE_CHECKING:  # pragma: no cover
    from carousel.database.local import PaperStore

ACTIONS = ("save", "dismiss", "relevant", "irrelevant")
POSITIVE = ("save", "relevant")
NEGATIVE = ("irrelevant",)


class FeedbackError(ValueError):
    pass


def default_path(radar_path: Union[str, Path]) -> Path:
    p = Path(radar_path)
    return p.with_name(f"{p.stem}.feedback.yml")


def load(path: Union[str, Path]) -> Dict[str, List[str]]:
    """{action: [refs]} from the file; a missing file is empty feedback."""
    p = Path(path)
    data: Dict[str, List[str]] = {a: [] for a in ACTIONS}
    if not p.exists():
        return data
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise FeedbackError(f"{p}: expected a mapping of {', '.join(ACTIONS)} to lists")
    unknown = sorted(set(raw) - set(ACTIONS))
    if unknown:
        raise FeedbackError(f"{p}: unknown action(s) {', '.join(map(str, unknown))}; use {', '.join(ACTIONS)}")
    for action, refs in raw.items():
        if refs is None:
            continue
        if not isinstance(refs, list) or not all(isinstance(r, (str, int)) for r in refs):
            raise FeedbackError(f"{p}: `{action}` must be a list of paper references")
        data[action] = list(dict.fromkeys(str(r).strip() for r in refs if str(r).strip()))
    return data


def save(path: Union[str, Path], data: Dict[str, List[str]], radar_id: Optional[str] = None) -> Path:
    p = Path(path)
    header = f'# Feedback for the "{radar_id}" radar. ' if radar_id else "# Radar feedback. "
    header += "Applied on the next `carousel scan`; remove a line to undo it.\n"
    body = yaml.safe_dump({a: list(data.get(a, [])) for a in ACTIONS}, sort_keys=False, default_flow_style=False)
    p.write_text(header + body, encoding="utf-8")
    return p


def update(path: Union[str, Path], action: str, refs: Iterable[str], remove: bool = False,
           radar_id: Optional[str] = None) -> Dict[str, List[str]]:
    if action not in ACTIONS:
        raise FeedbackError(f"unknown action {action!r}; use {', '.join(ACTIONS)}")
    data = load(path)
    for ref in refs:
        if remove:
            data[action] = [r for r in data[action] if r != ref]
        elif ref not in data[action]:
            data[action].append(ref)
    save(path, data, radar_id)
    return data


@dataclasses.dataclass(frozen=True)
class Feedback:
    """Feedback resolved to current paper IDs."""

    by_paper: Dict[str, Tuple[str, ...]]          # paper_id -> actions
    unresolved: Tuple[str, ...] = ()

    @property
    def excluded(self) -> Dict[str, str]:
        """paper_id -> the action that removes it from recommendations."""
        return {pid: next(a for a in ACTIONS if a in acts) for pid, acts in self.by_paper.items()}

    @property
    def positive(self) -> List[str]:
        return sorted(pid for pid, acts in self.by_paper.items() if set(acts) & set(POSITIVE))

    @property
    def negative(self) -> List[str]:
        return sorted(pid for pid, acts in self.by_paper.items() if set(acts) & set(NEGATIVE))

    @classmethod
    def empty(cls) -> "Feedback":
        return cls({})


def _resolve_ref(store: "PaperStore", ref: str) -> Optional[str]:
    from carousel.database.local import StoreError

    if ref.startswith("cp_"):
        try:
            pid = store.resolve(ref)
        except StoreError:
            return None
        return pid if store.get(pid) is not None else None
    scheme, sep, value = ref.partition(":")
    if sep and scheme.lower() in SCHEMES:
        scheme, value = scheme.lower(), value
    elif ref.startswith("10.") or ref.lower().startswith(("https://doi.org/", "doi.org/")):
        scheme, value = "doi", ref
    else:
        return None
    try:
        paper = store.find(scheme, value)
    except IdentifierError:
        return None
    return paper.paper_id if paper else None


def resolve(store: "PaperStore", data: Dict[str, List[str]], source: str = "feedback file") -> Tuple[Feedback, Dict[Tuple[str, str], str]]:
    """Resolve refs to current paper IDs. Returns the Feedback and the {(paper_id, action): ref} state to store."""
    by_paper: Dict[str, List[str]] = {}
    state: Dict[Tuple[str, str], str] = {}
    unresolved: List[str] = []
    for action in ACTIONS:
        for ref in data.get(action, []):
            pid = _resolve_ref(store, ref)
            if pid is None:
                unresolved.append(f"{action}:{ref}")
                continue
            by_paper.setdefault(pid, [])
            if action not in by_paper[pid]:
                by_paper[pid].append(action)
            state.setdefault((pid, action), ref)
    clash = sorted(pid for pid, acts in by_paper.items() if "relevant" in acts and "irrelevant" in acts)
    if clash:
        raise FeedbackError(f"{source}: marked both relevant and irrelevant: {', '.join(clash)}")
    return Feedback({k: tuple(v) for k, v in sorted(by_paper.items())}, tuple(unresolved)), state
