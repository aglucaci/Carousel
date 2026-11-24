"""The ``Radar`` API: one call runs retrieve -> dedup -> candidates -> rank -> persist.

.. code-block:: python

    from carousel import Radar

    radar = Radar.from_yaml("examples/viral_evolution.yml")      # or Radar(topic="viral evolution", mode="exploratory")
    results = radar.scan()                                       # queries sources, updates carousel.db, ranks, saves the run
    results.to_csv("recommendations.csv")
    results.delta()                                              # vs the previous run of this radar

Every scan is stored (``recommendation_runs`` / ``recommendation_items`` in
the store) with the source coverage, versions, weights, and each item's
features, so rescanning keeps history. Feedback in the radar's
``*.feedback.yml`` file is applied on every scan (:mod:`carousel.feedback`).
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple, Union

from carousel.connectors import CONNECTORS, VERIFIERS, Connector, ConnectorError, SearchQuery
from carousel.core.config import Mode, RadarConfig, load_radar
from carousel.core.paper import CanonicalPaper
from carousel.database.local import PaperStore, _iso
from carousel.ingestion.versions import fetch_linked, link_versions
from carousel.intelligence.events import events_for_run, paper_state, watched_papers
from carousel import feedback as fb
from carousel.ranking import rank
from carousel.ranking.explain import EXPLANATION_VERSION, explain
from carousel.search import Embedder, HashingEmbedder, candidates_for_radar
from carousel.search.candidates import CANDIDATES_VERSION

DEFAULT_STORE = "carousel.db"
EMBEDDER_CHOICES = ("auto", "semantic", "hashing", "none")

CSV_COLUMNS = (
    "rank", "score", "title", "authors", "venue", "earliest_public_date", "doi", "pmid", "pmcid", "openalex",
    "arxiv", "url", "relevance", "novelty", "method_match", "matched_terms", "methods_matched", "retrieved_by",
    "sources", "flags", "first_seen", "paper_id", "run_id",
)


def make_embedder(kind: Union[str, Embedder, None] = "auto") -> Tuple[Optional[Embedder], List[str]]:
    """Resolve an embedder choice. Returns (embedder, notes)."""
    if kind is None or isinstance(kind, Embedder):
        return kind, []
    if kind not in EMBEDDER_CHOICES:
        raise ValueError(f"embedder must be one of {', '.join(EMBEDDER_CHOICES)}")
    if kind == "none":
        return None, ["semantic retrieval off (--embedder none): lexical candidates only, novelty inactive"]
    if kind == "hashing":
        return HashingEmbedder(), ["using the non-semantic hashing embedder"]
    try:
        from carousel.search import SentenceTransformerEmbedder

        return SentenceTransformerEmbedder(), []
    except ImportError:
        if kind == "semantic":
            raise
        return HashingEmbedder(), [
            'semantic search unavailable (pip install "carousel-research[semantic]"); '
            "using the non-semantic hashing embedder"
        ]


def _paper_summary(p: CanonicalPaper) -> Dict[str, Any]:
    ids = p.identifiers
    return {
        "title": p.title, "authors": [a.name for a in p.authors[:3]] + (["et al."] if len(p.authors) > 3 else []),
        "venue": p.venue, "earliest_public_date": p.earliest_public_date, "url": p.url,
        "doi": ids.doi, "pmid": ids.pmid, "pmcid": ids.pmcid, "openalex": ids.openalex, "arxiv": ids.arxiv,
    }


def extract_recommended(st: PaperStore, paper_ids: Sequence[str], at: dt.datetime) -> Dict[str, Any]:
    """Run the local rule-based extractor on recommended papers (cached per text); never fails the scan."""
    from carousel.summarization import RuleBasedProvider, extract_paper

    provider = RuleBasedProvider()
    stats: Dict[str, Any] = {"provider": provider.name, "model_version": provider.model_version, "papers": 0,
                             "abstract_only": 0, "full_text": 0, "errors": []}
    for pid in paper_ids:
        try:
            e = extract_paper(st, pid, provider, at=at)
        except Exception as exc:  # one paper's text must not stop the scan
            stats["errors"].append(f"{pid}: {type(exc).__name__}: {exc}")
            continue
        stats["papers"] += 1
        stats[e.coverage.scope] = stats.get(e.coverage.scope, 0) + 1
    return stats


VERSION_FLAGS = {"published": "published_version", "preprint": "has_preprint"}
"""Flag on a recommended paper by the role of its linked version: a preprint since published, an article with
a preprint."""


def version_summaries(st: PaperStore, paper_id: str) -> List[Dict[str, Any]]:
    """The paper's linked versions, for a run's stored details: each stays its own paper."""
    out = []
    for link in st.version_links(paper_id):
        other = st.get(link["paper_id"])
        if other is None:
            continue
        out.append({"paper_id": other.paper_id, "role": link["role"], "title": other.title, "venue": other.venue,
                    "earliest_public_date": other.earliest_public_date, "doi": other.identifiers.doi,
                    "url": other.url, "linked_by": sorted({e.get("method", "source") for e in link["evidence"]})})
    return out


# -------------------------
# Results
# -------------------------

ANNOTATIONS_MAX_AGE = dt.timedelta(days=30)
TEXT_RECHECK_AFTER = dt.timedelta(days=30)  # a text not kept (licence) may be relicensed or join the OA subset


def enrich_from_europepmc(conn: Any, st: PaperStore, paper_ids: Sequence[str], at: dt.datetime) -> Dict[str, Any]:
    """Fetch full texts (open access, under a reusable licence) and annotations for ``paper_ids``.

    Bounded by the papers given (a scan passes its recommendations). Texts already kept or checked in the
    last :data:`TEXT_RECHECK_AFTER`, and annotations fetched in the last :data:`ANNOTATIONS_MAX_AGE`, are not
    fetched again. Errors are counted, not raised (and the text is tried again next scan).
    """
    stats: Dict[str, Any] = {"full_texts": 0, "full_texts_skipped_license": 0, "annotated": 0, "errors": []}
    articles: Dict[str, str] = {}
    for pid in paper_ids:
        paper = st.get(pid)
        if paper is None:
            continue
        pmc = paper.identifiers.pmcid
        checked = st.text_checked_at("europepmc", pmc) if pmc else None
        recent = checked is not None and at - checked <= TEXT_RECHECK_AFTER
        if pmc and paper.open_access and not st.has_text("europepmc", pmc) and not recent:
            try:
                text = conn.full_text(pmc, metadata_license=paper.license)
            except ConnectorError as exc:
                stats["errors"].append(str(exc))
            else:
                if text is None:  # not in the open-access subset, or not under a reusable licence
                    stats["full_texts_skipped_license"] += 1
                else:
                    st.save_text(paper.paper_id, text)
                    stats["full_texts"] += 1
                st.mark_text_checked("europepmc", pmc, "not_reusable" if text is None else "kept", at)
        refs = [r.source_id for r in paper.source("europepmc")]
        if not refs and paper.identifiers.pmid:
            refs = [f"MED:{paper.identifiers.pmid}"]
        for ref in refs[:1]:
            fetched = st.annotations_retrieved_at("europepmc", ref)
            if fetched is None or at - fetched > ANNOTATIONS_MAX_AGE:
                articles[ref] = paper.paper_id
    if articles:
        try:
            found = conn.annotations(articles)
        except ConnectorError as exc:
            stats["errors"].append(str(exc))
            found = {}
        for ref, pid in articles.items():
            st.save_annotations(pid, "europepmc", ref, found.get(ref, []), at)
            stats["annotated"] += 1 if found.get(ref) else 0
    return stats


@dataclasses.dataclass
class ScanResults:
    """A stored run, as returned by :meth:`Radar.scan` or :meth:`Radar.results`."""

    run: Dict[str, Any]
    previous: Optional[Dict[str, Any]] = None
    radar: Optional[Dict[str, Any]] = None  # config summary for reports (name, description, themes, ...)

    @property
    def run_id(self) -> str:
        return self.run["run_id"]

    @property
    def items(self) -> List[Dict[str, Any]]:
        return self.run["items"]

    @property
    def coverage(self) -> List[Dict[str, Any]]:
        return self.run["coverage"]

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        return iter(self.items)

    def paper_ids(self) -> List[str]:
        return [i["paper_id"] for i in self.items]

    def rows(self) -> List[Dict[str, Any]]:
        out = []
        for it in self.items:
            d, paper = it["details"], it["details"].get("paper", {})
            comp = it["components"]
            out.append({
                "rank": it["rank"], "score": round(it["score"], 4), "title": paper.get("title"),
                "authors": "; ".join(paper.get("authors") or []), "venue": paper.get("venue"),
                "earliest_public_date": paper.get("earliest_public_date"),
                **{k: paper.get(k) for k in ("doi", "pmid", "pmcid", "openalex", "arxiv", "url")},
                **{k: (None if comp.get(k) is None else round(comp[k], 4)) for k in ("relevance", "novelty", "method_match")},
                "matched_terms": "; ".join(d.get("matched_terms", [])),
                "methods_matched": "; ".join(d.get("methods_matched", [])),
                "retrieved_by": "; ".join(d.get("retrieved_by", [])),
                "sources": "; ".join(d.get("sources", [])),
                "flags": "; ".join(it["flags"]), "first_seen": d.get("first_seen"),
                "paper_id": it["paper_id"], "run_id": self.run_id,
            })
        return out

    def to_csv(self, path: Union[str, Path]) -> Path:
        p = Path(path)
        with open(p, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            w.writeheader()
            w.writerows(self.rows())
        return p

    def to_dict(self) -> Dict[str, Any]:
        out = {**self.run, "delta": self.delta()}
        if self.radar is not None:
            out["radar"] = self.radar
        return out

    def to_json(self, path: Optional[Union[str, Path]] = None) -> str:
        text = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
        if path is not None:
            Path(path).write_text(text + "\n", encoding="utf-8")
        return text

    def delta(self) -> Dict[str, Any]:
        """What changed in the recommendations since the previous run of this radar.

        (The full Research Delta — new publications, revisions, trends — is Phases 2-3.)
        """
        now = {i["paper_id"]: i["rank"] for i in self.items}
        if self.previous is None:
            return {"previous_run_id": None, "entered": sorted(now, key=now.get), "left": [], "moved": []}
        before = {i["paper_id"]: i["rank"] for i in self.previous["items"]}
        return {
            "previous_run_id": self.previous["run_id"],
            "entered": sorted((p for p in now if p not in before), key=now.get),
            "left": sorted((p for p in before if p not in now), key=before.get),
            "moved": [{"paper_id": p, "rank": now[p], "previous_rank": before[p], "change": before[p] - now[p]}
                      for p in sorted((p for p in now if p in before), key=now.get) if before[p] != now[p]],
        }

    def summary(self, top: int = 10) -> str:
        r = self.run
        lines = [f"run {r['run_id']}  radar={r['radar_id']}  as_of={r['as_of']}  status={r['status']}  "
                 f"mode={r['mode']}  ranking={r['ranking_version']}  model={r['model_version'] or '-'}"]
        for c in r["coverage"]:
            if c["status"] == "ok":
                trunc = f" (truncated: {c['truncation_reason']})" if c.get("truncated") else ""
                lines.append(f"  {c['source']:<9} {c['retrieved']} of {c['total_available']} retrieved{trunc}")
            else:
                lines.append(f"  {c['source']:<9} {c['status']}: {c.get('error') or c.get('note', '')}")
        lines.append(f"  observed {r['n_observed']} records -> {r['n_candidates']} candidates -> {len(self.items)} recommended")
        for n in r.get("notes", []):
            lines.append(f"  note: {n}")
        d = self.delta()
        if d["previous_run_id"]:
            lines.append(f"  vs previous run: {len(d['entered'])} entered, {len(d['left'])} left, {len(d['moved'])} moved")
        for it in self.items[:top]:
            title = (it["details"].get("paper", {}).get("title") or "")[:72]
            flags = f"  [{', '.join(it['flags'])}]" if it["flags"] else ""
            lines.append(f"  {it['rank']:>3}. {it['score']:.3f}  {title}  ({it['paper_id']}){flags}")
        return "\n".join(lines)


# -------------------------
# Radar
# -------------------------

class Radar:
    def __init__(
        self,
        config: Optional[RadarConfig] = None,
        *,
        topic: Optional[str] = None,
        queries: Optional[Sequence[str]] = None,
        sources: Sequence[str] = ("pubmed", "openalex"),
        mode: str = "balanced",
        days: int = 90,
        store: Union[str, Path, PaperStore] = DEFAULT_STORE,
        feedback_path: Optional[Union[str, Path]] = None,
        path: Optional[Union[str, Path]] = None,
    ) -> None:
        if config is None:
            if not topic:
                raise ValueError("give a RadarConfig, or topic= (and optionally queries=, sources=, mode=, days=)")
            config = RadarConfig.model_validate({
                "radar_id": "-".join("".join(ch if ch.isalnum() else " " for ch in topic.lower()).split())[:60] or "radar",
                "radar_name": topic, "sources": list(sources), "queries": list(queries or [topic]),
                "mode": mode, "filters": {"date_range": f"last_{days}_days"},
            })
        self.config = config
        self.path = Path(path) if path else None
        self.store = store
        self.feedback_path = Path(feedback_path) if feedback_path else (fb.default_path(self.path) if self.path else None)

    @classmethod
    def from_yaml(cls, path: Union[str, Path], **kw: Any) -> "Radar":
        return cls(load_radar(path), path=path, **kw)

    @property
    def radar_id(self) -> str:
        return self.config.radar_id or "radar"

    def summary(self) -> Dict[str, Any]:
        """What a report needs to describe the radar (no secrets, no file paths)."""
        c = self.config
        return {
            "radar_id": self.radar_id, "radar_name": c.radar_name, "description": c.description,
            "research_question": c.research_question, "sources": [s.value for s in c.sources],
            "queries": list(c.queries), "mode": c.mode.value, "window_days": c.filters.days,
            "themes": {k: (v.description or k) for k, v in c.signal_profile.items()},
            "config_hash": c.config_hash(),
        }

    def _open(self, store: Union[str, Path, PaperStore, None]) -> Tuple[PaperStore, bool]:
        store = store if store is not None else self.store
        if isinstance(store, PaperStore):
            return store, False
        return PaperStore(store), True

    def scan(
        self,
        *,
        as_of: Optional[dt.date] = None,
        observed_at: Optional[dt.datetime] = None,
        store: Union[str, Path, PaperStore, None] = None,
        connectors: Optional[Mapping[str, Connector]] = None,
        embedder: Union[str, Embedder, None] = "auto",
        top_n: int = 25,
        max_results: int = 200,
        mode: Optional[str] = None,
    ) -> ScanResults:
        """Query every source, update the store, rank, and save the run."""
        started = observed_at or dt.datetime.now(dt.timezone.utc)
        if started.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        as_of = as_of or started.astimezone(dt.timezone.utc).date()
        cfg = self.config if mode is None else self.config.model_copy(update={"mode": Mode(mode)})
        days = cfg.filters.days
        start = as_of - dt.timedelta(days=days - 1)
        emb, notes = make_embedder(embedder)
        conns = dict(connectors) if connectors is not None else {}
        st, owned = self._open(store)
        try:
            # ---- retrieve and observe ----
            coverage: List[Dict[str, Any]] = []
            observations: List[Tuple[str, str, str, str]] = []
            hits: Set[str] = set()
            verify = [s.value for s in cfg.sources if s.value in VERIFIERS]
            for src in cfg.sources:
                name = src.value
                if name in VERIFIERS:
                    continue  # runs after the search sources, on what they found
                conn = conns.get(name)
                if conn is None and connectors is None and name in CONNECTORS:
                    conn = conns[name] = CONNECTORS[name]()
                if conn is None:
                    coverage.append({"source": name, "status": "no_connector",
                                     "note": "no connector for this source yet (Europe PMC, arXiv, bioRxiv/medRxiv arrive in Phase 2)"})
                    continue
                native = cfg.source_queries.get(src)
                query = SearchQuery(terms=list(cfg.queries), exclude=list(cfg.exclude), start=start, end=as_of,
                                    max_results=max_results,
                                    native=" OR ".join(f"({q})" for q in native) if native else None)
                try:
                    res = conn.search(query)
                except ConnectorError as exc:
                    coverage.append({"source": name, "status": "error", "error": str(exc)})
                    continue
                coverage.append({"status": "ok", **res.coverage.model_dump(mode="json")})
                for rec in res.records:
                    obs = st.observe(rec, started)
                    observations.append((rec.source.value, rec.source_id, obs.paper.paper_id, obs.status))
                    hits.add(obs.paper.paper_id)
            hits = {st.resolve(pid) for pid in hits}  # follow any merges made later in this scan

            # ---- verify: look up the found papers' DOIs (Crossref: corrections, retractions, preprint links) ----
            for name in verify:
                conn = conns.get(name)
                if conn is None and connectors is None:
                    conn = conns[name] = VERIFIERS[name]()
                if conn is None:
                    coverage.append({"source": name, "status": "no_connector", "note": "not configured for this scan"})
                    continue
                dois = sorted({p.identifiers.doi for p in (st.get(pid) for pid in hits) if p and p.identifiers.doi})
                query = SearchQuery(terms=list(cfg.queries), start=start, end=as_of, max_results=max_results)
                try:
                    res = conn.lookup(query, dois)  # type: ignore[attr-defined]
                except ConnectorError as exc:
                    coverage.append({"source": name, "status": "error", "error": str(exc)})
                    continue
                coverage.append({"status": "ok", "role": "verification", **res.coverage.model_dump(mode="json")})
                for rec in res.records:
                    obs = st.observe(rec, started)
                    observations.append((rec.source.value, rec.source_id, obs.paper.paper_id, obs.status))
            hits = {st.resolve(pid) for pid in hits}  # a DOI lookup can merge papers too
            observations = [(s, i, st.resolve(pid), status) for s, i, pid, status in observations]

            # ---- link preprints and their publications that are both in the store (never merged) ----
            linked_before = link_versions(st, hits, started)

            # ---- feedback ----
            fb_data = fb.load(self.feedback_path) if self.feedback_path else {a: [] for a in fb.ACTIONS}
            feedback, state = fb.resolve(st, fb_data, source=str(self.feedback_path or "feedback"))
            st.sync_feedback(self.radar_id, state, started)
            if feedback.unresolved:
                notes.append(f"{len(feedback.unresolved)} feedback reference(s) not in the store yet: "
                             + ", ".join(feedback.unresolved[:5]))

            # ---- candidates and ranking ----
            window_start = dt.datetime.combine(start, dt.time(), tzinfo=dt.timezone.utc)
            pool = {p.paper_id: p for p in st.papers(seen_since=window_start)}
            for pid in hits:
                if pid not in pool:
                    p = st.get(pid)
                    if p is not None:
                        pool[pid] = p
            cands = candidates_for_radar(cfg, [pool[k] for k in sorted(pool)], as_of=as_of, store=st,
                                         embedder=emb, source_hits=hits)
            ranking = rank(cands, cfg, store=st, embedder=emb, top_n=top_n, feedback=feedback)

            # ---- enrich what was recommended: licensed full texts and annotations (Europe PMC) ----
            epmc = conns.get("europepmc")
            if epmc is not None and hasattr(epmc, "full_text"):
                stats = enrich_from_europepmc(epmc, st, [it.paper_id for it in ranking.items], started)
                for c in coverage:
                    if c["source"] == "europepmc" and c["status"] == "ok":
                        c["enrichment"] = stats

            # ---- grounded extraction of what was recommended: local rules only (nothing leaves; cached) ----
            extraction = extract_recommended(st, [it.paper_id for it in ranking.items], started)

            # ---- the other version of what was recommended, looked up where the store lacks it ----
            lookups = {k: v for k, v in conns.items() if k in {s.value for s in cfg.sources}}
            linked_after = fetch_linked(st, lookups, [it.paper_id for it in ranking.items], started)

            items = []
            for it in ranking.items:
                d = it.to_dict()
                d["details"]["paper"] = _paper_summary(st.get(it.paper_id))  # type: ignore[arg-type]
                d["details"]["versions"] = versions = version_summaries(st, it.paper_id)
                d["flags"] += [f for f in dict.fromkeys(VERSION_FLAGS[v["role"]] for v in versions) if f not in d["flags"]]
                d["details"]["explanation"] = explain(d, ranking.weights, ranking.inactive_signals)
                items.append(d)
            ranking_meta = {k: v for k, v in ranking.to_dict().items() if k != "items"}
            ranking_meta["extraction"] = extraction
            ranking_meta["versions"] = {
                "linked": linked_before["linked"] + linked_after["linked"],
                "refused": linked_before["refused"] + linked_after["refused"],
                "looked_up": linked_after["looked_up"], "found": linked_after["found"],
                "unresolved": linked_after["unresolved"], "errors": linked_after["errors"],
            }
            ranking_meta["window"] = {"start": start.isoformat(), "end": as_of.isoformat(), "days": days}

            started_iso = _iso(started)
            run_id = "run_" + hashlib.sha256(f"{self.radar_id}|{started_iso}|{cfg.config_hash()}".encode()).hexdigest()[:16]
            status = "ok" if all(c["status"] == "ok" for c in coverage) else "partial"
            run = {
                "run_id": run_id, "radar_id": self.radar_id, "radar_name": cfg.radar_name,
                "config_hash": cfg.config_hash(), "as_of": as_of.isoformat(), "started_at": started_iso,
                "finished_at": _iso(dt.datetime.now(dt.timezone.utc)), "status": status,
                "ranking_version": ranking.ranking_version, "candidates_version": CANDIDATES_VERSION,
                "model_version": ranking.model_version, "mode": ranking.mode, "explanation_version": EXPLANATION_VERSION,
                "n_observed": len(observations), "n_candidates": len(cands.candidates),
                "coverage": coverage, "ranking": ranking_meta, "notes": notes,
            }
            st.save_run(run, items, observations)

            # ---- Research Delta: snapshot the watched papers, then derive this run's events from stored runs ----
            earlier = [st.get_run(r["run_id"]) for r in st.list_runs(self.radar_id) if r["started_at"] < started_iso]
            followed = [pid for pid, action in state if action in fb.POSITIVE]
            states = {}
            for pid in sorted({st.resolve(p) for p in watched_papers(earlier, items, followed)}):
                paper = st.get(pid)
                if paper is not None:
                    states[pid] = paper_state(paper, st.version_links(pid))
            st.save_paper_states(run_id, states)
            st.save_events(events_for_run(st, run_id))
            prev_id = st.previous_run_id(self.radar_id, started_iso)
            saved = st.get_run(run_id)
            return ScanResults(saved, st.get_run(prev_id) if prev_id else None, self.summary())  # type: ignore[arg-type]
        finally:
            if owned:
                st.close()

    def runs(self, store: Union[str, Path, PaperStore, None] = None) -> List[Dict[str, Any]]:
        st, owned = self._open(store)
        try:
            return st.list_runs(self.radar_id)
        finally:
            if owned:
                st.close()

    def results(self, run_id: Optional[str] = None, store: Union[str, Path, PaperStore, None] = None) -> Optional[ScanResults]:
        """A stored run of this radar (the latest by default), with its predecessor for :meth:`ScanResults.delta`."""
        st, owned = self._open(store)
        try:
            runs = st.list_runs(self.radar_id)
            if not runs:
                return None
            run = st.get_run(run_id) if run_id else st.get_run(runs[-1]["run_id"])
            if run is None:
                return None
            prev = st.previous_run_id(self.radar_id, run["started_at"])
            return ScanResults(run, st.get_run(prev) if prev else None, self.summary())
        finally:
            if owned:
                st.close()
