"""Preprint <-> journal article links, without collapsing the records (plan task 18).

A preprint and the article it became are different documents: different
dates, often a different title, abstract, or author list, and sometimes
different results. Deduplication therefore never merges them (a fuzzy match
between a preprint and an article only ever goes to review). Instead they are
**linked**: both papers stay in the store, each with its own sources, and
each points at the other.

Links come from what a source asserts about a paper:

- Crossref ``relation``: ``has-preprint`` on an article, ``is-preprint-of``
  on a preprint, each naming the other DOI (deposited by the publisher or the
  preprint server; ``asserted_by`` says which side).
- Europe PMC ``commentCorrectionList``: "Preprint in" on an article, "Preprint
  of" on a preprint, naming the other Europe PMC record (Europe PMC makes most
  of these by matching title and first author; ``asserted_by`` is then
  ``title_author_match``).

:func:`link_versions` turns those relations into links between papers already
in the store; :func:`fetch_linked` looks up the other version at its source
when the store doesn't have it yet (only for the papers a scan recommends,
and at most once a month per missing work). A reviewer can also link or
unlink a pair (``carousel review link`` / ``unlink``); a reviewer's "not the
same work" is never overridden by a source.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from carousel.core.identifiers import SCHEMES, try_normalize
from carousel.core.paper import PREPRINT_RELATIONS, CanonicalPaper, Relation

LINK_RECHECK_AFTER = dt.timedelta(days=30)
"""A linked work that its source didn't have is looked up again after this long."""
MAX_LOOKUPS = 25
"""Linked works looked up per scan, at most."""

OTHER_ROLE = {"has-preprint": "preprint", "is-preprint-of": "published"}
"""Relation type -> the role of the work it names."""


def claims(paper: CanonicalPaper) -> List[Tuple[str, Relation]]:
    """(role of the other work, relation) for each preprint relation the paper's sources assert."""
    return [(OTHER_ROLE[r.type], r) for r in paper.relations if r.type in PREPRINT_RELATIONS]


def find_target(store: Any, rel: Relation) -> Optional[str]:
    """The stored paper a relation names, if any."""
    if rel.scheme == "europepmc":
        hit = store.find_source("europepmc", rel.value)
        if hit is None:
            src, _, rid = rel.value.partition(":")
            scheme = {"MED": "pmid", "PMC": "pmcid"}.get(src)
            value = try_normalize(scheme, rid) if scheme else None
            hit = store.find(scheme, value) if scheme and value else None
        return hit.paper_id if hit else None
    if rel.scheme in SCHEMES and try_normalize(rel.scheme, rel.value):
        hit = store.find(rel.scheme, rel.value)
        return hit.paper_id if hit else None
    return None


def link_versions(store: Any, paper_ids: Iterable[str], at: dt.datetime) -> Dict[str, Any]:
    """Link each paper to the versions its sources name that are in the store.

    Returns ``{linked, confirmed, refused, unresolved}``: new links, links seen again, links a reviewer's decision
    (or a contradicting link) kept out, and the ``(scheme, value)`` of linked works not in the store.
    """
    stats: Dict[str, Any] = {"linked": 0, "confirmed": 0, "refused": 0, "unresolved": []}
    seen: set = set()
    for pid in sorted(set(paper_ids)):
        paper = store.get(pid)
        if paper is None:
            continue
        for role, rel in claims(paper):
            target = find_target(store, rel)
            if target is None:
                if (rel.scheme, rel.value) not in seen:
                    seen.add((rel.scheme, rel.value))
                    stats["unresolved"].append((rel.scheme, rel.value))
                continue
            pre, pub = (target, paper.paper_id) if role == "preprint" else (paper.paper_id, target)
            evidence = {"method": rel.via or "source", "relation": rel.type, "scheme": rel.scheme, "value": rel.value,
                        "asserted_by": rel.asserted_by, "paper_id": paper.paper_id}
            outcome = store.link_asserted(pre, pub, {k: v for k, v in evidence.items() if v is not None}, at)
            if outcome in ("linked", "confirmed"):
                stats[outcome] += 1
            elif outcome == "distinct":
                stats["refused"] += 1
    return stats


def fetch_linked(store: Any, connectors: Mapping[str, Any], paper_ids: Iterable[str], at: dt.datetime,
                 max_lookups: int = MAX_LOOKUPS) -> Dict[str, Any]:
    """Look up, at its source, each version the papers' sources name but the store lacks; then link.

    DOIs go to Crossref, Europe PMC IDs to Europe PMC (when those connectors are given). A work its source
    doesn't return is remembered and not looked up again for :data:`LINK_RECHECK_AFTER`. Fetched records are
    observed into the store as papers of their own. Returns the link stats plus ``{looked_up, found, errors}``.
    """
    ids = sorted(set(paper_ids))
    first = link_versions(store, ids, at)
    wanted: Dict[str, List[str]] = {"crossref": [], "europepmc": []}
    for scheme, value in first["unresolved"]:
        checked = store.link_checked_at(scheme, value)
        if checked is not None and at - checked < LINK_RECHECK_AFTER:
            continue
        name = {"doi": "crossref", "europepmc": "europepmc"}.get(scheme)
        if name and name in connectors and hasattr(connectors[name], "fetch"):
            wanted[name].append(value)
    looked_up = found = 0
    errors: List[str] = []
    fetched: List[str] = []
    budget = max_lookups
    for name, values in wanted.items():
        values = sorted(values)[:budget]
        budget -= len(values)
        if not values:
            continue
        scheme = "doi" if name == "crossref" else "europepmc"
        try:
            records = connectors[name].fetch(values)
        except Exception as exc:  # a failed lookup leaves the links for a later scan
            errors.append(f"{name}: {exc}")
            continue
        looked_up += len(values)
        for rec in records:
            fetched.append(store.observe(rec, at).paper.paper_id)
        for value in values:
            ok = (store.find_source("europepmc", value) is not None if scheme == "europepmc"
                  else store.find("doi", value) is not None)
            found += ok
            store.mark_link_checked(scheme, value, "found" if ok else "not_found", at)
    second = link_versions(store, [store.resolve(p) for p in ids + fetched], at) if fetched else None
    out = {"linked": first["linked"] + (second["linked"] if second else 0),
           "confirmed": first["confirmed"], "refused": first["refused"] + (second["refused"] if second else 0),
           "unresolved": len(second["unresolved"] if second else first["unresolved"]),
           "looked_up": looked_up, "found": found, "errors": errors}
    return out


__all__ = ["LINK_RECHECK_AFTER", "MAX_LOOKUPS", "OTHER_ROLE", "claims", "fetch_linked", "find_target", "link_versions"]
