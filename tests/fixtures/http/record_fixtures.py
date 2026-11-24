"""Re-record the connector fixtures from the live APIs.

    python tests/fixtures/http/record_fixtures.py [pubmed] [openalex] [crossref] [europepmc] [europepmc-scan] [versions] [arxiv] [observations]   # default: all

crossref records two files: a lookup of known DOIs (a retracted paper and its
notice, a plain paper, a paper with a preprint and that preprint, and an
unregistered DOI), and the lookup a scan of the pubmed/openalex fixtures makes.

europepmc-scan re-records only ``europepmc_scan.json``, replaying the
requests it already holds and sending only new ones (a scan step added
later) to the API, so the existing responses stay put.

arxiv records ``arxiv_search.json`` (a paged search) and ``arxiv_extras.json``
(records by ID, one with a journal DOI, and Crossref's record of that DOI).

observations records ``observations.json``: weekly count requests (field and two
topics) from PubMed, Europe PMC, OpenAlex, and arXiv over four weeks.

versions records ``versions.json``: preprint/article pairs that Europe PMC and
Crossref link, fetched together and one side at a time.

Fixtures hold requests and responses only; API keys are never recorded. The
queries are deliberately tiny (a few results, small pages) so paging is
exercised while the files stay small.
"""

import datetime as dt
from pathlib import Path

import tempfile

from carousel.connectors import (ArxivConnector, CrossrefConnector, EuropePMCConnector, ExtendingTransport, HttpClient, OpenAlexConnector, PubMedConnector, RecordingTransport,
                                 ReplayTransport, SearchQuery)
from carousel.connectors.base import UrllibTransport

HERE = Path(__file__).parent

EPMC_RECORDS = ["MED:9500320"]                     # retracted (commentCorrectionList: "Retraction in", ...)
EPMC_FULL_TEXTS = ["PMC13778503",                   # open access, CC BY 4.0 in the JATS <license>
                   "PMC7092803"]                    # in PMC but not in the open-access subset (404)
EPMC_ANNOTATED = ["MED:43054306", "PMC:PMC13778503"]

# Preprint/article pairs Europe PMC links ("Preprint in" on the article, "Preprint of" on the preprint).
EPMC_ARTICLES = ["MED:42949957", "MED:43049340"]
EPMC_PREPRINTS = ["PPR:PPR1222416", "PPR:PPR1235797"]
CROSSREF_ARTICLE, CROSSREF_PREPRINT = "10.1021/acsomega.5c02989", "10.26434/chemrxiv-2025-8hnzz"  # has-preprint

KNOWN_DOIS = [
    "10.1016/S0140-6736(97)11096-0",   # retracted (Retraction Watch: retraction 2010, correction 2004)
    "10.1016/s0140-6736(10)60175-4",   # the retraction notice itself (update-to)
    "10.1038/nature12373",             # no updates
    "10.1021/acsomega.5c02989",        # has-preprint
    "10.26434/chemrxiv-2025-8hnzz",    # that preprint
    "10.9999/carousel-not-registered", # not a Crossref DOI
]

# arXiv: broader terms than QUERY so the window has enough preprints to page through.
ARXIV_QUERY = SearchQuery(terms=["immune escape", "antigenic drift", "viral evolution", "protein language model"],
                          exclude=["case report"], start=dt.date(2026, 10, 1), end=dt.date(2026, 10, 14), max_results=5)
ARXIV_RECORDS = ["1404.4197",   # v2; published as 10.1371/journal.ppat.1005526 (arxiv:doi)
                 "2610.01111"]  # v1; also in openalex_search.json under arXiv's DOI 10.48550/arxiv.2610.01111
ARXIV_JOURNAL_DOI = "10.1371/journal.ppat.1005526"

# Topic observations: a radar field, two topics' key terms, and four complete weeks before OBS_TODAY.
OBS_RADAR = {"radar_id": "escape", "radar_name": "Escape radar", "queries": ["immune escape", "antigenic drift"],
             "exclude": ["case report"], "sources": ["pubmed", "europepmc", "openalex", "arxiv"],
             "filters": {"date_range": "last_28_days"}}
OBS_TOPICS = {"tp_sars": ["sars cov", "spike", "omicron", "neutralizing"],
              "tp_flu": ["influenza", "hemagglutinin", "h3n2", "vaccine"]}
