"""Topic observations (plan task 25): weekly counts and denominators from source count queries.

Done when: recorded-HTTP fixture tests; a leakage test fails if counts_as_of(t) uses an observation made after t;
count requests only, budgeted.
"""

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from carousel.connectors import (ArxivConnector, CountResult, EuropePMCConnector, HttpClient, OpenAlexConnector,
                                 PubMedConnector, ReplayTransport, SearchQuery)
from carousel.connectors.arxiv import build_query as arxiv_query
from carousel.connectors.europepmc import build_query as epmc_query
from carousel.connectors.openalex import build_search
from carousel.connectors.pubmed import build_term
from carousel.core.config import RadarConfig
from carousel.database import PaperStore
from carousel.intelligence.observations import (FIELD, complete_weeks, counts_as_of, lag_profile, observe, queries_for,
                                                series)
from test_connectors import FIXTURES

sys.path.insert(0, str(FIXTURES))
from record_fixtures import OBS_RADAR, OBS_TODAY, OBS_TOPICS, OBS_WEEKS, obs_model  # noqa: E402

UTC = dt.timezone.utc
RADAR = RadarConfig.model_validate(OBS_RADAR)
T_OBS = dt.datetime(2026, 11, 23, 12, tzinfo=UTC)


class Scripted:
    """A counting source whose totals come from a function of (topic terms, week start)."""

    name = "pubmed"

    def __init__(self, counts, fail=()):
        self.counts, self.fail, self.calls = counts, set(fail), []

    def count(self, q: SearchQuery) -> CountResult:
        from carousel.connectors import ConnectorError

        self.calls.append((tuple(q.require), q.start))
        if q.start in self.fail:
            raise ConnectorError("HTTP 503")
        return CountResult(source="pubmed", native_query=build_term(q), date_field="pdat", start=q.start, end=q.end,
                           count=self.counts(tuple(q.require), q.start), requests=1, cost=1, cost_unit="requests")


def radar(sources=("pubmed",)):
    return RadarConfig.model_validate({**OBS_RADAR, "sources": list(sources)})


class QueryTests(unittest.TestCase):
    def test_require_adds_an_and_group_and_leaves_other_queries_alone(self) -> None:
        q = SearchQuery(terms=["immune escape", "antigenic drift"], exclude=["case report"], require=["sars cov", "spike"],
                        start=dt.date(2026, 11, 16), end=dt.date(2026, 11, 22))
        self.assertEqual(build_term(q), '((immune escape) OR (antigenic drift)) AND ((sars cov) OR (spike)) '
                                        'NOT ("case report"[Title/Abstract])')
        self.assertEqual(build_search(q), '("immune escape" OR "antigenic drift") AND ("sars cov" OR spike) NOT ("case report")')
        self.assertIn('AND (TITLE_ABS:"sars cov" OR TITLE_ABS:"spike")', epmc_query(q))
        self.assertIn('AND (ti:"sars cov" OR abs:"sars cov" OR ti:"spike" OR abs:"spike")', arxiv_query(q))
        plain = q.model_copy(update={"require": []})
        self.assertNotIn("sars", build_term(plain) + build_search(plain) + epmc_query(plain) + arxiv_query(plain))

    def test_weeks_and_queries(self) -> None:
        weeks = complete_weeks(OBS_TODAY, 3)  # 2026-11-23 is a Monday: the week ending yesterday is complete
        self.assertEqual(weeks[0], (dt.date(2026, 11, 16), dt.date(2026, 11, 22)))
        self.assertEqual(weeks[2], (dt.date(2026, 11, 2), dt.date(2026, 11, 8)))
        qs = queries_for(RADAR, obs_model(), weeks[0])
        self.assertEqual([t for t, _ in qs], [FIELD, "tp_sars", "tp_flu"])
        self.assertEqual(qs[1][1].require, OBS_TOPICS["tp_sars"])
        self.assertEqual((qs[0][1].require, qs[0][1].max_results), ([], 1))


class ObserveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.st = PaperStore(Path(self.tmp.name) / "s.db")

    def tearDown(self) -> None:
        self.st.close()
        self.tmp.cleanup()

    def test_append_only_and_as_of_rebuild(self) -> None:
        week = complete_weeks(OBS_TODAY, 1)[0][0]
        # The latest week is still being indexed: 60 papers when first counted, 100 a week later.
        late = {"n": 60}
        src = Scripted(lambda req, start: (late["n"] if start == week else 100) // (1 if not req else 4))
        t1 = dt.datetime(2026, 11, 23, 12, tzinfo=UTC)
        observe(self.st, radar(), obs_model(), {"pubmed": src}, at=t1, weeks=3, today=OBS_TODAY)
        late["n"] = 100
        t2 = t1 + dt.timedelta(days=7)
        observe(self.st, radar(), obs_model(), {"pubmed": src}, at=t2, weeks=3, today=OBS_TODAY)
        rows = self.st.topic_observations("escape")
        self.assertEqual(len(rows), 2 * 3 * 3)  # two observations of 3 weeks x (field + 2 topics): nothing replaced

        then, now = counts_as_of(rows, t1 + dt.timedelta(hours=1)), counts_as_of(rows, t2)
        self.assertEqual(then[("pubmed", FIELD, week)]["count"], 60)
        self.assertEqual(now[("pubmed", FIELD, week)]["count"], 100)
        self.assertEqual(counts_as_of(rows, t1 - dt.timedelta(seconds=1)), {})  # nothing was known before

        s = series(then, "pubmed", "tp_sars")
        self.assertEqual([x["week"] for x in s], ["2026-11-02", "2026-11-09", "2026-11-16"])
        self.assertEqual((s[-1]["count"], s[-1]["denominator"], s[-1]["share"]), (15, 60, 0.25))

        [lag] = lag_profile(rows, "pubmed")[2:]
        self.assertEqual(lag["week"], "2026-11-16")
        self.assertEqual([(o["days_after_week"], o["count"], o["share_of_latest"]) for o in lag["observations"]],
                         [(1, 60, 0.6), (8, 100, 1.0)])

    def test_leakage_guard(self) -> None:
        """A replay at t must not see any observation made after t, whatever order rows come in."""
        src = Scripted(lambda req, start: 10)
        t1, t2 = dt.datetime(2026, 11, 23, tzinfo=UTC), dt.datetime(2026, 12, 7, tzinfo=UTC)
        observe(self.st, radar(), obs_model(), {"pubmed": src}, at=t1, weeks=2, today=OBS_TODAY)
        observe(self.st, radar(), obs_model(), {"pubmed": Scripted(lambda req, start: 99)}, at=t2, weeks=2,
                today=dt.date(2026, 12, 7))
        rows = list(reversed(self.st.topic_observations("escape")))
        known = counts_as_of(rows, t2 - dt.timedelta(days=1))
        cutoff = (t2 - dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.assertTrue(known)
        for row in known.values():
            self.assertLessEqual(row["observed_at"], cutoff)  # the guard itself
            self.assertEqual(row["count"], 10)
        self.assertNotIn(("pubmed", FIELD, dt.date(2026, 11, 30)), known)  # observed only at t2

    def test_budget_newest_first_and_errors_recorded(self) -> None:
        weeks = complete_weeks(OBS_TODAY, 4)
        src = Scripted(lambda req, start: 5, fail={weeks[1][0]})
        stats = observe(self.st, radar(), obs_model(), {"pubmed": src}, at=T_OBS, weeks=4, today=OBS_TODAY, budget=7)
        self.assertEqual((stats["requests"], stats["truncated"], stats["errors"]), (7, True, 3))
        self.assertEqual(src.calls[0][1], weeks[0][0])  # newest week first
        rows = self.st.topic_observations("escape")
        self.assertEqual(len(rows), 7)
        failed = [r for r in rows if r["status"] == "error"]
        self.assertEqual({r["error"] for r in failed}, {"HTTP 503"})
        self.assertTrue(all(r["count"] is None for r in failed))
        self.assertNotIn(("pubmed", FIELD, weeks[1][0]), counts_as_of(rows, T_OBS))  # failures are never counts

    def test_only_sources_that_can_count(self) -> None:
        stats = observe(self.st, radar(("pubmed", "crossref")), obs_model(), {"pubmed": Scripted(lambda r, s: 1)},
                        at=T_OBS, weeks=1, today=OBS_TODAY)
        self.assertEqual(stats["sources"], ["pubmed"])


class RecordedTests(unittest.TestCase):
    """The same observation against the four real APIs (recorded). arXiv was rate-limiting during the recording
    (HTTP 429), so its failed requests are part of the fixture: they must be recorded, and never read as counts."""

    def test_counts_from_every_source(self) -> None:
        transport = ReplayTransport(FIXTURES / "observations.json")
        conns = {"pubmed": PubMedConnector(HttpClient(transport, sleep=lambda s: None), api_key=""),
                 "europepmc": EuropePMCConnector(HttpClient(transport, sleep=lambda s: None)),
                 "openalex": OpenAlexConnector(HttpClient(transport, sleep=lambda s: None), api_key=""),
                 "arxiv": ArxivConnector(HttpClient(transport, sleep=lambda s: None))}
        with tempfile.TemporaryDirectory() as d, PaperStore(Path(d) / "s.db") as st:
            stats = observe(st, RADAR, obs_model(), conns, at=T_OBS, weeks=OBS_WEEKS, today=OBS_TODAY)
            rows = st.topic_observations("escape")
        self.assertEqual((stats["requests"], stats["truncated"]), (4 * OBS_WEEKS * 3, False))
        self.assertEqual(len(rows), stats["requests"])  # every request is a row, failed or not
        known = counts_as_of(rows, T_OBS)
        for source in ("pubmed", "europepmc", "openalex"):
            with self.subTest(source=source):
                for topic in OBS_TOPICS:
                    s = series(known, source, topic)
                    self.assertEqual(len(s), OBS_WEEKS)
                    for x in s:
                        self.assertLessEqual(x["count"], x["denominator"])  # a topic is part of its field
                        self.assertGreaterEqual(x["count"], 0)
        failed = [r for r in rows if r["status"] == "error"]
        self.assertEqual({r["source"] for r in failed}, {"arxiv"})
        self.assertTrue(all("HTTP 429" in r["error"] and r["count"] is None for r in failed))
        self.assertFalse(any(known.get((r["source"], r["topic_id"], dt.date.fromisoformat(r["period_start"])))
                             for r in failed))  # a failure is never a count
        arxiv_ok = [r for r in rows if r["source"] == "arxiv" and r["status"] == "ok"]
        self.assertTrue(arxiv_ok and all(isinstance(r["count"], int) for r in arxiv_ok))
        field = [r for r in rows if r["topic_id"] == FIELD and r["source"] == "pubmed"]
        self.assertTrue(all(r["count"] > 0 for r in field))
        self.assertTrue(all(r["query"] and "spike" in r["query"] for r in rows if r["topic_id"] == "tp_sars" and r["query"]))
        self.assertEqual({r["classifier_version"] for r in rows}, {"topics-1:fixture"})


try:
    from test_api import ApiTestCase
except Exception:  # pragma: no cover - server extras missing
    ApiTestCase = None

if ApiTestCase is not None:
    class SeriesApiTests(ApiTestCase):
        def test_series_as_known_at_a_time(self) -> None:
            from carousel.database.importer import import_store
            from carousel.intelligence.topics import fit
            from test_topics import BASE, EMB, corpus

            store = Path(self.tmp.name) / "engine.db"
            with PaperStore(store) as st:
                ids = [st.observe(__import__("carousel.core.paper", fromlist=["SourceRecord"]).SourceRecord(
                    source="pubmed", source_id=p, title=t, abstract=a), T_OBS).paper.paper_id for p, t, a in BASE]
                run = {"run_id": "run_1", "radar_id": "escape", "radar_name": "Escape radar", "config_hash": "x",
                       "as_of": "2026-11-22", "started_at": "2026-11-23T00:00:00.000000Z",
                       "finished_at": "2026-11-23T00:00:00.000000Z", "status": "ok", "ranking_version": "0",
                       "candidates_version": "0", "model_version": None, "mode": "balanced", "n_observed": len(ids),
                       "n_candidates": 0, "coverage": [], "ranking": {}}
                st.save_run(run, [], [("pubmed", p, pid, "new_paper") for (p, _, _), pid in zip(BASE, ids)])
                model = fit("escape", corpus(BASE), EMB.model_version, "2026-11-23T00:00:00Z")
                model.members = {pid: model.members.get(p) for (p, _, _), pid in zip(BASE, ids)}
                st.save_topic_model(model)
                week = complete_weeks(OBS_TODAY, 1)[0][0]
                n = {"v": 40}
                src = Scripted(lambda req, start: n["v"] if not req else n["v"] // 4)
                observe(st, radar(), model, {"pubmed": src}, at=T_OBS, weeks=2, today=OBS_TODAY)
                n["v"] = 80
                observe(st, radar(), model, {"pubmed": src}, at=T_OBS + dt.timedelta(days=7), weeks=2, today=OBS_TODAY)
            counts = import_store(store, self.app.state.engine)
            self.assertEqual(counts["topic_observations"], 2 * 2 * (1 + len(model.topics)))
            self.assertEqual(import_store(store, self.app.state.engine)["topic_observations"], counts["topic_observations"])

            early = self.get("/radars/escape/topics/series", as_of="2026-11-24T00:00:00Z").json()
            late = self.get("/radars/escape/topics/series", as_of="2026-12-01T00:00:00Z").json()
            self.assertEqual(early["sources"], ["pubmed"])
            self.assertEqual([x["count"] for x in early["field"]["pubmed"]], [40, 40])
            self.assertEqual([x["count"] for x in late["field"]["pubmed"]], [80, 80])
            first = early["topics"][0]["series"]["pubmed"]
            self.assertEqual((first[-1]["week"], first[-1]["count"], first[-1]["denominator"], first[-1]["share"]),
                             (week.isoformat(), 10, 40, 0.25))
            self.assertEqual(self.get("/radars/escape/topics/series", as_of="2026-11-01T00:00:00Z").json()["sources"], [])


if __name__ == "__main__":
    unittest.main()
