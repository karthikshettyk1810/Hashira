"""The FastAPI milestone's real definition of done (per the design
discussion this was built from): can two materially different frameworks
produce equivalent concepts in the same System IR, without the query itself
needing to know which framework it is looking at?

Two semantically equivalent applications -- `tests/fixtures/django_basic/`
(reused as-is: it already matches the target shape exactly, so a byte-for-
byte duplicate under a new fixture name would add nothing) and
`tests/fixtures/fastapi_checkout/` -- both implement:

    POST /checkout
          |
    (view / route handler)
          |
    PaymentService.process

Each is indexed through its own, completely independent adapter/normalizer
pair (`hashira.adapters.django` vs `hashira.adapters.fastapi`) into a
separate system, and then asked the *same* question with the *same*,
framework-agnostic function: `reverse_impact` below imports nothing from
either adapter package and knows nothing about Django or FastAPI -- it only
walks `Relationship` records by type, exactly like `test_django_identity.py`'s
own killer test and `application/history.py`.

**Why the query root differs between the two fixtures.** Django's model
field (`payments.models.Payment.status`) has no FastAPI/Pydantic equivalent
in this milestone -- field-level tracking on Pydantic models was explicitly
out of scope (`adapters/fastapi/adapter.py`'s module docstring: that is a
DataAdapter's job, "basic ... association" was the ask). `PaymentService.process`
is the one concept both stacks build *identically*, down to the qualified
name (both fixtures use the same `payments/services.py` layout on purpose)
-- so it is the fair, architecture-only comparison point. The exact impact
sets are not expected to match (the design discussion's own framing:
"Django models and Pydantic schemas aren't equivalent") -- what has to match
is that the traversal reaches something playing "the handler that receives
the request" and something playing "the test that verifies the behavior" in
both graphs, using nothing but the universal relationship vocabulary.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from hashira.adapters.django import DjangoAdapter
from hashira.adapters.django import normalize as django_normalize
from hashira.adapters.fastapi import FastAPIAdapter
from hashira.adapters.fastapi import normalize as fastapi_normalize
from hashira.adapters.python import PythonAdapter
from hashira.application import IndexingService
from hashira.core import Entity, EntityType, Relationship, RelationshipType, System
from hashira.ports.repositories import UnitOfWork
from hashira.storage.memory import MemoryDatabase

DJANGO_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "django_basic"
FASTAPI_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fastapi_checkout"

# Structural edges a "what could this affect" query should not walk through
# -- containment/definition isn't impact (spec §25), matching every other
# impact-traversal test in this codebase (test_django_identity.py,
# application/history.py's own filtering).
_STRUCTURAL = {RelationshipType.CONTAINS, RelationshipType.DEFINES}


def reverse_impact(
    entities: list[Entity], relationships: list[Relationship], start_qualified_name: str
) -> set[str]:
    """Framework-agnostic reverse-impact traversal: "what, directly or
    transitively, depends on this entity?" This function takes only core IR
    types (`Entity`, `Relationship`) and knows nothing about Django,
    FastAPI, or any other adapter -- the entire point of this test."""
    by_qn = {e.qualified_name: e for e in entities}
    by_id = {e.id: e for e in entities}
    start = by_qn[start_qualified_name]
    seen = {start.id}
    frontier = [start.id]
    while frontier:
        next_frontier: list[str] = []
        for eid in frontier:
            for rel in relationships:
                if rel.type in _STRUCTURAL or not rel.is_current:
                    continue
                if rel.target_entity_id == eid and rel.source_entity_id not in seen:
                    seen.add(rel.source_entity_id)
                    next_frontier.append(rel.source_entity_id)
        frontier = next_frontier
    return {by_id[i].qualified_name for i in seen if i != start.id}


def _index(
    fixture: Path, tmp_path: Path, *, adapter: object, normalize: object, slug: str
) -> tuple[MemoryDatabase, System]:
    root = tmp_path / slug
    shutil.copytree(fixture, root)

    db = MemoryDatabase()
    system = System(name=slug, slug=slug)
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()

    service = IndexingService(
        db.unit_of_work,
        [PythonAdapter()],
        normalize,  # type: ignore[arg-type]
        framework_adapters=[adapter],  # type: ignore[list-item]
    )
    result = service.index(root, system_id=system.id, revision="rev1")
    assert result.errors == []
    return db, system


def _graph(uow: UnitOfWork, system_id: str) -> tuple[list[Entity], list[Relationship]]:
    entities = list(uow.graph.find_entities(system_id, limit=10_000))
    relationships = [
        rel for e in entities for rel in uow.graph.get_relationships(e.id, direction="out")
    ]
    return entities, relationships


@pytest.fixture
def django_graph(tmp_path: Path) -> tuple[list[Entity], list[Relationship]]:
    db, system = _index(
        DJANGO_FIXTURE,
        tmp_path,
        adapter=DjangoAdapter(),
        normalize=django_normalize,
        slug="django-checkout",
    )
    with db.unit_of_work() as uow:
        return _graph(uow, system.id)


@pytest.fixture
def fastapi_graph(tmp_path: Path) -> tuple[list[Entity], list[Relationship]]:
    db, system = _index(
        FASTAPI_FIXTURE,
        tmp_path,
        adapter=FastAPIAdapter(),
        normalize=fastapi_normalize,
        slug="fastapi-checkout",
    )
    with db.unit_of_work() as uow:
        return _graph(uow, system.id)


def test_both_frameworks_route_a_request_to_the_same_service_method(
    django_graph: tuple[list[Entity], list[Relationship]],
    fastapi_graph: tuple[list[Entity], list[Relationship]],
) -> None:
    """The one query this milestone was actually built to answer: what is
    affected if `PaymentService.process` changes -- asked identically of two
    independently-built graphs."""
    django_entities, django_relationships = django_graph
    fastapi_entities, fastapi_relationships = fastapi_graph

    django_affected = reverse_impact(
        django_entities, django_relationships, "payments.services.PaymentService.process"
    )
    fastapi_affected = reverse_impact(
        fastapi_entities, fastapi_relationships, "payments.services.PaymentService.process"
    )

    # Both traversals actually reach something -- the shared query works
    # against both graphs without any framework-specific fallback.
    assert django_affected
    assert fastapi_affected

    # Each reaches the thing that receives the HTTP request in its own
    # stack (Django's view method; FastAPI's route handler function) --
    # different qualified names, same architectural role.
    assert "payments.views.CheckoutView.post" in django_affected
    assert "payments.routers.checkout" in fastapi_affected

    # Each reaches the test that exercises the behavior directly.
    django_test = "payments.tests.test_checkout.TestCheckout.test_process_marks_payment_captured"
    fastapi_test = "payments.tests.test_checkout.TestCheckout.test_process_marks_payment_captured"
    assert django_test in django_affected
    assert fastapi_test in fastapi_affected

    # The exact sets are deliberately NOT asserted equal (Django's chain has
    # an intervening class the FastAPI handler does not; see
    # test_fastapi_normalizer.py's killer-test docstring for why FastAPI's
    # route entity ends up in the same unbroken chain while Django's needs a
    # separate hop). What matters is that the *traversal mechanism* -- one
    # function, zero Django/FastAPI imports -- works unmodified on both.


def test_both_routes_are_the_same_universal_entity_type(
    django_graph: tuple[list[Entity], list[Relationship]],
    fastapi_graph: tuple[list[Entity], list[Relationship]],
) -> None:
    """Independent evidence, not yet promotion (per the design discussion):
    both normalizers, built independently, reached for the exact same core
    vocabulary -- a route is an `EntityType.INTERFACE` that `EXPOSES` a
    handler -- without either adapter importing anything from the other."""
    django_entities, django_relationships = django_graph
    fastapi_entities, fastapi_relationships = fastapi_graph

    django_route = next(e for e in django_entities if e.qualified_name == "payments.urls:checkout/")
    fastapi_route = next(
        e
        for e in fastapi_entities
        if e.qualified_name == "payments.routers.router:POST /payments/checkout/"
    )

    assert django_route.type is EntityType.INTERFACE
    assert fastapi_route.type is EntityType.INTERFACE

    assert any(
        r.type is RelationshipType.EXPOSES and r.source_entity_id == django_route.id
        for r in django_relationships
    )
    assert any(
        r.type is RelationshipType.EXPOSES and r.source_entity_id == fastapi_route.id
        for r in fastapi_relationships
    )


def test_both_handlers_are_tagged_python_symbols_not_reclassified(
    django_graph: tuple[list[Entity], list[Relationship]],
    fastapi_graph: tuple[list[Entity], list[Relationship]],
) -> None:
    """The identity discipline this milestone was explicitly asked to
    repeat from Django: a handler is still a Python entity Python's own
    adapter extracted, tagged via `metadata`, never a second,
    framework-specific entity minted for convenience.

    What gets tagged differs by idiom, and that difference is itself real,
    expected evidence rather than a discrepancy: Django's routing targets a
    class-based view, so the *class* (`CheckoutView`) is tagged
    `django_kind="view"` while `.post` itself carries no tag of its own.
    FastAPI's routing targets a plain function directly, so the *handler
    function itself* is tagged `fastapi_kind="route_handler"` -- there is no
    wrapping class to tag instead."""
    django_entities, _ = django_graph
    fastapi_entities, _ = fastapi_graph

    django_handler = next(
        e for e in django_entities if e.qualified_name == "payments.views.CheckoutView.post"
    )
    django_view_class = next(
        e for e in django_entities if e.qualified_name == "payments.views.CheckoutView"
    )
    fastapi_handler = next(
        e for e in fastapi_entities if e.qualified_name == "payments.routers.checkout"
    )

    assert django_handler.type is EntityType.SYMBOL
    assert django_view_class.type is EntityType.SYMBOL
    assert fastapi_handler.type is EntityType.SYMBOL

    assert django_view_class.metadata["framework"] == "django"
    assert django_view_class.metadata["django_kind"] == "view"
    assert fastapi_handler.metadata["framework"] == "fastapi"
    assert fastapi_handler.metadata["fastapi_kind"] == "route_handler"
