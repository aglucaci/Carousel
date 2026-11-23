"""Postgres helpers: engines and Alembic migrations (``pip install "carousel-research[server]"``).

    from carousel.database import postgres
    postgres.upgrade("postgresql+psycopg://user:pass@localhost/carousel")

The database URL can also come from ``CAROUSEL_DATABASE_URL``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def database_url(url: Optional[str] = None) -> str:
    url = url or os.environ.get("CAROUSEL_DATABASE_URL")
    if not url:
        raise ValueError("no database URL: pass --url or set CAROUSEL_DATABASE_URL")
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def engine(url: Optional[str] = None) -> Engine:
    return create_engine(database_url(url), future=True)


def alembic_config(url: Optional[str] = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", database_url(url).replace("%", "%%"))
    return cfg


def upgrade(url: Optional[str] = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: Optional[str] = None, revision: str = "base") -> None:
    command.downgrade(alembic_config(url), revision)


def current(url: Optional[str] = None) -> Optional[str]:
    from alembic.runtime.migration import MigrationContext

    with engine(url).connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()
