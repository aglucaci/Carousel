"""Build ``fixtures/explore.json``: the Explore API response from a real engine computation (no network).

    python apps/web/e2e/make_explore_fixture.py

The store is the recorded task-32 scan (tests/fixtures/http/citations.json: an OpenAlex search plus the works citing
two seed papers), with one saved paper and a two-topic model, exactly as ``tests/test_explore.py`` builds it. Next to
the graph the fixture keeps ``stored_citations``: every stored citation edge between two of the graph's papers, read
straight from the store's edge table, so the spec checks the drawing against the store and not only against itself.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "tests"))

from carousel.search.explore import store_subgraph  # noqa: E402
from test_explore import AS_OF, scanned_store  # noqa: E402

sys.path.insert(0, str(HERE.parents[2] / "tests" / "fixtures" / "http"))
from record_fixtures import CITATION_SEEDS  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        st = scanned_store(Path(d) / "s.db")
        graph = store_subgraph(st, "escape", CITATION_SEEDS, as_of=AS_OF)
        work = {n["paper_id"]: st.get(n["paper_id"]).identifiers.openalex for n in graph["nodes"]}
        by_work = {w: pid for pid, w in work.items() if w}
        stored = sorted({(by_work[e["citing"]], by_work[e["cited"]])
                         for e in st.db.execute("SELECT citing, cited FROM citation_edges WHERE observed_at <= ?",
                                                (AS_OF.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),))
                         if e["citing"] in by_work and e["cited"] in by_work})
        st.close()
    out = {"radars": [{"radar_id": "escape", "title": "Escape radar", "description": "", "state": "active", "n_runs": 1,
                       "created_at": "2026-10-15T06:00:00Z", "updated_at": "2026-10-15T06:00:00Z"}],
           "graph": graph, "stored_citations": [list(x) for x in stored]}
    path = HERE / "fixtures" / "explore.json"
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(path, len(graph["nodes"]), "nodes", len(graph["links"]), "links", len(stored), "stored citations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
