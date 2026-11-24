"""Build ``fixtures/whats-new.json``: What's New API responses from a real engine computation (no network).

    python apps/web/e2e/make_whats_new_fixture.py     # needs the [trends] extra

The topic events come from the task-28 test scenario (``tests/test_topic_events.py``), including a refit for a
landscape shift; one new publication carries its rule-based extraction's findings, and one paper gets a retraction
notice. Everything is shaped as ``GET /radars/{id}/events`` and ``GET /events/{id}`` return it.
"""

from __future__ import annotations

import datetime as dt
import json
import random
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "tests"))

from carousel.core.paper import SourceRecord  # noqa: E402
from carousel.intelligence.topic_events import derive_topic_events  # noqa: E402
from carousel.intelligence.topics import fit_radar  # noqa: E402
from carousel.summarization import RuleBasedProvider, extract_paper  # noqa: E402
from test_topic_events import AS_OF, EMB, RADAR, build  # noqa: E402
from test_topics import GROUPS  # noqa: E402


def api_event(e, findings=()):
    return {"event_id": e["event_id"], "type": e["type"], "kind": e["kind"], "radar_id": e["radar_id"],
            "run_id": e["run_id"] or None, "date": e["date"], "paper_id": e["paper_id"], "title": e["title"],
            "evidence": e["evidence"], "events_version": e["events_version"],
            "papers": e["evidence"].get("papers") or [], "findings": list(findings)}


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        st, model, crispr = build(Path(d))
        before = derive_topic_events(st, RADAR, AS_OF, "run_1")["events"]  # the scan before the refit
        rng = random.Random(9)
        words = GROUPS["malaria"].split()
        obs = []
        for i in range(14):
            rec = SourceRecord(source="pubmed", source_id=f"malaria{i}", title=" ".join(rng.sample(words, 6)),
                               abstract=" ".join(rng.choice(words) for _ in range(30)), dates={"published": "2026-10-15"})
            obs.append(("pubmed", rec.source_id, st.observe(rec, AS_OF).paper.paper_id, "new_paper"))
        st.save_run({"run_id": "run_2", "radar_id": "lab", "radar_name": "Lab", "config_hash": "x", "as_of": "2026-12-14",
                     "started_at": "2026-12-15T00:00:00.000000Z", "finished_at": "2026-12-15T00:00:00.000000Z",
                     "status": "ok", "ranking_version": "0", "candidates_version": "0", "model_version": None,
                     "mode": "balanced", "n_observed": len(obs), "n_candidates": 0, "coverage": [], "ranking": {}}, [], obs)
        fit_radar(st, "lab", EMB, "2026-12-15T00:00:00Z")
        after = derive_topic_events(st, RADAR, AS_OF, "run_2")["events"]  # after it: the landscape shift
        merged = {e["event_id"]: e for e in before + after}
        events = [api_event(e) for e in sorted(merged.values(), key=lambda e: (e["type"], e["event_id"]))]

        paper = st.observe(SourceRecord(
            source="pubmed", source_id="9001", identifiers={"pmid": "9001", "doi": "10.5555/carousel-demo"},
            title="Prime editing restores function in a mouse model of retinal degeneration",
            abstract="We aimed to test prime editing in vivo. We found that a single dose restored visual function in "
                     "12 of 14 mice. A limitation is the short follow-up.",
            dates={"published": "2026-12-08"}), AS_OF).paper
        ex = extract_paper(st, paper.paper_id, RuleBasedProvider(), at=AS_OF)
        sections = {p.id: p.section for p in ex.passages}
        findings = [{"text": c.text, "quote": c.evidence[0].quote, "section": sections.get(c.evidence[0].passage_id),
                     "provider": ex.provider, "coverage": ex.coverage.scope} for c in ex.fields["findings"].claims]
        pub = {"event_id": "ev_newpub_demo", "type": "new_publication", "kind": None, "radar_id": "lab", "run_id": "run_2",
               "date": "2026-12-15", "paper_id": paper.paper_id, "title": paper.title,
               "evidence": {"run_id": "run_2", "previous_run_id": "run_1", "rank": 3, "earliest_public_date": "2026-12-08",
                            "window": {"start": "2026-09-23", "end": "2026-12-14"}, "sources": ["pubmed"],
                            "doi": "10.5555/carousel-demo"},
               "events_version": "1", "papers": [{"paper_id": paper.paper_id, "title": paper.title,
                                                  "doi": "10.5555/carousel-demo", "earliest_public_date": "2026-12-08"}],
               "findings": findings}
        flu = st.get(st.find_source("pubmed", "flu0").paper_id)
        rev = {"event_id": "ev_retraction_demo", "type": "publication_revision", "kind": "retraction", "radar_id": "lab",
               "run_id": "run_2", "date": "2026-12-15", "paper_id": flu.paper_id, "title": flu.title,
               "evidence": {"run_id": "run_2", "previous_run_id": "run_1", "retracted": {"before": False, "now": True},
                            "notice": {"type": "retraction", "notice_doi": "10.5555/retraction-demo",
                                       "asserted_by": "retraction-watch", "via": "crossref"}},
               "events_version": "1", "papers": [{"paper_id": flu.paper_id, "title": flu.title, "doi": None,
                                                  "earliest_public_date": flu.earliest_public_date}], "findings": []}
        st.close()
    out = {"radars": [{"radar_id": "lab", "title": "Lab", "description": "", "state": "active", "n_runs": 2,
                       "created_at": "2026-12-01T00:00:00Z", "updated_at": "2026-12-15T00:00:00Z"}],
           "events": [pub, *events, rev]}
    path = HERE / "fixtures" / "whats-new.json"
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print("wrote", path, {e["type"] for e in out["events"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
