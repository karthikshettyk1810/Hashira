"""The in-memory backend against the shared UnitOfWork conformance suite.

See `uow_conformance.py`'s module docstring for how the wildcard import works.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from hashira.ports import UnitOfWork
from hashira.storage.memory import MemoryDatabase
from tests.contract.uow_conformance import *  # noqa: F403

pytestmark = pytest.mark.contract


@pytest.fixture
def uow_factory() -> Callable[[], UnitOfWork]:
    database = MemoryDatabase()
    return database.unit_of_work
