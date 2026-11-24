"""Running an extraction: read the text, check consent, call the provider, check grounding, cache.

:func:`extract_paper` reads the best text the store has for a paper, in this
order: text the user supplies (private), a full text kept under a reusable
licence, otherwise the title and abstract. The result says which
(``coverage``); an abstract-only extraction says so plainly, because methods,
limitations, and data statements are often only in the full text.

**Consent.** A ``local`` provider never sends text anywhere. An ``external``
one sends the passages to a model service, so it runs only with recorded
consent for that provider covering every class of text in the request:
``public`` (published abstracts, openly licensed full texts) and, separately,
``private`` (text the user supplied). Without it, :class:`ConsentRequired` is
raised **before** anything is sent.

**Cache.** Extractions are stored per paper, provider, model version, schema
version, and input hash, so the same text is never sent to the same model
twice; a new full text, a new model, or a new schema makes a new one.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

from carousel.summarization import passages as P
from carousel.summarization.grounding import cited, ground
from carousel.summarization.providers import ExtractionError
from carousel.summarization.schema import SCHEMA_VERSION, Coverage, Extraction, Passage, input_hash

TEXT_CLASSES = ("public", "private")


class ConsentRequired(PermissionError):
    """An external provider would receive text without recorded consent; nothing was sent."""

    def __init__(self, provider: str, missing: List[str]) -> None:
        self.provider, self.missing = provider, missing
        super().__init__(f"sending {' and '.join(missing)} text to the external provider {provider!r} needs consent "
                         f"first (carousel consent grant {provider} --text {' --text '.join(missing)})")


def gather(store: Any, paper_id: str, user_text: Optional[str] = None) -> Dict[str, Any]:
    """The passages to read and what they are: ``{passages, scope, sources, statement}``."""
    paper = store.get(paper_id)
    if paper is None:
        raise KeyError(paper_id)
    if user_text and user_text.strip():
        passages = P.from_user_text(user_text)
        return {"passages": passages, "scope": "user_text", "sources": [{"kind": "user_text", "private": True}],
                "statement": "Extracted from text you supplied (private). Nothing from other sources was read."}
    texts = [t for t in store.texts(paper.paper_id) if t.kind == "full_text"]
    if texts:
        t = texts[0]
        passages = P.from_abstract(None, paper.title) + P.from_sections(
            ((s.title, s.text) for s in t.sections), prefix="s")
        return {"passages": passages, "scope": "full_text",
                "sources": [{"kind": "full_text", "source": t.source, "source_id": t.source_id, "license": t.license,
                             "reuse": t.reuse}],
                "statement": f"Extracted from the full text ({t.source_id}, {t.license or 'licence not stated'})."}
    passages = P.from_abstract(paper.abstract, paper.title)
    statement = ("Extracted from the title and abstract only; no full text is available under a reusable licence. "
                 "Methods, limitations, and data availability are often reported only in the full text, so "
                 "“not reported” here means not reported in the abstract.")
    if len(passages) <= 1:
        statement = "No abstract is available; only the title was read. Most fields cannot be reported."
    return {"passages": passages, "scope": "abstract_only", "sources": [{"kind": "abstract"}], "statement": statement}


def extract_paper(store: Any, paper_id: str, provider: Any, *, user_text: Optional[str] = None,
                  at: Optional[dt.datetime] = None, refresh: bool = False) -> Extraction:
    """The grounded extraction of one paper, from the cache when the same text was read by the same model."""
    at = at or dt.datetime.now(dt.timezone.utc)
    paper_id = store.resolve(paper_id)
    src = gather(store, paper_id, user_text)
    passages: List[Passage] = src["passages"]
    digest = input_hash(passages)
    if not refresh:
        hit = store.get_extraction(paper_id, provider.name, provider.model_version, SCHEMA_VERSION, digest)
        if hit is not None:
            return hit
    classes = sorted({p.text_class for p in passages}) or ["public"]
    if provider.location == "external":
        missing = [c for c in classes if not store.has_consent(provider.name, c)]
        if missing:
            raise ConsentRequired(provider.name, missing)
    if not passages:
        raise ExtractionError(f"{paper_id} has no text to read (no title, abstract, or full text)")
    raw = provider.extract(passages)
    fields, rejected = ground(raw, passages)
    served = str(raw.get("model_version") or provider.model_version)
    result = Extraction(
        paper_id=paper_id, provider=provider.name, model_version=served, schema_version=SCHEMA_VERSION,
        input_sha256=digest, location=provider.location, text_classes=classes,  # type: ignore[arg-type]
        coverage=Coverage(scope=src["scope"], statement=src["statement"], sources=src["sources"], passages=len(passages)),
        fields=fields, passages=cited(fields, passages), rejected=rejected,
        created_at=at.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"))
    # Cached under the model asked for, so the next request finds it even when a fallback model answered.
    store.save_extraction(result, requested_model=provider.model_version)
    return result


__all__ = ["ConsentRequired", "TEXT_CLASSES", "extract_paper", "gather"]
