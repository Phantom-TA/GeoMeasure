from __future__ import annotations

from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Base


@lru_cache(maxsize=8)
def get_engine(url: str) -> Engine:
    """One engine per database URL per process (API process and each worker process)."""
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            # WAL lets the API read while a worker process writes
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
    return create_engine(url, pool_pre_ping=True)


def get_session_factory(url: str) -> sessionmaker[Session]:
    return sessionmaker(get_engine(url), expire_on_commit=False)


def init_db(url: str) -> None:
    Base.metadata.create_all(get_engine(url))
