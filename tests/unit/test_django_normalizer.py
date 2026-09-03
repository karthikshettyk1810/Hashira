"""Django's Stage 2: django.* observations + Python's own entities -> a
cross-domain graph. Verifies the actual entity/relationship shapes, not just
that observations were produced."""

from __future__ import annotations

from pathlib import Path

import pytest

from hashira.adapters.django import DjangoAdapter, normalize
from hashira.adapters.python import PythonAdapter
from hashira.adapters.python.discovery import discover_python_files
from hashira.core import EntityStatus, EntityType, RelationshipType
from hashira.core.ids import IDPrefix, new_id


@pytest.fixture
def system_id() -> str:
    return new_id(IDPrefix.SYSTEM)


def _write(root: Path, relpath: str, content: str) -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _run(tmp_path: Path, system_id: str):  # type: ignore[no-untyped-def]
    files = list(discover_python_files(tmp_path))
    python_result = PythonAdapter().extract(tmp_path, files, system_id=system_id, revision="rev1")
    django_result = DjangoAdapter().enrich(
        tmp_path, python_result, system_id=system_id, revision="rev1"
    )
    observations = [*python_result.observations, *django_result.observations]
    return normalize(observations, system_id=system_id, revision="rev1")


def _minimal_project(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "payments/models.py",
        "from django.db import models\n\n\nclass Payment(models.Model):\n"
        "    status = models.CharField(max_length=20)\n",
    )
    _write(
        tmp_path,
        "payments/services.py",
        "from .models import Payment\n\n\nclass PaymentService:\n"
        "    def process(self, amount):\n"
        "        payment = Payment()\n"
        '        payment.status = "pending"\n'
        "        return payment.status\n",
    )
    _write(
        tmp_path,
        "payments/views.py",
        "from django.views import View\n\nfrom .services import PaymentService\n\n\n"
        "class CheckoutView(View):\n"
        "    def post(self, request):\n"
        "        service = PaymentService()\n        return service.process(1)\n",
    )
    _write(
        tmp_path,
        "payments/urls.py",
        "from django.urls import path\n\nfrom .views import CheckoutView\n\n"
        "urlpatterns = [\n"
        '    path("checkout/", CheckoutView.as_view(), name="checkout"),\n'
        "]\n",
    )


def test_model_class_is_tagged_not_duplicated(tmp_path: Path, system_id: str) -> None:
    _minimal_project(tmp_path)
    run = _run(tmp_path, system_id)
    by_qn = {e.qualified_name: e for e in run.entities}

    model = by_qn["payments.models.Payment"]
    assert model.type is EntityType.SYMBOL  # still fundamentally a Python class
    assert model.metadata["framework"] == "django"
    assert model.metadata["django_kind"] == "model"
    # Exactly one entity for this qualified name -- not a duplicate DATA_ENTITY.
    assert len([e for e in run.entities if e.qualified_name == "payments.models.Payment"]) == 1


def test_view_class_is_tagged(tmp_path: Path, system_id: str) -> None:
    _minimal_project(tmp_path)
    run = _run(tmp_path, system_id)
    by_qn = {e.qualified_name: e for e in run.entities}
    view = by_qn["payments.views.CheckoutView"]
    assert view.metadata["framework"] == "django"
    assert view.metadata["django_kind"] == "view"


def test_field_entity_is_minted_and_contained_by_its_model(tmp_path: Path, system_id: str) -> None:
    _minimal_project(tmp_path)
    run = _run(tmp_path, system_id)
    by_qn = {e.qualified_name: e for e in run.entities}

    field = by_qn["payments.models.Payment.status"]
    assert field.type is EntityType.SYMBOL
    assert field.metadata["field_type"] == "CharField"

    model = by_qn["payments.models.Payment"]
    contains = [
        r
        for r in run.relationships
        if r.type is RelationshipType.CONTAINS and r.source_entity_id == model.id
    ]
    assert any(r.target_entity_id == field.id for r in contains)


def test_route_exposes_the_resolved_view(tmp_path: Path, system_id: str) -> None:
    _minimal_project(tmp_path)
    run = _run(tmp_path, system_id)
    by_qn = {e.qualified_name: e for e in run.entities}

    route = by_qn["payments.urls:checkout/"]
    assert route.type is EntityType.INTERFACE
    assert route.metadata["url_name"] == "checkout"

    view = by_qn["payments.views.CheckoutView"]
    exposes = [
        r
        for r in run.relationships
        if r.type is RelationshipType.EXPOSES and r.source_entity_id == route.id
    ]
    assert any(r.target_entity_id == view.id for r in exposes)


