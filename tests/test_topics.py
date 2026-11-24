"""Topics v1 (plan task 24): versioned topic models per radar.

Done when: the same papers and model version give the same topics; a fixture corpus with known groups is
recovered; a refit maps unchanged topics to themselves and reports splits and merges; `carousel topics` lists them.
The corpus is synthetic: groups with disjoint vocabularies, embedded with the deterministic hashing embedder.
"""

import contextlib
import datetime as dt
import io
import random
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from carousel.core.paper import SourceRecord
from carousel.database import PaperStore
from carousel.intelligence.topics import fit, key_terms, kmeans, relink, update_topics
from carousel.search.embeddings import HashingEmbedder

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 24, tzinfo=UTC)
EMB = HashingEmbedder()

GROUPS = {
    "flu": "influenza hemagglutinin antigenic drift vaccine strain neutralizing antibody ferret serology clade",
    "crispr": "crispr base editing guide rna cas9 off-target editing efficiency knock-in prime editor",
    "coral": "coral reef bleaching thermal stress symbiont zooxanthellae ocean warming reef recovery",
    "malaria": "plasmodium falciparum artemisinin resistance mosquito vector parasite clearance kelch13",
    "soil": "soil microbiome nitrogen fixation rhizosphere fungal network root exudate carbon",
}


def papers(group, n, start=0, seed=0):
    rng = random.Random(f"{group}:{seed}")
    words = GROUPS[group].split()
    out = []
    for i in range(start, start + n):
        title = " ".join(rng.sample(words, 6))
        abstract = " ".join(rng.choice(words) for _ in range(30))
        out.append((f"{group}_{i:03d}", title, abstract))
    return out


def corpus(rows):
    vecs = EMB.embed([f"{t} {a}" for _, t, a in rows])
    return [(pid, v, f"{t} {a}") for (pid, t, a), v in zip(rows, vecs)]


def purity(model, truth):
    groups = Counter()
    for pid, tid in model.members.items():
        if tid:
            groups[(tid, truth(pid))] += 1
    by_topic = Counter()
    for (tid, _), c in groups.items():
        by_topic[tid] = max(by_topic[tid], c)
    assigned = sum(1 for t in model.members.values() if t)
    return sum(by_topic.values()) / assigned


BASE = papers("flu", 14) + papers("crispr", 14) + papers("coral", 14)


