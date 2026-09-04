"""Constructor-keyword writes (`Payment(status=x)`), proven through the
full indexing pipeline -- not just the adapter's own raw observations.
Impact Analysis v0.4's own definition of done for this specific widening:
a supported construct must produce the correct edge, and that edge must
behave exactly like any other structural relationship under reconciliation
(`application/indexing.py::_reconcile_relationships`) -- retracted
(`valid_until_revision` set) once the keyword argument that produced it
stops being observed, the same "boring re-index is boring" guarantee
`docs/ROADMAP.md`'s Python-adapter milestone already proved for ordinary
CALLS/IMPORTS/DEFINES edges.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hashira.adapters.python import PythonAdapter
from hashira.adapters.sqlalchemy import SQLAlchemyAdapter, normalize
from hashira.application import IndexingService
from hashira.core import RelationshipType, System
from hashira.storage.memory import MemoryDatabase


def _write(root: Path, relpath: str, content: str) -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _models(root: Path) -> None:
    _write(root, "payments/__init__.py", "")
    _write(
        root,
        "payments/models.py",
        "from sqlalchemy import Column, String\n"
        "from sqlalchemy.orm import declarative_base\n\n"
        "Base = declarative_base()\n\n\n"
        "class Payment(Base):\n"
        '    __tablename__ = "payments"\n\n'
        "    status = Column(String(20))\n",
    )


def _service(db: MemoryDatabase) -> IndexingService:
    return IndexingService(
        db.unit_of_work, [PythonAdapter()], normalize, data_adapters=[SQLAlchemyAdapter()]
    )


@pytest.fixture
def db() -> MemoryDatabase:
    return MemoryDatabase()


@pytest.fixture
def system(db: MemoryDatabase) -> System:
    system = System(name="Checkout", slug="checkout-constructor-writes")
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()
    return system


def test_constructor_write_edge_is_retracted_once_the_keyword_disappears(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    _models(tmp_path)
    _write(
        tmp_path,
        "payments/service.py",
        'from .models import Payment\n\n\ndef create():\n    return Payment(status="pending")\n',
    )
    service = _service(db)
    result_a = service.index(tmp_path, system_id=system.id, revision="rev1")
    assert result_a.errors == []

    with db.unit_of_work() as uow:
        entities = uow.graph.find_entities(system.id, limit=10_000)
        by_qn = {e.qualified_name: e for e in entities}
        accessor = by_qn["payments.service.create"]
        column = by_qn["payments.status"]
        writes_at_a = [
            r
            for r in uow.graph.get_relationships(accessor.id, direction="out")
            if r.type is RelationshipType.WRITES and r.target_entity_id == column.id
        ]
    assert len(writes_at_a) == 1
    assert writes_at_a[0].is_current

    # The keyword argument disappears -- the function now returns a bare
    # instance, no longer writing `status` at all.
    _write(
        tmp_path,
        "payments/service.py",
        "from .models import Payment\n\n\ndef create():\n    return Payment()\n",
    )
    result_b = service.index(tmp_path, system_id=system.id, revision="rev2")
    assert result_b.errors == []

    with db.unit_of_work() as uow:
        rel = uow.graph.get_relationships(accessor.id, direction="out")
        writes_at_b = [
            r for r in rel if r.type is RelationshipType.WRITES and r.target_entity_id == column.id
        ]
    assert len(writes_at_b) == 1
    assert writes_at_b[0].id == writes_at_a[0].id  # same row, not replaced
    assert not writes_at_b[0].is_current
    assert writes_at_b[0].valid_until_revision == "rev2"
