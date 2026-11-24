"""Shared connector machinery: HTTP with retries and pacing, an on-disk
response cache, a per-run spending budget, recorded-HTTP fixtures, and the
query / coverage models every connector speaks.

Secrets (API keys) are never part of a cache key, a fixture, or a coverage
record. Connectors pass them in ``Request.secret_params`` / ``secret_headers``,
which are sent but not stored.
"""

from __future__ import annotations

import abc
import dataclasses
import datetime as dt
import hashlib
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from carousel import __version__
from carousel.core.paper import SourceRecord, UTCDateTime

USER_AGENT = f"carousel/{__version__}"

KEPT_HEADERS = ("content-type", "retry-after")
"""Response headers kept in caches and fixtures (plus any ``x-ratelimit-*``)."""


class ConnectorError(RuntimeError):
    """A source could not be queried."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class BudgetExceeded(ConnectorError):
    """A request would exceed the run's spending budget for a source."""


# -------------------------
# HTTP primitives
# -------------------------

@dataclasses.dataclass(frozen=True)
class Request:
    url: str
    params: Tuple[Tuple[str, str], ...] = ()
    headers: Tuple[Tuple[str, str], ...] = ()
    secret_params: Tuple[Tuple[str, str], ...] = dataclasses.field(default=(), compare=False, repr=False)
    secret_headers: Tuple[Tuple[str, str], ...] = dataclasses.field(default=(), compare=False, repr=False)
    method: str = "GET"  # POST sends params form-encoded in the body (for queries too long for a URL)

    @classmethod
    def build(
        cls,
        url: str,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        secret_params: Optional[Mapping[str, Optional[str]]] = None,
        secret_headers: Optional[Mapping[str, Optional[str]]] = None,
        method: str = "GET",
    ) -> "Request":
        def norm(m: Optional[Mapping[str, Any]]) -> Tuple[Tuple[str, str], ...]:
            return tuple(sorted((k, str(v)) for k, v in (m or {}).items() if v is not None))

        if method not in ("GET", "POST"):
            raise ValueError(f"unsupported method {method}")
        return cls(url, norm(params), norm(headers), norm(secret_params), norm(secret_headers), method)

    def key(self) -> str:
        """Identity of the request for caching and fixtures. Excludes secrets."""
        k = self.url + ("?" + urllib.parse.urlencode(self.params) if self.params else "")
        return k if self.method == "GET" else f"{self.method} {k}"

    def full_url(self) -> str:
        if self.method != "GET":
            return self.url
        params = self.params + self.secret_params
        return self.url + ("?" + urllib.parse.urlencode(params) if params else "")

    def body(self) -> Optional[bytes]:
        if self.method == "GET":
            return None
        return urllib.parse.urlencode(self.params + self.secret_params).encode("utf-8")


@dataclasses.dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    headers: Mapping[str, str] = dataclasses.field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self) -> str:
        return self.body.decode("utf-8")

    def json(self) -> Any:
        return json.loads(self.body)

    def header(self, name: str) -> Optional[str]:
        return self.headers.get(name.lower())

    def rate_limit_headers(self) -> Dict[str, str]:
        return {k: v for k, v in self.headers.items() if k.startswith("x-ratelimit-")}


def _kept_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    lower = {k.lower(): v for k, v in headers.items()}
    return {k: v for k, v in sorted(lower.items()) if k in KEPT_HEADERS or k.startswith("x-ratelimit-")}


class Transport(abc.ABC):
    @abc.abstractmethod
    def send(self, request: Request) -> Response: ...


