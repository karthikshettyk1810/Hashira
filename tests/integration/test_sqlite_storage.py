"""The SQLite backend against the shared UnitOfWork conformance suite.

Spec §33: "Integration: Git → observations → IR → Postgres, incremental
indexing, snapshot creation." No Git or adapter exists yet, so this is the
slice that does exist — IR → SQLite → IR — held to the same conformance suite
as the in-memory backend (`tests/contract/test_memory_conformance.py`).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from hashira.ports import UnitOfWork
from hashira.storage.sqlite import SqliteDatabase
from tests.contract.uow_conformance import *  # noqa: F403


@pytest.fixture
def uow_factory() -> Callable[[], UnitOfWork]:
    database = SqliteDatabase(":memory:")
    return database.unit_of_work


@pytest.fixture
def uow_factory_file(tmp_path: Path) -> Callable[[], UnitOfWork]:
    """A second flavor of the fixture, file-backed, to prove the in-memory
    StaticPool trick in `SqliteDatabase` isn't hiding a real-file-only bug."""
    database = SqliteDatabase(tmp_path / "hashira.sqlite3")
    return database.unit_of_work


def test_file_backed_database_persists_across_unit_of_work_instances(
    uow_factory_file: Callable[[], UnitOfWork],
) -> None:
    from hashira.core import System

    system = System(name="File Backed", slug="file-backed")
    with uow_factory_file() as uow:
        uow.systems.save(system)
        uow.commit()

    with uow_factory_file() as uow:
        assert uow.systems.get(system.id) == system
