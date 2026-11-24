"""Re-record the connector fixtures from the live APIs.

    python tests/fixtures/http/record_fixtures.py [pubmed] [openalex] [crossref]   # default: all

crossref records two files: a lookup of known DOIs (a retracted paper and its
notice, a plain paper, a paper with a preprint and that preprint, and an
unregistered DOI), and the lookup a scan of the pubmed/openalex fixtures makes.

Fixtures hold requests and responses only; API keys are never recorded. The
queries are deliberately tiny (a few results, small pages) so paging is
exercised while the files stay small.
"""

import datetime as dt
from pathlib import Path

import tempfile

from carousel.connectors import (CrossrefConnector, HttpClient, OpenAlexConnector, PubMedConnector, RecordingTransport,
                                 ReplayTransport, SearchQuery)
from carousel.connectors.base import UrllibTransport

HERE = Path(__file__).parent

KNOWN_DOIS = [
    "10.1016/S0140-6736(97)11096-0",   # retracted (Retraction Watch: retraction 2010, correction 2004)
    "10.1016/s0140-6736(10)60175-4",   # the retraction notice itself (update-to)
    "10.1038/nature12373",             # no updates
    "10.1021/acsomega.5c02989",        # has-preprint
    "10.26434/chemrxiv-2025-8hnzz",    # that preprint
    "10.9999/carousel-not-registered", # not a Crossref DOI
]

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


if __name__ == "__main__":
    import sys

    main(sys.argv[1:] or ["pubmed", "openalex", "crossref"])
