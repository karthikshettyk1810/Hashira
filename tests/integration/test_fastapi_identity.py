"""FastAPI milestone's own integration coverage: index the real
`tests/fixtures/fastapi_checkout/` project through the full pipeline
(Python + FastAPI, storage, identity resolution) and check the same
invariants already proven for Django -- a boring re-index stays boring, and
both storage backends agree. The actual cross-framework claim (the milestone's
real definition of done) lives in `test_cross_framework_equivalence.py`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from hashira.adapters.fastapi import FastAPIAdapter, normalize
from hashira.adapters.python import PythonAdapter
from hashira.application import IndexingService
from hashira.core import EntityStatus, EntityType, RelationshipType, System
from hashira.storage.memory import MemoryDatabase
from hashira.storage.sqlite import SqliteDatabase

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fastapi_checkout"


def _copy_fixture(tmp_path: Path) -> Path:
    dest = tmp_path / "project"
    shutil.copytree(FIXTURE, dest)
    return dest


def _service(db: MemoryDatabase | SqliteDatabase) -> IndexingService:
    return IndexingService(
        db.unit_of_work, [PythonAdapter()], normalize, framework_adapters=[FastAPIAdapter()]
    )


@pytest.fixture
def db() -> MemoryDatabase:
    return MemoryDatabase()


@pytest.fixture
def system(db: MemoryDatabase) -> System:
    system = System(name="Checkout", slug="checkout-fastapi")
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()
    return system


def test_the_checkout_route_reaches_the_service(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    root = _copy_fixture(tmp_path)
    result = _service(db).index(root, system_id=system.id, revision="rev1")
    assert result.errors == []

    with db.unit_of_work() as uow:
        entities = uow.graph.find_entities(system.id, limit=10_000)
        by_qn = {e.qualified_name: e for e in entities}

        route = by_qn["payments.routers.router:POST /payments/checkout/"]
        handler = by_qn["payments.routers.checkout"]
        service_method = by_qn["payments.services.PaymentService.process"]

        exposes = uow.graph.get_relationships(
            route.id, direction="out", types=[RelationshipType.EXPOSES]
        )
        assert any(r.target_entity_id == handler.id for r in exposes)

        calls = uow.graph.get_relationships(
            handler.id, direction="out", types=[RelationshipType.CALLS]
        )
        assert any(r.target_entity_id == service_method.id for r in calls)


def test_the_me_route_depends_on_get_current_user(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    root = _copy_fixture(tmp_path)
    _service(db).index(root, system_id=system.id, revision="rev1")

    with db.unit_of_work() as uow:
        entities = uow.graph.find_entities(system.id, limit=10_000)
        by_qn = {e.qualified_name: e for e in entities}

        handler = by_qn["payments.routers.me"]
        dependency = by_qn["payments.dependencies.get_current_user"]

        depends_on = uow.graph.get_relationships(
            handler.id, direction="out", types=[RelationshipType.DEPENDS_ON]
        )
        assert any(r.target_entity_id == dependency.id for r in depends_on)


def test_model_and_handler_are_tagged_not_reclassified(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    root = _copy_fixture(tmp_path)
    _service(db).index(root, system_id=system.id, revision="rev1")

    with db.unit_of_work() as uow:
        entities = uow.graph.find_entities(system.id, limit=10_000)
    by_qn = {e.qualified_name: e for e in entities}

    handler = by_qn["payments.routers.checkout"]
    assert handler.type is EntityType.SYMBOL
    assert handler.metadata["fastapi_kind"] == "route_handler"

    schema = by_qn["payments.schemas.PaymentResponse"]
    assert schema.type is EntityType.SYMBOL
    assert schema.metadata["fastapi_kind"] == "schema"

    route = by_qn["payments.routers.router:POST /payments/checkout/"]
    assert route.type is EntityType.INTERFACE


def test_unchanged_fastapi_project_reindex_is_boring(
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