class FitTests(unittest.TestCase):
    def test_known_groups_are_recovered_deterministically(self) -> None:
        a = fit("r", corpus(BASE), EMB.model_version, "2026-11-24T00:00:00Z")
        b = fit("r", list(reversed(corpus(BASE))), EMB.model_version, "2026-11-25T00:00:00Z")
        self.assertEqual(a.params["k"], 3)  # chosen by silhouette
        self.assertEqual(purity(a, lambda pid: pid.split("_")[0]), 1.0)
        self.assertEqual(sum(1 for t in a.members.values() if t is None), 0)
        self.assertEqual(a.classifier_version, b.classifier_version)  # same papers, same model: same version...
        self.assertEqual([(t.topic_id, t.label, t.size) for t in a.topics],
                         [(t.topic_id, t.label, t.size) for t in b.topics])  # ...and the same topics
        self.assertTrue(a.classifier_version.startswith("topics-1:"))
        labels = {t.label for t in a.topics}
        self.assertTrue(any("crispr" in l or "editing" in l for l in labels), labels)
        self.assertTrue(any("coral" in l or "reef" in l for l in labels), labels)

    def test_no_structure_no_topics(self) -> None:
        from carousel.intelligence.topics import NoTopicStructure

        rng = random.Random(7)
        vocab = " ".join(GROUPS.values()).split()
        noise = [(f"n{i}", " ".join(rng.sample(vocab, 8)), " ".join(rng.choice(vocab) for _ in range(40))) for i in range(40)]
        with self.assertRaisesRegex(NoTopicStructure, "don't separate into topics"):
            fit("r", corpus(noise), EMB.model_version, "t")
        self.assertEqual(len(fit("r", corpus(noise), EMB.model_version, "t", k=2).topics), 2)  # unless asked for k

    def test_too_few_papers(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 15 papers"):
            fit("r", corpus(papers("flu", 5)), EMB.model_version, "t")

    def test_assignment_and_unassigned(self) -> None:
        model = fit("r", corpus(BASE), EMB.model_version, "t")
        [(_, v, _)] = corpus(papers("coral", 1, start=500))
        tid, sim = model.assign(v)
        self.assertEqual({model.members[p] for p in model.members if p.startswith("coral")}, {tid})
        self.assertGreater(sim, 0.3)
        [(_, other, _)] = corpus([("x", "quantum chromodynamics lattice gauge", "gluon confinement hadron spectrum quark")])
        self.assertEqual(model.assign(other)[0], None)  # far from every topic: unassigned, not forced

    def test_key_terms_prefer_distinctive_words(self) -> None:
        terms = key_terms({"a": ["coral reef bleaching study", "coral reef warming", "reef bleaching coral"],
                           "b": ["base editing crispr", "crispr guide editing", "prime editing crispr"]})
        self.assertIn("coral reef", terms["a"][:2])
        self.assertNotIn("coral", terms["a"])  # covered by the pair
        self.assertIn("crispr", " ".join(terms["b"]))

    def test_kmeans_is_seeded(self) -> None:
        vecs = [v for _, v, _ in corpus(BASE)]
        self.assertEqual(kmeans(vecs, 3), kmeans(vecs, 3))


class RefitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old = fit("r", corpus(BASE), EMB.model_version, "t1")
        self.by_group = {g: self.old.members[f"{g}_000"] for g in ("flu", "crispr", "coral")}

    def test_unchanged_topics_keep_their_identity_and_a_new_one_appears(self) -> None:
        new = fit("r", corpus(BASE + papers("malaria", 14)), EMB.model_version, "t2", previous=self.old)
        self.assertNotEqual(new.classifier_version, self.old.classifier_version)
        rel = Counter(m["relation"] for m in new.mapping)
        self.assertEqual(rel, Counter({"same": 3, "new": 1}))
        for g, tid in self.by_group.items():
            self.assertEqual(new.members[f"{g}_005"], tid)  # same topic, same ID across versions
        [m] = [m for m in new.mapping if m["relation"] == "new"]
        self.assertEqual(new.members["malaria_000"], m["to_topic"])

    def test_merge_and_retire_are_reported(self) -> None:
        # Refit with k=2 on the same papers: two of the three topics have to merge.
        new = fit("r", corpus(BASE), EMB.model_version, "t2", previous=self.old, k=2)
        rel = Counter(m["relation"] for m in new.mapping)
        self.assertEqual((rel["merge"], rel["same"]), (2, 1))
        merged = {m["to_topic"] for m in new.mapping if m["relation"] == "merge"}
        self.assertEqual(len(merged), 1)
        self.assertNotIn(merged.pop(), self.by_group.values())  # a merged topic is a new identity

    def test_split_is_reported(self) -> None:
        # The old model saw coral and soil papers as one topic (k=2 over three groups) ...
        mixed = BASE[:14] + papers("coral", 14) + papers("soil", 14)
        old = fit("r", corpus(mixed), EMB.model_version, "t1", k=2)
        new = fit("r", corpus(mixed), EMB.model_version, "t2", previous=old, k=3)  # ... the refit separates them
        splits = [m for m in new.mapping if m["relation"] == "split"]
        if splits:  # which old topic held two groups depends on the seed; whichever it is must be reported
            self.assertEqual(len({m["from_topic"] for m in splits}), 1)
            self.assertEqual(len(splits), 2)
        else:
            self.assertEqual(Counter(m["relation"] for m in new.mapping)["merge"], 0)
            self.fail(f"no split reported: {new.mapping}")

    def test_without_shared_papers_centroids_decide(self) -> None:
        later = papers("flu", 14, start=100) + papers("crispr", 14, start=100) + papers("coral", 14, start=100)
        new = fit("r", corpus(later), EMB.model_version, "t2", previous=self.old)
        self.assertEqual(Counter(m["relation"] for m in new.mapping), Counter({"same": 3}))
        self.assertEqual({t.topic_id for t in new.topics}, set(self.by_group.values()))


class StoreTests(unittest.TestCase):
    def test_store_round_trip_scan_hook_and_cli(self) -> None:
        from carousel import cli

        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "s.db")
            with PaperStore(db) as st:
                ids = []
                for pid, title, abstract in BASE:
                    obs = st.observe(SourceRecord(source="pubmed", source_id=pid, title=title, abstract=abstract), T0)
                    ids.append(obs.paper.paper_id)
                run = {"run_id": "run_1", "radar_id": "r", "radar_name": "R", "config_hash": "x", "as_of": "2026-11-24",
                       "started_at": "2026-11-24T00:00:00.000000Z", "finished_at": "2026-11-24T00:00:00.000000Z",
                       "status": "ok", "ranking_version": "0", "candidates_version": "0", "model_version": None,
                       "mode": "balanced", "n_observed": len(ids), "n_candidates": 0, "coverage": [], "ranking": {}}
                st.save_run(run, [], [("pubmed", p, pid, "new_paper") for (p, _, _), pid in zip(BASE, ids)])

                first = update_topics(st, "r", EMB, ids, T0)
                self.assertEqual((first["status"], first["topics"]), ("fitted", 3))
                model = st.active_topic_model("r")
                self.assertEqual(model.classifier_version, first["classifier_version"])
                self.assertEqual(len(model.topics), 3)
                self.assertTrue(all(c > 0 for p, c in model.confidence.items() if model.members[p]))

                new_ids = []
                for pid, title, abstract in papers("coral", 3, start=200):
                    new_ids.append(st.observe(SourceRecord(source="pubmed", source_id=pid, title=title,
                                                           abstract=abstract), T0).paper.paper_id)
                again = update_topics(st, "r", EMB, ids + new_ids, T0 + dt.timedelta(days=1))
                self.assertEqual((again["status"], again["assigned"], again["already"]), ("assigned", 3, len(ids)))
                placed = st.paper_topics(model.classifier_version)
                coral = model.members[ids[28]]
                self.assertTrue(all(placed[p]["topic_id"] == coral and placed[p]["origin"] == "assigned" for p in new_ids))
                other = update_topics(st, "r", HashingEmbedder(dim=128), ids, T0)
                self.assertEqual(other["status"], "skipped")  # another embedder's vectors can't be compared

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["topics", "list", "r", "--store", db]), 0)
            text = out.getvalue()
            self.assertIn(model.classifier_version, text)
            self.assertIn("unassigned", text)
            self.assertEqual(sum(1 for line in text.splitlines() if line.rstrip().endswith(" papers") or " papers  " in line), 4)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["topics", "papers", "r", "--store", db, "--topic", coral]), 0)
            self.assertEqual(out.getvalue().count("\n"), 17)  # 14 fitted + 3 assigned
            def fit_cli():
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(cli.main(["topics", "fit", "r", "--store", db, "--embedder", "hashing"]), 0)
                return out.getvalue()

            self.assertIn("unchanged: " + model.classifier_version, fit_cli())  # same papers: no new version
            with PaperStore(db) as st:  # a second scan observes a new group of papers
                rows = [(pid, st.observe(SourceRecord(source="pubmed", source_id=pid, title=t, abstract=a), T0).paper.paper_id)
                        for pid, t, a in papers("malaria", 14)]
                st.save_run({**run, "run_id": "run_2", "started_at": "2026-11-25T00:00:00.000000Z"}, [],
                            [("pubmed", p, pid, "new_paper") for p, pid in rows])
            text = fit_cli()
            self.assertIn("fitted topics-1:", text)
            self.assertEqual(text.count(" same "), 3)
            self.assertEqual(text.count(" new "), 1)
            with PaperStore(db) as st:
                versions = st.topic_models("r")
                self.assertEqual([v["active"] for v in versions], [0, 1])  # earlier versions are kept
                self.assertEqual(versions[1]["previous_version"], model.classifier_version)
                self.assertEqual(st.active_topic_model("r").topic(coral).size, 14)  # the coral topic kept its ID


