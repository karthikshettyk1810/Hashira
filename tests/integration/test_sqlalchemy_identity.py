"""The SQLAlchemy milestone's own integration coverage: index the real
`tests/fixtures/sqlalchemy_basic/` project through the full pipeline (Python
+ SQLAlchemy as a `DataAdapter`, storage, identity resolution) -- with *no*
`FrameworkAdapter` configured at all, proving `SQLAlchemyAdapter` means the
same thing whether or not a web framework is even present -- and answer the
actual worked example from the design discussion this was built from:

    "What is affected if `payments.status` changes?"

entirely from the graph, no LLM involved.
`tests/integration/test_fastapi_sqlalchemy_together.py` then plugs this same
adapter, unmodified, into the FastAPI fixture.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from hashira.adapters.python import PythonAdapter
from hashira.adapters.sqlalchemy import SQLAlchemyAdapter, normalize
from hashira.application import IndexingService
from hashira.core import EntityStatus, EntityType, RelationshipType, System
from hashira.storage.memory import MemoryDatabase
from hashira.storage.sqlite import SqliteDatabase

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sqlalchemy_basic"

# Structural edges a "what could this affect" query should not walk through
# -- containment isn't impact (spec §25), matching every other impact test
# in this codebase.
_STRUCTURAL = {RelationshipType.CONTAINS, RelationshipType.DEFINES}


def _copy_fixture(tmp_path: Path) -> Path:
    dest = tmp_path / "project"
    shutil.copytree(FIXTURE, dest)
    return dest


def _service(db: MemoryDatabase | SqliteDatabase) -> IndexingService:
    """No `framework_adapters` at all -- the whole point of this test."""
    return IndexingService(
        db.unit_of_work, [PythonAdapter()], normalize, data_adapters=[SQLAlchemyAdapter()]
    )


def _reverse_impact(entities, relationships, start_qn: str) -> set[str]:  # type: ignore[no-untyped-def]
    by_qn = {e.qualified_name: e for e in entities}
    by_id = {e.id: e for e in entities}
    start = by_qn[start_qn]
    seen = {start.id}
    frontier = [start.id]
    while frontier:
        next_frontier = []
        for eid in frontier:
            for rel in relationships:
                if rel.type in _STRUCTURAL or not rel.is_current:
                    continue
                if rel.target_entity_id == eid and rel.source_entity_id not in seen:
                    seen.add(rel.source_entity_id)
                    next_frontier.append(rel.source_entity_id)
        frontier = next_frontier
    return {by_id[i].qualified_name for i in seen if i != start.id}


@pytest.fixture
def db() -> MemoryDatabase:
    return MemoryDatabase()


@pytest.fixture
def system(db: MemoryDatabase) -> System:
    system = System(name="Ledger", slug="ledger")
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()
    return system


def test_the_killer_test_what_is_affected_if_payments_status_changes(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    root = _copy_fixture(tmp_path)
    result = _service(db).index(root, system_id=system.id, revision="rev1")
    assert result.errors == []

    with db.unit_of_work() as uow:
        entities = uow.graph.find_entities(system.id, limit=10_000)
        relationships = [
            rel for e in entities for rel in uow.graph.get_relationships(e.id, direction="out")
        ]

    affected = _reverse_impact(entities, relationships, "payments.status")
    assert affected == {
        "payments.service.PaymentService.process",
        "payments.tests.test_service.TestPaymentService.test_process_marks_payment_captured",
    }


def test_table_identity_primary_keys_and_foreign_keys(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    root = _copy_fixture(tmp_path)
    _service(db).index(root, system_id=system.id, revision="rev1")

    with db.unit_of_work() as uow:
        entities = uow.graph.find_entities(system.id, limit=10_000)
        by_qn = {e.qualified_name: e for e in entities}

        accounts = by_qn["accounts"]
        payments = by_qn["payments"]
        assert accounts.type is EntityType.DATA_ENTITY
        assert payments.type is EntityType.DATA_ENTITY

        accounts_id = by_qn["accounts.id"]
        payments_id = by_qn["payments.id"]
        assert accounts_id.metadata["is_primary_key"] is True
        assert payments_id.metadata["is_primary_key"] is True

        account_class = by_qn["payments.models.Account"]
        payment_class = by_qn["payments.models.Payment"]
        maps_to = uow.graph.get_relationships(
            payment_class.id, direction="out", types=[RelationshipType.MAPS_TO]
        )
        assert any(r.target_entity_id == payments.id for r in maps_to)
        maps_to_accounts = uow.graph.get_relationships(
            account_class.id, direction="out", types=[RelationshipType.MAPS_TO]
        )
        assert any(r.target_entity_id == accounts.id for r in maps_to_accounts)

        fk_source = by_qn["payments.account_id"]
        fk_target = by_qn["accounts.id"]
        references = uow.graph.get_relationships(
            fk_source.id, direction="out", types=[RelationshipType.REFERENCES]
        )
        assert any(r.target_entity_id == fk_target.id for r in references)


def test_model_is_tagged_not_reclassified(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    root = _copy_fixture(tmp_path)
    _service(db).index(root, system_id=system.id, revision="rev1")

    with db.unit_of_work() as uow:
        entities = uow.graph.find_entities(system.id, limit=10_000)
    by_qn = {e.qualified_name: e for e in entities}

    model = by_qn["payments.models.Payment"]
    assert model.type is EntityType.SYMBOL
    assert model.metadata["sqlalchemy_kind"] == "model"


def test_unchanged_sqlalchemy_project_reindex_is_boring(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    root = _copy_fixture(tmp_path)
    service = _service(db)
    service.index(root, system_id=system.id, revision="rev1")

    with db.unit_of_work() as uow:
        before = {e.id for e in uow.graph.find_entities(system.id, limit=10_000)}

    second = service.index(root, system_id=system.id, revision="rev2")

    with db.unit_of_work() as uow:
        after_entities = uow.graph.find_entities(system.id, limit=10_000)
    after = {e.id for e in after_entities}

    assert before == after
    assert all(e.status is EntityStatus.ACTIVE for e in after_entities)
    assert second.entities_upserted == len(after)


def test_full_pipeline_matches_between_sqlite_and_memory_backends(
    tmp_path: Path, system: System
) -> None:
    root = _copy_fixture(tmp_path)

    memory_db = MemoryDatabase()
    with memory_db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()
    _service(memory_db).index(root, system_id=system.id, revision="rev1")

    sqlite_db = SqliteDatabase(":memory:")
    with sqlite_db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()
    _service(sqlite_db).index(root, system_id=system.id, revision="rev1")

    with memory_db.unit_of_work() as uow:
        memory_qns = {e.qualified_name for e in uow.graph.find_entities(system.id, limit=10_000)}
    with sqlite_db.unit_of_work() as uow:
        sqlite_qns = {e.qualified_name for e in uow.graph.find_entities(system.id, limit=10_000)}

    assert memory_qns == sqlite_qns