OBS_TODAY = dt.date(2026, 11, 23)
OBS_WEEKS = 4


def obs_model():
    from types import SimpleNamespace

    return SimpleNamespace(classifier_version="topics-1:fixture",
                           topics=[SimpleNamespace(topic_id=k, key_terms=v) for k, v in OBS_TOPICS.items()])


QUERY = SearchQuery(
    terms=["immune escape", "antigenic drift"],
    exclude=["case report"],
    start=dt.date(2026, 10, 1),
    end=dt.date(2026, 10, 14),
    max_results=5,
)


def main(sources) -> None:
    if "pubmed" in sources:
        rec = RecordingTransport(UrllibTransport(), HERE / "pubmed_search.json")
        pm = PubMedConnector(HttpClient(rec, min_interval=0.4), api_key="", page_size=3, batch_size=5)
        res = pm.search(QUERY)
        print("pubmed", res.coverage.total_available, len(res.records), rec.save())

    if "openalex" in sources:
        rec = RecordingTransport(UrllibTransport(), HERE / "openalex_search.json")
        oa = OpenAlexConnector(HttpClient(rec, min_interval=1.1), api_key="")
        res = oa.search(QUERY)
        print("openalex", res.coverage.total_available, len(res.records), res.coverage.rate_limit, rec.save())


    if "crossref" in sources:
        rec = RecordingTransport(UrllibTransport(), HERE / "crossref_lookup.json")
        cr = CrossrefConnector(HttpClient(rec, min_interval=0.5), mailto="")
        print("crossref lookup", len(cr.fetch(KNOWN_DOIS)), rec.save())

        # The lookup a scan makes: replay the search fixtures, record only Crossref.
        from carousel import Radar

        rec = RecordingTransport(UrllibTransport(), HERE / "crossref_scan.json")
        conns = {
            "pubmed": PubMedConnector(HttpClient(ReplayTransport(HERE / "pubmed_search.json")), api_key="",
                                      page_size=3, batch_size=5),
            "openalex": OpenAlexConnector(HttpClient(ReplayTransport(HERE / "openalex_search.json")), api_key=""),
            "crossref": CrossrefConnector(HttpClient(rec, min_interval=0.5), mailto=""),
        }
        from carousel.core.config import RadarConfig

        radar = Radar(RadarConfig.model_validate({
            "radar_id": "escape", "radar_name": "Escape radar", "sources": ["pubmed", "openalex", "crossref"],
            "queries": QUERY.terms, "exclude": QUERY.exclude, "filters": {"date_range": "last_14_days"}}))
        with tempfile.TemporaryDirectory() as d:
            res = radar.scan(as_of=QUERY.end, store=Path(d) / "s.db", connectors=conns, embedder="hashing",
                             max_results=QUERY.max_results)
        cov = [c for c in res.run["coverage"] if c["source"] == "crossref"][0]
        print("crossref scan", cov["total_available"], cov["retrieved"], rec.save())


    if "europepmc" in sources:
        rec = RecordingTransport(UrllibTransport(timeout=120), HERE / "europepmc_search.json")
        ep = EuropePMCConnector(HttpClient(rec, min_interval=0.3), page_size=3)
        res = ep.search(QUERY)
        print("europepmc search", res.coverage.total_available, len(res.records), rec.save())

        rec = RecordingTransport(UrllibTransport(timeout=120), HERE / "europepmc_extras.json")
        ep = EuropePMCConnector(HttpClient(rec, min_interval=0.3))
        print("  wakefield", [r.source_id for r in ep.fetch(EPMC_RECORDS)])
        print("  full texts", [(p, bool(ep.full_text(p))) for p in EPMC_FULL_TEXTS])
        print("  annotations", {k: len(v) for k, v in ep.annotations(EPMC_ANNOTATED).items()}, rec.save())

    if "europepmc" in sources or "europepmc-scan" in sources:
        # A scan's Europe PMC traffic: search, then full texts, annotations, and linked versions for its top 5.
        from carousel import Radar
        from carousel.core.config import RadarConfig

        path = HERE / "europepmc_scan.json"
        live = UrllibTransport(timeout=120)
        rec = RecordingTransport(ExtendingTransport(live, path) if "europepmc" not in sources else live, path)
        conns = {
            "pubmed": PubMedConnector(HttpClient(ReplayTransport(HERE / "pubmed_search.json")), api_key="",
                                      page_size=3, batch_size=5),
            "openalex": OpenAlexConnector(HttpClient(ReplayTransport(HERE / "openalex_search.json")), api_key=""),
            "europepmc": EuropePMCConnector(HttpClient(rec, min_interval=0.3), page_size=3),
        }
        radar = Radar(RadarConfig.model_validate({
            "radar_id": "escape", "radar_name": "Escape radar", "sources": ["pubmed", "openalex", "europepmc"],
            "queries": QUERY.terms, "exclude": QUERY.exclude, "filters": {"date_range": "last_14_days"}}))
        with tempfile.TemporaryDirectory() as d:
            res = radar.scan(as_of=QUERY.end, store=Path(d) / "s.db", connectors=conns, embedder="hashing",
                             max_results=QUERY.max_results, top_n=5)
        cov = [c for c in res.run["coverage"] if c["source"] == "europepmc"][0]
        print("europepmc scan", cov["retrieved"], cov.get("enrichment"), res.run["ranking"].get("versions"), rec.save())

    if "versions" in sources:
        rec = RecordingTransport(UrllibTransport(timeout=120), HERE / "versions.json")
        client = HttpClient(rec, min_interval=0.5)
        ep, cr = EuropePMCConnector(client), CrossrefConnector(client, mailto="")
        print("versions: both sides", [r.source_id for r in ep.fetch(EPMC_ARTICLES + EPMC_PREPRINTS)])
        print("  articles", [(r.source_id, [x.value for x in r.relations]) for r in ep.fetch(EPMC_ARTICLES)])
        print("  preprints", [(r.source_id, [x.value for x in r.relations]) for r in ep.fetch(EPMC_PREPRINTS)])
        for doi in (CROSSREF_ARTICLE, CROSSREF_PREPRINT):
            print("  crossref", [(r.source_id, [x.value for x in r.relations]) for r in cr.fetch([doi])])
        print("  ", rec.save())

    if "arxiv" in sources:
        rec = RecordingTransport(UrllibTransport(timeout=60), HERE / "arxiv_search.json")
        ax = ArxivConnector(HttpClient(rec, min_interval=3.1), page_size=2)
        res = ax.search(ARXIV_QUERY)
        print("arxiv search", res.coverage.total_available, len(res.records), res.coverage.pages, rec.save())

        rec = RecordingTransport(UrllibTransport(timeout=60), HERE / "arxiv_extras.json")
        client = HttpClient(rec, min_interval=3.1)
        ax, cr = ArxivConnector(client), CrossrefConnector(client, mailto="")
        print("  records", [(r.source_id, r.version, [x.value for x in r.relations]) for r in ax.fetch(ARXIV_RECORDS)])
        print("  journal", [r.source_id for r in cr.fetch([ARXIV_JOURNAL_DOI])], rec.save())

    if "observations" in sources:
        from carousel.core.config import RadarConfig
        from carousel.database import PaperStore
        from carousel.intelligence.observations import observe

        path = HERE / "observations.json"  # requests already recorded are replayed; delete the file to re-record all
        rec = RecordingTransport(ExtendingTransport(UrllibTransport(timeout=90), path), path)
        conns = {
            "pubmed": PubMedConnector(HttpClient(rec, min_interval=0.4), api_key=""),
            "europepmc": EuropePMCConnector(HttpClient(rec, min_interval=0.3)),
            "openalex": OpenAlexConnector(HttpClient(rec, min_interval=1.1), api_key=""),
            "arxiv": ArxivConnector(HttpClient(rec, min_interval=5.0)),  # arXiv answers 429 to bursts of retries
        }
        with tempfile.TemporaryDirectory() as d, PaperStore(Path(d) / "s.db") as st:
            stats = observe(st, RadarConfig.model_validate(OBS_RADAR), obs_model(), conns,
                            at=dt.datetime(2026, 11, 23, 12, tzinfo=dt.timezone.utc), weeks=OBS_WEEKS, today=OBS_TODAY)
            print("observations", stats, rec.save())
            for r in st.topic_observations("escape"):
                print("  ", r["source"], r["period_start"], r["topic_id"], r["count"], r["status"], r["error"] or "")


if __name__ == "__main__":
    import sys

    main(sys.argv[1:] or ["pubmed", "openalex", "crossref", "europepmc", "versions", "arxiv", "observations"])
