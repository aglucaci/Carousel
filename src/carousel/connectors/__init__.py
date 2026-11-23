"""Literature source connectors. Phase 1: PubMed and OpenAlex."""

from carousel.connectors.base import (
    Budget,
    BudgetExceeded,
    Connector,
    ConnectorError,
    HttpClient,
    QueryCoverage,
    RecordingTransport,
    ReplayTransport,
    Request,
    Response,
    ResponseCache,
    SearchQuery,
    SearchResult,
)
from carousel.connectors.openalex import OpenAlexConnector
from carousel.connectors.pubmed import PubMedConnector

CONNECTORS = {c.name: c for c in (PubMedConnector, OpenAlexConnector)}

__all__ = [
    "CONNECTORS",
    "Budget",
    "BudgetExceeded",
    "Connector",
    "ConnectorError",
    "HttpClient",
    "OpenAlexConnector",
    "PubMedConnector",
    "QueryCoverage",
    "RecordingTransport",
    "ReplayTransport",
    "Request",
    "Response",
    "ResponseCache",
    "SearchQuery",
    "SearchResult",
]
