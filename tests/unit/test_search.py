"""`application.search`: basic textual entity search -- discovery, not
identity. No fuzzy ranking, no embeddings, exact/substring only."""

from __future__ import annotations

import pytest

from hashira.application.search import search_entities
from hashira.core import Entity, EntityType, System
from hashira.storage.memory import MemoryDatabase


@pytest.fixture
def db() -> MemoryDatabase:
    return MemoryDatabase()


@pytest.fixture
def system(db: MemoryDatabase) -> System:
    system = System(name="Checkout", slug="checkout-search")
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()
    return system


def _entity(system: System, name: str, qualified_name: str) -> Entity:
    return Entity(
        system_id=system.id, type=EntityType.SYMBOL, name=name, qualified_name=qualified_name
    )


def test_substring_match_on_qualified_name(db: MemoryDatabase, system: System) -> None:
    payment_status = _entity(system, "status", "payments.models.Payment.status")
    checkout = _entity(system, "checkout", "orders.routers.checkout")
    with db.unit_of_work() as uow:
        uow.graph.upsert_entities([payment_status, checkout])
        uow.commit()

    with db.unit_of_work() as uow:
        results = search_entities(uow, system_id=system.id, query="Payment")

    assert [e.id for e in results] == [payment_status.id]


def test_search_is_case_insensitive(db: MemoryDatabase, system: System) -> None:
    entity = _entity(system, "PaymentService", "payments.services.PaymentService")
    with db.unit_of_work() as uow:
        uow.graph.upsert_entities([entity])
        uow.commit()

    with db.unit_of_work() as uow:
        results = search_entities(uow, system_id=system.id, query="paymentservice")

    assert [e.id for e in results] == [entity.id]


def test_matches_on_bare_name_too(db: MemoryDatabase, system: System) -> None:
    entity = _entity(system, "checkout", "payments.routers.checkout")
    with db.unit_of_work() as uow:
        uow.graph.upsert_entities([entity])
        uow.commit()

    with db.unit_of_work() as uow:
        results = search_entities(uow, system_id=system.id, query="check")

    assert [e.id for e in results] == [entity.id]


def test_no_match_returns_empty(db: MemoryDatabase, system: System) -> None:
    entity = _entity(system, "checkout", "payments.routers.checkout")
    with db.unit_of_work() as uow:
        uow.graph.upsert_entities([entity])
        uow.commit()

    with db.unit_of_work() as uow:
        results = search_entities(uow, system_id=system.id, query="nonexistent")

    assert results == []


def test_blank_query_matches_nothing(db: MemoryDatabase, system: System) -> None:
    entity = _entity(system, "checkout", "payments.routers.checkout")
    with db.unit_of_work() as uow:
        uow.graph.upsert_entities([entity])
        uow.commit()

    with db.unit_of_work() as uow:
        assert search_entities(uow, system_id=system.id, query="   ") == []


def test_limit_is_respected(db: MemoryDatabase, system: System) -> None:
    entities = [_entity(system, f"payment{i}", f"payments.payment{i}") for i in range(5)]
    with db.unit_of_work() as uow:
        uow.graph.upsert_entities(entities)
        uow.commit()

    with db.unit_of_work() as uow:
        results = search_entities(uow, system_id=system.id, query="payment", limit=2)

    assert len(results) == 2
