"""Start the CAROUSEL API and a scan worker for the end-to-end test.

Playwright runs this (see playwright.config.ts). It resets a disposable
database, then serves the API with scans that replay the recorded HTTP
fixtures in ``e2e/fixtures/http`` (no network) and use the hashing embedder
(no model download).

Environment:

``CAROUSEL_E2E_DATABASE_URL``  Postgres with pgvector. **Wiped.** Its database
                               name must contain ``test`` or ``e2e``.
``CAROUSEL_E2E_REDIS_URL``     Default ``redis://127.0.0.1:6379/15``. **That
                               Redis database is flushed.**
``CAROUSEL_E2E_API_PORT``      Default 18765.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    from redis import Redis
    from sqlalchemy.engine import make_url

    from carousel.database import postgres

    url = os.environ.get("CAROUSEL_E2E_DATABASE_URL")
    if not url:
        print("set CAROUSEL_E2E_DATABASE_URL to a disposable Postgres database (it is wiped)", file=sys.stderr)
        return 2
    name = make_url(url).database or ""
    if "test" not in name and "e2e" not in name:
        print(f"refusing to wipe database {name!r}: its name must contain 'test' or 'e2e'", file=sys.stderr)
        return 2
    redis_url = os.environ.get("CAROUSEL_E2E_REDIS_URL", "redis://127.0.0.1:6379/15")
    port = os.environ.get("CAROUSEL_E2E_API_PORT", "18765")

    postgres.downgrade(url)
    postgres.upgrade(url)
    Redis.from_url(redis_url).flushdb()

    tmp = tempfile.TemporaryDirectory(prefix="carousel-e2e-")
    env = {k: v for k, v in os.environ.items()
           if k not in ("OPENALEX_API_KEY", "NCBI_API_KEY", "CAROUSEL_API_TOKEN")}
    env.update({
        "CAROUSEL_DATABASE_URL": url,
        "CAROUSEL_REDIS_URL": redis_url,
        "CAROUSEL_STORE": str(Path(tmp.name) / "engine.db"),
        "CAROUSEL_HTTP_REPLAY": str(HERE / "fixtures" / "http"),
        "CAROUSEL_EMBEDDER": "hashing",
    })
    procs = [
        subprocess.Popen([sys.executable, "-m", "carousel", "worker", "--simple"], env=env),
        subprocess.Popen([sys.executable, "-m", "carousel", "api", "serve", "--host", "127.0.0.1", "--port", port], env=env),
    ]

    def stop(*_: object) -> None:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        tmp.cleanup()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while all(p.poll() is None for p in procs):
        time.sleep(0.5)
    print("the API or the worker exited", file=sys.stderr)
    stop()
    return 1


if __name__ == "__main__":
    sys.exit(main())
