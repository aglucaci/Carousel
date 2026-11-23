"""Re-record the connector fixtures from the live APIs.

    python tests/fixtures/http/record_fixtures.py

Fixtures hold requests and responses only; API keys are never recorded. The
queries are deliberately tiny (a few results, small pages) so paging is
exercised while the files stay small.
"""

import datetime as dt
from pathlib import Path

from carousel.connectors import HttpClient, OpenAlexConnector, PubMedConnector, RecordingTransport, SearchQuery
from carousel.connectors.base import UrllibTransport

HERE = Path(__file__).parent

QUERY = SearchQuery(
    terms=["immune escape", "antigenic drift"],
    exclude=["case report"],
    start=dt.date(2026, 10, 1),
    end=dt.date(2026, 10, 14),
    max_results=5,
)


def main() -> None:
    rec = RecordingTransport(UrllibTransport(), HERE / "pubmed_search.json")
    pm = PubMedConnector(HttpClient(rec, min_interval=0.4), api_key="", page_size=3, batch_size=5)
    res = pm.search(QUERY)
    print("pubmed", res.coverage.total_available, len(res.records), rec.save())

    rec = RecordingTransport(UrllibTransport(), HERE / "openalex_search.json")
    oa = OpenAlexConnector(HttpClient(rec, min_interval=1.1), api_key="")
    res = oa.search(QUERY)
    print("openalex", res.coverage.total_available, len(res.records), res.coverage.rate_limit, rec.save())


if __name__ == "__main__":
    main()
