"""Re-record the HTTP fixtures the end-to-end test replays. Needs the network.

    python apps/web/e2e/record_fixtures.py

It builds the radar exactly as the app's proposal step does for
``scenario.json``, scans PubMed and OpenAlex once (without API keys, as the
test does), and writes ``fixtures/http/{pubmed,openalex}.json``. No secrets
are recorded.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

from carousel.api.services import propose_radar
from carousel.connectors import HttpClient, OpenAlexConnector, PubMedConnector, RecordingTransport
from carousel.connectors.base import UrllibTransport
from carousel.core.config import RadarConfig
from carousel.radar import Radar

HERE = Path(__file__).resolve().parent


class _NoRadars:
    """Stands in for a database session: no radar IDs are taken yet."""

    def get(self, *_: object) -> None:
        return None


def main() -> int:
    sc = json.loads((HERE / "scenario.json").read_text())
    as_of = dt.date.fromisoformat(sc["as_of"])
    prop = propose_radar(_NoRadars(), topic=sc["topic"], window_days=sc["window_days"], as_of=as_of)  # type: ignore[arg-type]
    cfg = RadarConfig.model_validate(prop["config"])
    out = HERE / "fixtures" / "http"
    rec = {name: RecordingTransport(UrllibTransport(), out / f"{name}.json") for name in ("pubmed", "openalex")}
    conns = {
        "pubmed": PubMedConnector(HttpClient(rec["pubmed"], min_interval=0.4), api_key=""),
        "openalex": OpenAlexConnector(HttpClient(rec["openalex"], min_interval=1.1), api_key=""),
    }
    with tempfile.TemporaryDirectory() as d:
        res = Radar(cfg, store=Path(d) / "store.db").scan(
            as_of=as_of, max_results=sc["max_results"], top_n=sc["top_n"], connectors=conns, embedder="hashing")
    for r in rec.values():
        print("wrote", r.save(), f"({len(r.interactions)} requests)")
    print(res.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
