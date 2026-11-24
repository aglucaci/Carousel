"""Re-record the HTTP fixtures the end-to-end test replays. Needs the network.

    python apps/web/e2e/record_fixtures.py [--extend] [pubmed] [openalex] [europepmc] [crossref]   # default: all

It builds the radar exactly as the app's proposal step does for
``scenario.json``, scans once (without API keys, as the test does), and writes
``fixtures/http/<source>.json`` for the sources named. Sources not named are
replayed from their existing fixtures, so re-recording Crossref alone keeps
the same papers. With ``--extend``, the named sources replay what their
fixtures already hold and only requests the scan didn't make before (a new
step) go to the network. No secrets are recorded.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

from carousel import feedback as fb
from carousel.api.services import propose_radar
from carousel.connectors import (CrossrefConnector, EuropePMCConnector, ExtendingTransport, HttpClient, OpenAlexConnector, PubMedConnector, RecordingTransport,
                                 ReplayTransport)
from carousel.connectors.base import UrllibTransport
from carousel.core.config import RadarConfig
from carousel.radar import Radar

HERE = Path(__file__).resolve().parent


class _NoRadars:
    """Stands in for a database session: no radar IDs are taken yet."""

    def get(self, *_: object) -> None:
        return None


def main(record=("pubmed", "openalex", "europepmc", "crossref"), extend: bool = False) -> int:
    sc = json.loads((HERE / "scenario.json").read_text())
    as_of = dt.date.fromisoformat(sc["as_of"])
    prop = propose_radar(_NoRadars(), topic=sc["topic"], window_days=sc["window_days"], as_of=as_of)  # type: ignore[arg-type]
    cfg = RadarConfig.model_validate(prop["config"])
    out = HERE / "fixtures" / "http"
    def live(name: str):
        inner = UrllibTransport(timeout=120)
        return ExtendingTransport(inner, out / f"{name}.json") if extend else inner

    rec = {name: RecordingTransport(live(name), out / f"{name}.json") for name in record}

    def transport(name: str):
        return rec[name] if name in rec else ReplayTransport(out / f"{name}.json")

    conns = {
        "pubmed": PubMedConnector(HttpClient(transport("pubmed"), min_interval=0.4), api_key=""),
        "openalex": OpenAlexConnector(HttpClient(transport("openalex"), min_interval=1.1), api_key=""),
        "europepmc": EuropePMCConnector(HttpClient(transport("europepmc"), min_interval=0.3)),
        "crossref": CrossrefConnector(HttpClient(transport("crossref"), min_interval=0.5), mailto=""),
    }
    with tempfile.TemporaryDirectory() as d:
        # The test's two scans: the second follows dismissing the top paper, saving the second, and marking the
        # third relevant and the fourth not relevant, so it recommends (and enriches) papers the first did not.
        feedback = Path(d) / "feedback.yml"
        radar = Radar(cfg, store=Path(d) / "store.db", feedback_path=feedback)
        res = radar.scan(as_of=as_of, max_results=sc["max_results"], top_n=sc["top_n"], connectors=conns,
                         embedder="hashing")
        first, second, third, fourth = (it["paper_id"] for it in res.items[:4])
        fb.save(feedback, {**{a: [] for a in fb.ACTIONS}, "dismiss": [first], "save": [second], "relevant": [third],
                           "irrelevant": [fourth]}, cfg.radar_id)
        res = radar.scan(as_of=as_of, max_results=sc["max_results"], top_n=sc["top_n"], connectors=conns,
                         embedder="hashing")
    for r in rec.values():
        new = len(r.inner.new) if isinstance(r.inner, ExtendingTransport) else len(r.interactions)
        print("wrote", r.save(), f"({len(r.interactions)} requests, {new} from the network)")
    print(res.summary())
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--extend"]
    sys.exit(main(tuple(args) or ("pubmed", "openalex", "europepmc", "crossref"), extend="--extend" in sys.argv))
