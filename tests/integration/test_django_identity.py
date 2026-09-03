"""The Django milestone's actual test: index a real Django-shaped project
through the full pipeline (Python + Django, storage, identity resolution)
and ask the question that motivated building the framework enricher at all --

    "What parts of the system are affected if Payment.status changes?"

— answered entirely from the graph, no LLM involved. See
`tests/fixtures/django_basic/` for the project this indexes.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from hashira.adapters.django import DjangoAdapter, normalize
from hashira.adapters.python import PythonAdapter
from hashira.application import IndexingService
from hashira.core import EntityStatus, EntityType, RelationshipType, System
from hashira.storage.memory import MemoryDatabase
from hashira.storage.sqlite import SqliteDatabase

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "django_basic"

# Structural edges a real "what could this affect" query should not walk
# through -- containment isn't impact (spec §25). Matches core.relationships
# IMPACT_EDGES' own exclusion of CONTAINS/DEFINES.
_STRUCTURAL = {RelationshipType.CONTAINS, RelationshipType.DEFINES}


def _copy_fixture(tmp_path: Path) -> Path:
    dest = tmp_path / "project"
    shutil.copytree(FIXTURE, dest)
    return dest


def _service(db: MemoryDatabase | SqliteDatabase) -> IndexingService:
    return IndexingService(
        db.unit_of_work, [PythonAdapter()], normalize, framework_adapters=[DjangoAdapter()]
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
    system = System(name="Checkout", slug="checkout")
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()
    return system


def test_the_killer_test_what_is_affected_if_payment_status_changes(
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

    affected = _reverse_impact(entities, relationships, "payments.models.Payment.status")

    # Exactly the chain from the design discussion: the service that touches
    # the field directly, the view that calls it, and the test that calls it.
    assert affected == {
        "payments.services.PaymentService.process",
        "payments.views.CheckoutView.post",
        "payments.tests.test_checkout.TestCheckout.test_process_marks_payment_captured",
    }


def test_the_route_is_reachable_one_hop_beyond_the_affected_view(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    """The URL route isn't part of the *impact* set (it doesn't call
    anything -- it EXPOSES a view, spec §11), but it must still be one
    DEFINES-hop-then-EXPOSES away from the affected method, so a caller that
    wants "which endpoints are affected" can ask for it explicitly."""
    root = _copy_fixture(tmp_path)
    _service(db).index(root, system_id=system.id, revision="rev1")

    with db.unit_of_work() as uow:
        entities = uow.graph.find_entities(system.id, limit=10_000)
        by_qn = {e.qualified_name: e for e in entities}
        by_id = {e.id: e for e in entities}

        post_method = by_qn["payments.views.CheckoutView.post"]
        view_class = by_qn["payments.views.CheckoutView"]
        defines = [
            rel
            for e in entities
            for rel in uow.graph.get_relationships(
                e.id, direction="out", types=[RelationshipType.DEFINES]
            )
            if rel.target_entity_id == post_method.id
        ]
        assert any(rel.source_entity_id == view_class.id for rel in defines)

        exposes = uow.graph.get_relationships(
            view_class.id, direction="in", types=[RelationshipType.EXPOSES]
        )
        assert any(
            by_id[rel.source_entity_id].qualified_name == "payments.urls:checkout/"
            for rel in exposes
        )


def test_model_and_view_are_tagged_not_reclassified(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    root = _copy_fixture(tmp_path)
    _service(db).index(root, system_id=system.id, revision="rev1")

    with db.unit_of_work() as uow:
        entities = uow.graph.find_entities(system.id, limit=10_000)
    by_qn = {e.qualified_name: e for e in entities}

    payment = by_qn["payments.models.Payment"]
    assert payment.type is EntityType.SYMBOL
    assert payment.metadata["django_kind"] == "model"

    view = by_qn["payments.views.CheckoutView"]
    assert view.type is EntityType.SYMBOL
    assert view.metadata["django_kind"] == "view"

    route = by_qn["payments.urls:checkout/"]
    assert route.type is EntityType.INTERFACE


def test_unchanged_django_project_reindex_is_boring(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    """The same invariant already proven for plain Python: re-indexing an
    unchanged project produces zero new entities, zero supersessions --
    now including the Django-specific field/route/model/view entities."""
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
