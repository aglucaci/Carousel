"""Literature source connectors.

``CONNECTORS`` are searched by a scan (PubMed, OpenAlex, Europe PMC, arXiv). ``VERIFIERS`` look up
the DOIs a scan found instead of searching (Crossref).
"""

from carousel.connectors.base import (
    Budget,
    BudgetExceeded,
    Connector,
    ConnectorError,
    CountResult,
    HttpClient,
    QueryCoverage,
    RecordingTransport,
    ExtendingTransport,
    ReplayTransport,
    Request,
    Response,
    ResponseCache,
    SearchQuery,
    SearchResult,
)
from carousel.connectors.arxiv import ArxivConnector
from carousel.connectors.crossref import CrossrefConnector
from carousel.connectors.europepmc import EuropePMCConnector
from carousel.connectors.openalex import OpenAlexConnector
from carousel.connectors.pubmed import PubMedConnector

CONNECTORS = {c.name: c for c in (PubMedConnector, OpenAlexConnector, EuropePMCConnector, ArxivConnector)}
VERIFIERS = {c.name: c for c in (CrossrefConnector,)}

__all__ = [
    "CONNECTORS",
    "VERIFIERS",
    "ArxivConnector",
    "CrossrefConnector",
    "EuropePMCConnector",
    "Budget",
    "BudgetExceeded",
    "Connector",
    "ConnectorError",
    "CountResult",
    "HttpClient",
    "OpenAlexConnector",
    "PubMedConnector",
    "QueryCoverage",
    "RecordingTransport",
    "ExtendingTransport",
    "ReplayTransport",
    "Request",
    "Response",
    "ResponseCache",
    "SearchQuery",
    "SearchResult",
]
