"""The topic map shared across radars (plan task 34).

Done when: deterministic for the same papers and version; per-radar topics, observations, and trends are untouched
(their tests unchanged); refits map old→new as in task 24. Two radars share one field (coral reefs) and differ in the
others, so the shared map should put both radars' coral topics in one shared topic, far from the rest.
"""

import contextlib
import datetime as dt
import io
import tempfile
import unittest
from pathlib import Path

from carousel.core.paper import SourceRecord
from carousel.database.local import PaperStore
from carousel.intelligence.shared_topics import (SHARED_MAP, assign_to_shared, crosswalk, field_distance, fit_shared,
                                                 shared_paper_ids)
from carousel.intelligence.topics import update_topics
from test_topics import EMB, T0, papers

STAMP = "2026-11-25T00:00:00Z"
RADARS = {"a": [("flu", 0), ("crispr", 0), ("coral", 0)], "b": [("coral", 100), ("malaria", 0), ("soil", 0)]}


def add_radar(st: PaperStore, radar: str, groups, n: int = 14, day: int = 0) -> list:
    rows = [r for g, start in groups for r in papers(g, n, start=start)]
    ids = [st.observe(SourceRecord(source="pubmed", source_id=pid, title=t, abstract=a), T0).paper.paper_id
           for pid, t, a in rows]
    at = (T0 + dt.timedelta(days=day)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    run = {"run_id": f"run_{radar}_{day}", "radar_id": radar, "radar_name": radar, "config_hash": "x", "as_of": at[:10],
           "started_at": at, "finished_at": at, "status": "ok", "ranking_version": "0", "candidates_version": "0",
           "model_version": None, "mode": "balanced", "n_observed": len(ids), "n_candidates": 0, "coverage": [], "ranking": {}}
    st.save_run(run, [], [("pubmed", p, pid, "new_paper") for (p, _, _), pid in zip(rows, ids)])
    return ids


def build(path) -> PaperStore:
    st = PaperStore(path)
    for radar, groups in RADARS.items():
        ids = add_radar(st, radar, groups)
        update_topics(st, radar, EMB, ids, T0)  # each radar's own topics (task 24)
    return st


def group_of(st, pid):
    return st.get(pid).sources[0].source_id.split("_")[0]


class SharedMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.st = build(Path(self.tmp.name) / "s.db")

    def tearDown(self) -> None:
        self.st.close()
        self.tmp.cleanup()

    def test_fit_finds_the_union_of_fields_and_leaves_radar_models_alone(self) -> None:
        before = {r: (self.st.active_topic_model(r).classifier_version,
                      self.st.paper_topics(self.st.active_topic_model(r).classifier_version)) for r in RADARS}
        model, changed = fit_shared(self.st, EMB, STAMP)
        self.assertTrue(changed)
        self.assertEqual(len(model.topics), 5)  # flu, crispr, coral (both radars), malaria, soil
        self.assertEqual(model.params["corpus_radars"], {"a": 42, "b": 42})
        for t in model.topics:  # each shared topic is one field
            self.assertEqual(len({group_of(self.st, p) for p, tid in model.members.items() if tid == t.topic_id}), 1)
        after = {r: (self.st.active_topic_model(r).classifier_version,
                     self.st.paper_topics(self.st.active_topic_model(r).classifier_version)) for r in RADARS}
        self.assertEqual(after, before)  # per-radar models and assignments untouched
        self.assertEqual(self.st.active_topic_model(SHARED_MAP).classifier_version, model.classifier_version)

    def test_deterministic(self) -> None:
        model, _ = fit_shared(self.st, EMB, STAMP)
        again, changed = fit_shared(self.st, EMB, "2026-11-26T00:00:00Z")
        self.assertEqual((changed, again.classifier_version), (False, model.classifier_version))
        with tempfile.TemporaryDirectory() as d:  # the same papers in another store: the same version and topics
            other = build(Path(d) / "t.db")
            m2, _ = fit_shared(other, EMB, STAMP)
            other.close()
        self.assertEqual(m2.classifier_version, model.classifier_version)
        self.assertEqual([(t.topic_id, t.label) for t in m2.topics], [(t.topic_id, t.label) for t in model.topics])

    def test_crosswalk_and_field_distance(self) -> None:
        model, _ = fit_shared(self.st, EMB, STAMP)
        coral = {}
        for r in RADARS:
            cw = crosswalk(self.st, r)
            self.assertTrue(cw["comparable"])
            for row in cw["topics"]:
                self.assertEqual((row["relation"], row["placed"]), ("within", row["members"]))
                if "coral" in row["label"] or "reef" in row["label"]:
                    coral[r] = row
        self.assertEqual(coral["a"]["primary"], coral["b"]["primary"])  # both radars' coral topics: one shared topic
        d = field_distance(self.st, coral["a"]["radar_topic"], coral["a"]["radar_topic"], radar_id="a")
        self.assertEqual((d["distance"], d["same_topic"]), (0.0, True))
        pa = [p for p, t in model.members.items() if t and group_of(self.st, p) == "flu"][0]
        pb = [p for p, t in model.members.items() if t and group_of(self.st, p) == "soil"][0]
        pc = [p for p, t in model.members.items() if t and group_of(self.st, p) == "flu" and p != pa][0]
        far, near = field_distance(self.st, pa, pb), field_distance(self.st, pa, pc)
        self.assertEqual(near["distance"], 0.0)
        self.assertGreater(far["distance"], 0.3)
        self.assertEqual(far["a"]["basis"], "assigned")
        sym = field_distance(self.st, pb, pa)
        self.assertEqual(sym["distance"], far["distance"])

    def test_refit_maps_old_to_new_and_scans_assign(self) -> None:
        first, _ = fit_shared(self.st, EMB, STAMP)
        # a scan of radar b observes new soil papers: the hook places them on the shared map
        new = add_radar(self.st, "b", [("soil", 300)], n=3, day=1)
        hook = assign_to_shared(self.st, EMB, new, T0 + dt.timedelta(days=1))
        self.assertEqual((hook["assigned"], hook["classifier_version"]), (3, first.classifier_version))
        soil = {self.st.paper_topics(first.classifier_version)[p]["topic_id"] for p in new}
        self.assertEqual(len(soil), 1)
        # a third radar brings a new field; the refit keeps the five topics' identities and adds one
        add_radar(self.st, "c", [("soil", 500), ("flu", 500)], n=14, day=2)
        ids, from_radar = shared_paper_ids(self.st)
        self.assertEqual(set(from_radar), {"a", "b", "c"})
        second, changed = fit_shared(self.st, EMB, "2026-11-27T00:00:00Z")
        self.assertTrue(changed)
        self.assertEqual(second.previous_version, first.classifier_version)
        same = [m for m in second.mapping if m["relation"] == "same"]
        self.assertEqual(len(same), 5)
        self.assertEqual({m["from_topic"] for m in same}, {t.topic_id for t in first.topics})
        self.assertEqual({t.topic_id for t in second.topics}, {t.topic_id for t in first.topics})  # lineage kept

    def test_cli(self) -> None:
        from carousel import cli

        db = self.st.path
        self.st.close()

        def run(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(["topicmap", *argv, "--store", db])
            return code, out.getvalue()

        self.assertEqual(run("list"), (0, "no shared topic map yet: carousel topicmap fit\n"))
        code, text = run("fit", "--embedder", "hashing")
        self.assertEqual(code, 0, text)
        self.assertIn("5 shared topics from 84 papers (a: 42, b: 42)", text)
        code, text = run("crosswalk", "a")
        self.assertEqual((code, text.count("(within;")), (0, 3))
        self.st = PaperStore(db)
        model = self.st.active_topic_model(SHARED_MAP)
        flu = [p for p, t in model.members.items() if t and group_of(self.st, p) == "flu"][0]
        soil = [p for p, t in model.members.items() if t and group_of(self.st, p) == "soil"][0]
        code, text = run("distance", flu, soil)
        self.assertEqual(code, 0, text)
        self.assertIn("(assigned)", text)


if __name__ == "__main__":
    unittest.main()
