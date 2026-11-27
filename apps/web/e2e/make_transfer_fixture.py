"""Build ``fixtures/transfer.json``: For You and "Why this paper?" API responses for a run with Method Transfers.

    CAROUSEL_TEST_DATABASE_URL=postgresql+psycopg://... python apps/web/e2e/make_transfer_fixture.py

Runs the task-35 test scenario (``tests/test_transfer.py``: an influenza radar using deep mutational scanning, two
soil-microbiome papers that use it too, and the shared topic map), imports the store into the given **disposable**
Postgres (it is reset first), and saves what the API returns for the radar list, the ranked papers, and each paper's
detail and explanation, so the spec renders real responses.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "tests"))

from sqlalchemy.orm import Session  # noqa: E402

from carousel import Radar  # noqa: E402
from carousel.api import services  # noqa: E402
from carousel.core.config import RadarConfig  # noqa: E402
from carousel.database import importer, postgres  # noqa: E402
from test_transfer import EMB, RADAR, SCAN, build  # noqa: E402


def main() -> int:
    url = os.environ["CAROUSEL_TEST_DATABASE_URL"]
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "s.db"
        ids = build(db)
        Radar(RadarConfig.model_validate(RADAR)).scan(as_of=SCAN.date(), observed_at=SCAN, store=db, connectors={},
                                                     embedder=EMB, top_n=25)
        postgres.downgrade(url)
        postgres.upgrade(url)
        engine = postgres.engine(url)
        importer.import_store(db, engine)
        with Session(engine) as s:
            services.upsert_radar(s, RadarConfig.model_validate(RADAR))
            s.commit()
            papers = services.ranked_papers(s, "escape")
            out = {"radars": services.list_radars(s), "papers": papers,
                   "paper": {it["paper_id"]: services.paper_detail(s, it["paper_id"]) for it in papers["items"]},
                   "explanation": {it["paper_id"]: services.explanation(s, it["paper_id"], "escape") for it in papers["items"]},
                   "transfers": ids["transfers"], "near": ids["near"]}
        engine.dispose()
    path = HERE / "fixtures" / "transfer.json"
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(path, len(out["papers"]["items"]), "items")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
