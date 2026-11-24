"""The grounding check: every claim must cite passages that exist, with quotes found verbatim in them.

Applied to every provider's answer, local or external, before anything is
stored. A claim that fails is dropped and recorded in ``rejected`` with the
reason; a field left with no grounded claim becomes ``not_reported``. The
check is deterministic and provider-agnostic: a model is never trusted to
have cited correctly.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence, Tuple

from carousel.summarization.passages import by_id, normalized
from carousel.summarization.schema import FIELDS, MAX_CLAIMS, Claim, FieldResult, Passage, Rejected

MIN_QUOTE_CHARS = 3


def ground(raw: Mapping[str, Any], passages: Sequence[Passage]) -> Tuple[Dict[str, FieldResult], List[Rejected]]:
    """Check a provider's ``{"fields": {name: {"status", "claims": [...]}}}`` against the passages it was given."""
    index = by_id(passages)
    fields_in = (raw or {}).get("fields") or {}
    out: Dict[str, FieldResult] = {}
    rejected: List[Rejected] = []
    for name in fields_in:
        if name not in FIELDS:
            rejected.append(Rejected(field=name, claim={}, reason="unknown field"))
    for name in FIELDS:
        item = fields_in.get(name) or {}
        kept: List[Claim] = []
        for c in item.get("claims") or []:
            reason = _check(c, index)
            if reason:
                rejected.append(Rejected(field=name, claim=c if isinstance(c, dict) else {"value": str(c)}, reason=reason))
                continue
            claim = Claim.model_validate({"text": c["text"].strip(),
                                          "evidence": [{"passage_id": e["passage_id"], "quote": e["quote"].strip()}
                                                       for e in c["evidence"]]})
            if any(k.text == claim.text for k in kept):
                continue
            kept.append(claim)
        if len(kept) > MAX_CLAIMS:
            for c in kept[MAX_CLAIMS:]:
                rejected.append(Rejected(field=name, claim=c.model_dump(), reason=f"more than {MAX_CLAIMS} claims"))
            kept = kept[:MAX_CLAIMS]
        # A field whose every claim failed is not reported, as far as the text shows.
        out[name] = FieldResult(status="reported" if kept else "not_reported", claims=kept)
    return out, rejected


def _check(c: Any, index: Mapping[str, Passage]) -> str:
    if not isinstance(c, dict) or not isinstance(c.get("text"), str) or not c["text"].strip():
        return "claim has no text"
    evidence = c.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return "claim cites no passage"
    for e in evidence:
        if not isinstance(e, dict):
            return "malformed evidence"
        pid, quote = e.get("passage_id"), e.get("quote")
        if pid not in index:
            return f"cites a passage that does not exist: {pid!r}"
        if not isinstance(quote, str) or len(quote.strip()) < MIN_QUOTE_CHARS:
            return f"quote from {pid} is empty or too short"
        if normalized(quote).strip(" .") not in normalized(index[pid].text):
            return f"quote not found verbatim in {pid}"
    return ""


def cited(fields: Mapping[str, FieldResult], passages: Sequence[Passage]) -> List[Passage]:
    """Only the passages some claim cites, in input order."""
    used = {e.passage_id for f in fields.values() for c in f.claims for e in c.evidence}
    return [p for p in passages if p.id in used]


__all__ = ["MIN_QUOTE_CHARS", "cited", "ground"]
