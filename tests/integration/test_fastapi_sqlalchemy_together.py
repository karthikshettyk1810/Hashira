"""The SQLAlchemy milestone's second definition-of-done check: the exact
same `SQLAlchemyAdapter` used in `test_sqlalchemy_identity.py` against a
framework-free fixture now plugs into the FastAPI fixture, unmodified --
proving a `DataAdapter` really does not care which, if any, `FrameworkAdapter`
produced the code it enriches (`ports/adapters.py::DataAdapter`'s own
docstring). No FastAPI-SQLAlchemy-specific code exists anywhere in either
adapter; the only new piece this needed was `adapters._compose.compose_normalizers`
-- generic infrastructure for layering more than one enricher onto one
`IndexingService` run, not glue between these two adapters specifically.

`tests/fixtures/fastapi_checkout/payments/services.py` already uses a real
SQLAlchemy model (`payments/db_models.py`, added for this milestone) for its
`Payment.status` handling -- the same `PaymentService.process` the earlier
FastAPI-only and cross-framework tests already exercise, now additionally
producing `sqlalchemy.*` observations when `SQLAlchemyAdapter` is wired in
alongside `FastAPIAdapter`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from hashira.adapters._compose import compose_normalizers
from hashira.adapters.fastapi import FastAPIAdapter
from hashira.adapters.fastapi.normalizer import enrich_normalized_run as enrich_fastapi
from hashira.adapters.python import PythonAdapter
from hashira.adapters.sqlalchemy import SQLAlchemyAdapter
from hashira.adapters.sqlalchemy.normalizer import enrich_normalized_run as enrich_sqlalchemy
from hashira.application import IndexingService
from hashira.core import EntityType, RelationshipType, System
from hashira.storage.memory import MemoryDatabase

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fastapi_checkout"

_STRUCTURAL = {RelationshipType.CONTAINS, RelationshipType.DEFINES}


def _copy_fixture(tmp_path: Path) -> Path:
    dest = tmp_path / "project"
    shutil.copytree(FIXTURE, dest)
    return dest


def _service(db: MemoryDatabase) -> IndexingService:
    normalize = compose_normalizers(enrich_fastapi, enrich_sqlalchemy)
    return IndexingService(
        db.unit_of_work,
        [PythonAdapter()],
        normalize,
        framework_adapters=[FastAPIAdapter()],
        data_adapters=[SQLAlchemyAdapter()],
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
    system = System(name="Checkout", slug="checkout-fastapi-sqlalchemy")
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()
    return system


def test_route_to_table_is_one_fully_connected_graph(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    """The graph this milestone was actually built to prove exists: a
    request enters through the FastAPI route, reaches the handler, calls
    the service, and the service's read/write of `Payment.status` is
    visible as a SQLAlchemy column -- all in one indexing run, one merged
    entity per underlying Python construct (not two, one per enricher)."""
    root = _copy_fixture(tmp_path)
    result = _service(db).index(root, system_id=system.id, revision="rev1")
    assert result.errors == []

    with db.unit_of_work() as uow:
        entities = uow.graph.find_entities(system.id, limit=10_000)
        by_qn = {e.qualified_name: e for e in entities}

        # Exactly one PaymentService.process entity -- not one per enricher.
        process_qn = "payments.services.PaymentService.process"
        assert len([e for e in entities if e.qualified_name == process_qn]) == 1

        route = by_qn["payments.routers.router:POST /payments/checkout/"]
        handler = by_qn["payments.routers.checkout"]
        service_method = by_qn["payments.services.PaymentService.process"]
        table = by_qn["payments"]
        column = by_qn["payments.status"]

        assert route.type is EntityType.INTERFACE
        assert table.type is EntityType.DATA_ENTITY

        exposes = uow.graph.get_relationships(
            route.id, direction="out", types=[RelationshipType.EXPOSES]
        )
        assert any(r.target_entity_id == handler.id for r in exposes)

        calls = uow.graph.get_relationships(
            handler.id, direction="out", types=[RelationshipType.CALLS]
        )
        assert any(r.target_entity_id == service_method.id for r in calls)

        writes = uow.graph.get_relationships(
            service_method.id, direction="out", types=[RelationshipType.WRITES]
        )
        assert any(r.target_entity_id == column.id for r in writes)

        contains = uow.graph.get_relationships(
            table.id, direction="out", types=[RelationshipType.CONTAINS]
        )
        assert any(r.target_entity_id == column.id for r in contains)

        relationships = [
            rel for e in entities for rel in uow.graph.get_relationships(e.id, direction="out")
        ]

    # The full chain, walked in one traversal from the route down to the
    # column -- no framework-specific knowledge in this traversal either.
    affected = _reverse_impact(entities, relationships, "payments.status")
    assert affected == {
        "payments.services.PaymentService.process",
        "payments.routers.checkout",
        "payments.routers.router:POST /payments/checkout/",
        "payments.tests.test_checkout.TestCheckout.test_process_marks_payment_captured",
    }


def test_no_duplicate_relationship_rows_from_composing_two_enrichers(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    """The actual risk `compose_normalizers` exists to close: naively
    running two enrichers' own `normalize()` independently and merging
    their output would mint two different ids for the same Python
    `PaymentService.process`, and the plain `CALLS`/`IMPORTS`/`DEFINES`
    edges Python's own normalizer produces would then appear twice."""
    root = _copy_fixture(tmp_path)
    _service(db).index(root, system_id=system.id, revision="rev1")

    with db.unit_of_work() as uow:
        entities = uow.graph.find_entities(system.id, limit=10_000)
        seen: set[tuple[str, str, str]] = set()
        for entity in entities:
            for rel in uow.graph.get_relationships(entity.id, direction="out"):
                if not rel.is_current:
                    continue
                key = (rel.source_entity_id, rel.target_entity_id, rel.type.value)
                assert key not in seen, f"duplicate relationship row: {key}"
                seen.add(key)


def test_model_and_handler_are_each_tagged_by_their_own_enricher(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    root = _copy_fixture(tmp_path)
    _service(db).index(root, system_id=system.id, revision="rev1")

    with db.unit_of_work() as uow:
        entities = uow.graph.find_entities(system.id, limit=10_000)
    by_qn = {e.qualified_name: e for e in entities}

    handler = by_qn["payments.routers.checkout"]
    assert handler.metadata["framework"] == "fastapi"
    assert handler.metadata["fastapi_kind"] == "route_handler"

    model = by_qn["payments.db_models.Payment"]
    assert model.metadata["framework"] == "sqlalchemy"
    assert model.metadata["sqlalchemy_kind"] == "model"