def test_field_access_produces_reads_and_writes(tmp_path: Path, system_id: str) -> None:
    _minimal_project(tmp_path)
    run = _run(tmp_path, system_id)
    by_qn = {e.qualified_name: e for e in run.entities}

    accessor = by_qn["payments.services.PaymentService.process"]
    field = by_qn["payments.models.Payment.status"]

    writes = [
        r
        for r in run.relationships
        if r.type is RelationshipType.WRITES
        and r.source_entity_id == accessor.id
        and r.target_entity_id == field.id
    ]
    reads = [
        r
        for r in run.relationships
        if r.type is RelationshipType.READS
        and r.source_entity_id == accessor.id
        and r.target_entity_id == field.id
    ]
    assert len(writes) == 1
    assert len(reads) == 1


def test_the_full_impact_chain_is_reachable_from_the_field(tmp_path: Path, system_id: str) -> None:
    """The actual "killer test": walking from Payment.status backward
    through non-structural edges reaches the service, and from there the
    view -- entirely from the graph, no LLM involved."""
    _minimal_project(tmp_path)
    run = _run(tmp_path, system_id)
    by_qn = {e.qualified_name: e for e in run.entities}
    by_id = {e.id: e for e in run.entities}

    field = by_qn["payments.models.Payment.status"]
    seen = {field.id}
    frontier = [field.id]
    while frontier:
        next_frontier = []
        for eid in frontier:
            for rel in run.relationships:
                if rel.type in (RelationshipType.CONTAINS, RelationshipType.DEFINES):
                    continue
                if rel.target_entity_id == eid and rel.source_entity_id not in seen:
                    seen.add(rel.source_entity_id)
                    next_frontier.append(rel.source_entity_id)
        frontier = next_frontier

    affected = {by_id[i].qualified_name for i in seen if i != field.id}
    assert affected == {
        "payments.services.PaymentService.process",
        "payments.views.CheckoutView.post",
    }


def test_unresolved_route_view_is_not_a_relationship(tmp_path: Path, system_id: str) -> None:
    _write(
        tmp_path,
        "config/urls.py",
        "from django.urls import include, path\n\nurlpatterns = [\n"
        '    path("api/", include("payments.urls")),\n'
        "]\n",
    )
    run = _run(tmp_path, system_id)
    assert [r for r in run.relationships if r.type is RelationshipType.EXPOSES] == []
    assert any(o.kind == "django.url_route" for o in run.unresolved)


def test_field_on_a_model_with_no_candidate_stays_unresolved(
    tmp_path: Path, system_id: str
) -> None:
    """A defensive case: if the model class somehow isn't in this run's
    candidates (shouldn't happen given the current pipeline, but the
    normalizer must not crash or invent one) -- the field observation is
    just left unresolved, matching every other adapter's behavior."""
    from hashira.core import Observation, Origin

    obs = Observation(
        system_id=system_id,
        adapter="django@0.1.0",
        origin=Origin.STATIC_ANALYSIS,
        kind="django.model_field",
        payload={
            "model_qualified_name": "nowhere.NoSuchModel",
            "field_name": "status",
            "field_type_qualified_name": "django.db.models.CharField",
            "line": 1,
        },
    )
    run = normalize([obs], system_id=system_id, revision="rev1")
    assert run.entities == []
    assert obs in run.unresolved


def test_boring_reindex_is_boring_for_django_entities_too(tmp_path: Path, system_id: str) -> None:
    """The same invariant Python entities already have (DECLARATION_ANCHOR
    makes an unchanged re-index resolve as MATCHED) must hold for
    Django-minted entities too -- fields and routes are ordinary candidate
    entities with the same identity claims, not a special case."""
    from hashira.identity import resolve

    _minimal_project(tmp_path)
    run1 = _run(tmp_path, system_id)
    field1 = next(e for e in run1.entities if e.qualified_name == "payments.models.Payment.status")

    run2 = _run(tmp_path, system_id)
    field2 = next(e for e in run2.entities if e.qualified_name == "payments.models.Payment.status")

    decision = resolve(field2, existing=[field1])
    assert decision.outcome.value == "MATCHED"
    assert field1.status is EntityStatus.ACTIVE
