"""`application.graph`: direct, id-keyed entity/relationship lookups --
the small primitives `hashira.mcp`'s `get_entity`/`get_relationships` tools
call straight through to."""

from __future__ import annotations

import pytest

from hashira.application.graph import get_entity, get_entity_at_revision, get_relationships
from hashira.core import (
    Entity,
    EntityType,
    Evidence,
    Origin,
    Relationship,
    RelationshipType,
    SourceRef,
    System,
)
from hashira.storage.memory import MemoryDatabase


@pytest.fixture
def db() -> MemoryDatabase:
    return MemoryDatabase()


@pytest.fixture
def system(db: MemoryDatabase) -> System:
    system = System(name="Checkout", slug="checkout-graph")
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()
    return system


def _entity(system: System, name: str) -> Entity:
    return Entity(system_id=system.id, type=EntityType.SYMBOL, name=name, qualified_name=name)


def _evidence(system: System) -> Evidence:
    return Evidence(
        system_id=system.id,
        origin=Origin.STATIC_ANALYSIS,
        source=SourceRef(provider="python-ast", reference="app.py"),
        summary="observed",
        locator="app.py:1",
    )


def _rel(
    system: System, source: Entity, target: Entity, type: RelationshipType, evidence: Evidence
) -> Relationship:
    return Relationship(
        system_id=system.id,
        source_entity_id=source.id,
        target_entity_id=target.id,
        type=type,
        origin=Origin.STATIC_ANALYSIS,
        evidence_ids=[evidence.id],
    )


# --- get_entity --------------------------------------------------------------


def test_get_entity_returns_the_stored_entity(db: MemoryDatabase, system: System) -> None:
    entity = _entity(system, "a")
    with db.unit_of_work() as uow:
        uow.graph.upsert_entities([entity])
        uow.commit()

    with db.unit_of_work() as uow:
        found = get_entity(uow, entity_id=entity.id)

    assert found is not None
    assert found.id == entity.id
    assert found.qualified_name == "a"


def test_get_entity_unknown_id_returns_none(db: MemoryDatabase, system: System) -> None:
    with db.unit_of_work() as uow:
        assert get_entity(uow, entity_id="ent_nonexistent") is None


# --- get_entity_at_revision ---------------------------------------------------


def test_get_entity_at_revision_reflects_first_seen_revision(
    db: MemoryDatabase, system: System
) -> None:
    entity = _entity(system, "a").model_copy(
        update={"first_seen_revision": "rev2", "last_seen_revision": "rev2"}
    )
    with db.unit_of_work() as uow:
        uow.graph.upsert_entities([entity])
        uow.commit()

    with db.unit_of_work() as uow:
        at_rev1 = get_entity_at_revision(
            uow, system_id=system.id, entity_id=entity.id, revision="rev1"
        )
        at_rev2 = get_entity_at_revision(
            uow, system_id=system.id, entity_id=entity.id, revision="rev2"
        )

    assert at_rev1 is None  # not born yet as of rev1
    assert at_rev2 is not None
    assert at_rev2.id == entity.id


def test_get_entity_at_revision_none_means_current(db: MemoryDatabase, system: System) -> None:
    entity = _entity(system, "a")
    with db.unit_of_work() as uow:
        uow.graph.upsert_entities([entity])
        uow.commit()

    with db.unit_of_work() as uow:
        current = get_entity_at_revision(
            uow, system_id=system.id, entity_id=entity.id, revision=None
        )

    assert current is not None
    assert current.id == entity.id


# --- get_relationships --------------------------------------------------------


def test_get_relationships_filters_by_direction(db: MemoryDatabase, system: System) -> None:
    a = _entity(system, "a")
    b = _entity(system, "b")
    c = _entity(system, "c")
    ev = _evidence(system)
    with db.unit_of_work() as uow:
        uow.graph.upsert_entities([a, b, c])
        uow.graph.upsert_relationships(
            [
                _rel(system, a, b, RelationshipType.CALLS, ev),
                _rel(system, c, a, RelationshipType.CALLS, ev),
            ]
        )
        uow.evidence.record([ev])
        uow.commit()

    with db.unit_of_work() as uow:
        outgoing = get_relationships(uow, system_id=system.id, entity_id=a.id, direction="out")
        incoming = get_relationships(uow, system_id=system.id, entity_id=a.id, direction="in")
        both = get_relationships(uow, system_id=system.id, entity_id=a.id, direction="both")

    assert {r.target_entity_id for r in outgoing} == {b.id}
    assert {r.source_entity_id for r in incoming} == {c.id}
    assert len(both) == 2


def test_get_relationships_filters_by_type(db: MemoryDatabase, system: System) -> None:
    a = _entity(system, "a")
    b = _entity(system, "b")
    ev = _evidence(system)
    with db.unit_of_work() as uow:
        uow.graph.upsert_entities([a, b])
        uow.graph.upsert_relationships(
            [
                _rel(system, a, b, RelationshipType.CALLS, ev),
                _rel(system, a, b, RelationshipType.IMPORTS, ev),
            ]
        )
        uow.evidence.record([ev])
        uow.commit()

    with db.unit_of_work() as uow:
        calls_only = get_relationships(
            uow,
            system_id=system.id,
            entity_id=a.id,
            direction="out",
            types=[RelationshipType.CALLS],
        )

    assert {r.type for r in calls_only} == {RelationshipType.CALLS}


def test_get_relationships_is_revision_aware(db: MemoryDatabase, system: System) -> None:
    a = _entity(system, "a")
    b = _entity(system, "b")
    ev = _evidence(system)
    rel = _rel(system, a, b, RelationshipType.CALLS, ev).model_copy(
        update={"valid_from_revision": "rev2"}
    )
    with db.unit_of_work() as uow:
        uow.graph.upsert_entities([a, b])
        uow.graph.upsert_relationships([rel])
        uow.evidence.record([ev])
        uow.commit()

    with db.unit_of_work() as uow:
        at_rev1 = get_relationships(
            uow, system_id=system.id, entity_id=a.id, direction="out", revision="rev1"
        )
        at_rev2 = get_relationships(
            uow, system_id=system.id, entity_id=a.id, direction="out", revision="rev2"
        )

    assert at_rev1 == []
    assert len(at_rev2) == 1