class UrllibTransport(Transport):
    """Real network access (stdlib)."""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def send(self, request: Request) -> Response:
        headers = {"User-Agent": USER_AGENT, **dict(request.headers), **dict(request.secret_headers)}
        body = request.body()
        if body is not None:
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        req = urllib.request.Request(request.full_url(), data=body, headers=headers, method=request.method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return Response(resp.status, resp.read(), _kept_headers(dict(resp.headers)))
        except urllib.error.HTTPError as exc:
            return Response(exc.code, exc.read() or b"", _kept_headers(dict(exc.headers or {})))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            hint = ""
            if isinstance(getattr(exc, "reason", exc), ssl.SSLCertVerificationError):
                hint = (" (Python could not verify the HTTPS certificate. On macOS python.org builds, run "
                        "'Install Certificates.command'; behind a proxy, set SSL_CERT_FILE to your CA bundle.)")
            raise ConnectorError(f"network error for {request.url}: {exc}{hint}") from exc


# -------------------------
# Recorded-HTTP fixtures
# -------------------------

class ReplayTransport(Transport):
    """Serves responses from a fixture file; any unrecorded request is an error (tests never hit the network)."""

    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self._queue: Dict[str, List[Dict[str, Any]]] = {}
        for item in data["interactions"]:
            self._queue.setdefault(item["request"], []).append(item["response"])
        self.sent: List[str] = []

    def send(self, request: Request) -> Response:
        key = request.key()
        self.sent.append(key)
        queue = self._queue.get(key)
        if not queue:
            known = "\n  ".join(sorted(self._queue)) or "(none)"
            raise AssertionError(f"no recorded response for\n  {key}\nin {self.path}; recorded:\n  {known}")
        r = queue.pop(0) if len(queue) > 1 else queue[0]
        return Response(r["status"], r["body"].encode("utf-8"), r.get("headers", {}))


class RecordingTransport(Transport):
    """Passes requests to ``inner`` and records them (without secrets) for :class:`ReplayTransport`."""

    def __init__(self, inner: Transport, path: Union[str, Path]) -> None:
        self.inner = inner
        self.path = Path(path)
        self.interactions: List[Dict[str, Any]] = []

    def send(self, request: Request) -> Response:
        resp = self.inner.send(request)
        self.interactions.append({
            "request": request.key(),
            "response": {"status": resp.status, "headers": _kept_headers(resp.headers), "body": resp.text()},
        })
        return resp

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"recorded_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "user_agent": USER_AGENT, "interactions": self.interactions}
        self.path.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        return self.path


class ExtendingTransport(Transport):
    """For re-recording: replays what ``path`` already holds and sends only new requests to ``live``.

    Wrapped in a :class:`RecordingTransport` writing to the same file, it adds a scan's new requests (e.g. a
    new lookup step) without changing the responses the existing tests rely on.
    """

    def __init__(self, live: Transport, path: Union[str, Path]) -> None:
        self.live = live
        self.replay = ReplayTransport(path) if Path(path).exists() else None
        self.new: List[str] = []

    def send(self, request: Request) -> Response:
        if self.replay is not None and self.replay._queue.get(request.key()):
            return self.replay.send(request)
        self.new.append(request.key())
        return self.live.send(request)


# -------------------------
# Cache and budget
# -------------------------

class ResponseCache:
    """Successful responses on disk, keyed by the (secret-free) request, with a time-to-live."""

    def __init__(self, directory: Union[str, Path], ttl: dt.timedelta = dt.timedelta(hours=12),
                 clock: Callable[[], float] = time.time) -> None:
        self.dir = Path(directory)
        self.ttl = ttl
        self.clock = clock

    def _path(self, request: Request) -> Path:
        h = hashlib.sha256(request.key().encode("utf-8")).hexdigest()
        return self.dir / h[:2] / f"{h}.json"

    def get(self, request: Request) -> Optional[Response]:
        path = self._path(request)
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if entry.get("key") != request.key() or self.clock() - entry["stored_at"] > self.ttl.total_seconds():
            return None
        return Response(entry["status"], entry["body"].encode("utf-8"), entry["headers"])

    def put(self, request: Request, response: Response) -> None:
        path = self._path(request)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"key": request.key(), "stored_at": self.clock(), "status": response.status,
                 "headers": _kept_headers(response.headers), "body": response.text()}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


class Budget:
    """Spending limit for one source in one run, in the source's own cost units (OpenAlex: credits)."""

    def __init__(self, limit: Optional[float] = None, unit: str = "requests") -> None:
        self.limit = limit
        self.unit = unit
        self.used = 0.0

    @property
    def remaining(self) -> Optional[float]:
        return None if self.limit is None else max(self.limit - self.used, 0.0)

    def ensure(self, cost: float) -> None:
        if self.limit is not None and self.used + cost > self.limit:
            raise BudgetExceeded(f"budget of {self.limit:g} {self.unit} would be exceeded ({self.used:g} used, next request costs {cost:g})")

    def charge(self, cost: float) -> None:
        self.used += cost


@dataclasses.dataclass
class ClientStats:
    requests: int = 0
    cache_hits: int = 0
    retries: int = 0
    cost: float = 0.0
    rate_limit: Dict[str, str] = dataclasses.field(default_factory=dict)


