"""Engine and schema lifecycle for the SQLite backend (spec §4: local-first).

`pip install hashira && hashira index` is meant to work with zero
infrastructure — this is the file that makes that literally true: a
`SqliteDatabase` is a file path (or ``:memory:``) and nothing else.
"""

from __future__ import annotations

from pathlib import Path
from sqlite3 import Connection as DBAPIConnection

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.pool import ConnectionPoolEntry, StaticPool

from . import schema
from .repositories import SqliteUnitOfWork

__all__ = ["SqliteDatabase"]


def _enable_foreign_keys(dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
    """SQLite disables FK enforcement by default, per-connection. `event.listen`
    on "connect" is how that gets applied to every connection the pool opens,
    not just the first one."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.close()


class SqliteDatabase:
    """Owns one SQLite engine and hands out a fresh `SqliteUnitOfWork` per
    logical transaction. One instance per process is the expected lifetime;
    `unit_of_work()` is cheap and safe to call repeatedly.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) == ":memory:":
            # A bare in-memory SQLite connection is private to whichever
            # connection opened it; StaticPool keeps every `engine.connect()`
            # call sharing the *same* underlying connection, so state written
            # in one unit of work is visible to the next.
            self._engine = sa.create_engine(
                "sqlite:///:memory:",
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        else:
            self._engine = sa.create_engine(f"sqlite:///{Path(path)}")
        event.listen(self._engine, "connect", _enable_foreign_keys)
        schema.metadata.create_all(self._engine)

    def unit_of_work(self) -> SqliteUnitOfWork:
        """A fresh transactional boundary. Use one per indexing run or
        logical write — see `SqliteUnitOfWork`'s docstring for why forgetting
        to commit is safe rather than silently persisting."""
        return SqliteUnitOfWork(self._engine)

    def dispose(self) -> None:
        self._engine.dispose()