try:
    from test_api import ApiTestCase
except Exception:  # pragma: no cover - server extras missing
    ApiTestCase = None

if ApiTestCase is not None:
    class TopicApiTests(ApiTestCase):
        def test_topics_reach_the_api(self) -> None:
            from carousel.database.importer import import_store

            store = Path(self.tmp.name) / "engine.db"
            with PaperStore(store) as st:
                ids = [st.observe(SourceRecord(source="pubmed", source_id=pid, title=t, abstract=a), T0).paper.paper_id
                       for pid, t, a in BASE]
                run = {"run_id": "run_1", "radar_id": "r", "radar_name": "R", "config_hash": "x", "as_of": "2026-11-24",
                       "started_at": "2026-11-24T00:00:00.000000Z", "finished_at": "2026-11-24T00:00:00.000000Z",
                       "status": "ok", "ranking_version": "0", "candidates_version": "0", "model_version": None,
                       "mode": "balanced", "n_observed": len(ids), "n_candidates": 0, "coverage": [], "ranking": {}}
                st.save_run(run, [], [("pubmed", p, pid, "new_paper") for (p, _, _), pid in zip(BASE, ids)])
                self.assertEqual(update_topics(st, "r", EMB, ids, T0)["status"], "fitted")
                version = st.active_topic_model("r").classifier_version
            counts = import_store(store, self.app.state.engine)
            self.assertEqual((counts["topic_models"], counts["topics"], counts["paper_topics"]), (1, 3, 42))
            self.assertEqual(import_store(store, self.app.state.engine)["topics"], 3)  # idempotent
            body = self.get("/radars/r/topics").json()
            self.assertEqual((body["classifier_version"], body["k"], body["active"]), (version, 3, True))
            self.assertEqual(sorted(t["papers"] for t in body["topics"]), [14, 14, 14])
            self.assertTrue(all(t["topic_id"].startswith("tp_") and t["key_terms"] for t in body["topics"]))
            self.assertEqual(self.get("/radars/r/topics", version="nope").status_code, 404)


if __name__ == "__main__":
    unittest.main()