class HttpClient:
    """GET with caching, budget, pacing between requests, and retries on 429 / 5xx."""

    RETRY_STATUSES = (429, 500, 502, 503, 504)

    def __init__(
        self,
        transport: Optional[Transport] = None,
        cache: Optional[ResponseCache] = None,
        budget: Optional[Budget] = None,
        min_interval: float = 0.0,
        max_retries: int = 4,
        backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        cost_header: Optional[str] = None,
    ) -> None:
        self.transport = transport or UrllibTransport()
        self.cache = cache
        self.budget = budget or Budget()
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.backoff = backoff
        self.sleep = sleep
        self.clock = clock
        self.cost_header = cost_header
        self.stats = ClientStats()
        self._last_sent: Optional[float] = None

    def get(self, request: Request, cost: float = 1.0) -> Response:
        if self.cache is not None:
            hit = self.cache.get(request)
            if hit is not None:
                self.stats.cache_hits += 1
                return hit
        self.budget.ensure(cost)
        attempt = 0
        while True:
            self._pace()
            resp = self.transport.send(request)
            self.stats.requests += 1
            if resp.rate_limit_headers():
                self.stats.rate_limit = resp.rate_limit_headers()
            if resp.status in self.RETRY_STATUSES and attempt < self.max_retries:
                attempt += 1
                self.stats.retries += 1
                self.sleep(self._retry_delay(resp, attempt))
                continue
            break
        if not resp.ok:
            snippet = resp.body[:300].decode("utf-8", "replace")
            raise ConnectorError(f"HTTP {resp.status} from {request.url}: {snippet}", status=resp.status)
        charged = cost
        if self.cost_header and resp.header(self.cost_header):
            try:
                charged = float(resp.header(self.cost_header))  # type: ignore[arg-type]
            except ValueError:
                pass
        self.budget.charge(charged)
        self.stats.cost += charged
        if self.cache is not None:
            self.cache.put(request, resp)
        return resp

    def _pace(self) -> None:
        if self.min_interval and self._last_sent is not None:
            wait = self.min_interval - (self.clock() - self._last_sent)
            if wait > 0:
                self.sleep(wait)
        self._last_sent = self.clock()

    def _retry_delay(self, resp: Response, attempt: int) -> float:
        retry_after = resp.header("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except ValueError:
                pass
        return min(self.backoff * 2 ** (attempt - 1), 30.0)


# -------------------------
# Query and coverage models
# -------------------------

MAX_WINDOW_DAYS = 366
MAX_RESULTS_CAP = 2000


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SearchQuery(_Strict):
    """A bounded literature query. Unbounded (full-corpus) scans are not expressible."""

    terms: List[str] = Field(min_length=1, description="Any of these (OR)")
    exclude: List[str] = Field(default_factory=list, description="None of these (NOT)")
    start: dt.date
    end: dt.date
    max_results: int = Field(default=200, ge=1, le=MAX_RESULTS_CAP)
    native: Optional[str] = Field(
        default=None, description="Source-native query sent verbatim instead of building one from terms (radar source_queries)"
    )

    @field_validator("terms", "exclude")
    @classmethod
    def _clean(cls, v: List[str]) -> List[str]:
        out = [" ".join(t.split()) for t in v]
        if any(not t for t in out):
            raise ValueError("terms must be non-empty")
        return out

    @model_validator(mode="after")
    def _window(self) -> "SearchQuery":
        if self.end < self.start:
            raise ValueError("end is before start")
        if (self.end - self.start).days + 1 > MAX_WINDOW_DAYS:
            raise ValueError(f"date window is longer than {MAX_WINDOW_DAYS} days; CAROUSEL does not run full-corpus scans")
        return self

    @classmethod
    def from_radar(cls, radar: Any, as_of: Optional[dt.date] = None, max_results: int = 200) -> "SearchQuery":
        """Build the query for a :class:`~carousel.core.config.RadarConfig` over its date window."""
        end = as_of or dt.datetime.now(dt.timezone.utc).date()
        return cls(terms=list(radar.queries), exclude=list(radar.exclude),
                   start=end - dt.timedelta(days=radar.filters.days - 1), end=end, max_results=max_results)


class QueryCoverage(_Strict):
    """What one source returned for one query, and what it did not.

    Persisted with every run so a ranking can say which sources it saw and
    whether any of them was cut short.
    """

    source: str
    connector_version: str
    native_query: str = Field(description="The query exactly as sent to the source (secrets excluded)")
    date_field: str = Field(description="Which of the source's dates the window applies to")
    start: dt.date
    end: dt.date
    total_available: Optional[int] = Field(description="Matches the source reported, if it reports a count")
    retrieved: int
    truncated: bool
    truncation_reason: Optional[str] = Field(default=None, description="max_results | budget | no_api_key | error")
    pages: int
    requests: int
    cache_hits: int
    cost: float
    cost_unit: str
    rate_limit: Dict[str, str] = Field(default_factory=dict, description="Last X-RateLimit-* headers seen")
    errors: List[str] = Field(default_factory=list)
    retrieved_at: UTCDateTime


class SearchResult(_Strict):
    records: List[SourceRecord]
    coverage: QueryCoverage


class Connector(abc.ABC):
    """One literature source."""

    name: str
    version: str
    cost_unit: str = "requests"

    def __init__(self, client: HttpClient) -> None:
        self.client = client

    @abc.abstractmethod
    def search(self, query: SearchQuery) -> SearchResult: ...

    @abc.abstractmethod
    def fetch(self, ids: Sequence[str]) -> List[SourceRecord]:
        """Fetch records by this source's own IDs (for refresh and verification)."""

    def _coverage(self, query: SearchQuery, before: ClientStats, **kw: Any) -> QueryCoverage:
        s = self.client.stats
        return QueryCoverage(
            source=self.name,
            connector_version=self.version,
            start=query.start,
            end=query.end,
            requests=s.requests - before.requests,
            cache_hits=s.cache_hits - before.cache_hits,
            cost=round(s.cost - before.cost, 6),
            cost_unit=self.cost_unit,
            rate_limit=dict(s.rate_limit),
            retrieved_at=dt.datetime.now(dt.timezone.utc),
            **kw,
        )

    def _snapshot_stats(self) -> ClientStats:
        return dataclasses.replace(self.client.stats, rate_limit=dict(self.client.stats.rate_limit))
