"""FastAPI's Stage 2: fastapi.* observations + Python's own entities -> a
cross-domain graph. Verifies the actual entity/relationship shapes, not just
that observations were produced."""

from __future__ import annotations

from pathlib import Path

import pytest

from hashira.adapters.fastapi import FastAPIAdapter, normalize
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
    fastapi_result = FastAPIAdapter().enrich(
        tmp_path, python_result, system_id=system_id, revision="rev1"
    )
    observations = [*python_result.observations, *fastapi_result.observations]
    return normalize(observations, system_id=system_id, revision="rev1")


def _minimal_project(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "payments/schemas.py",
        "from pydantic import BaseModel\n\n\n"
        "class PaymentRequest(BaseModel):\n    amount: int\n\n\n"
        "class PaymentResponse(BaseModel):\n    status: str\n",
    )
    _write(
        tmp_path,
        "payments/services.py",
        'class PaymentService:\n    def process(self, amount):\n        return "captured"\n',
    )
    _write(
        tmp_path,
        "payments/routers.py",
        "from fastapi import APIRouter\n\n"
        "from .schemas import PaymentRequest, PaymentResponse\n"
        "from .services import PaymentService\n\n"
        "router = APIRouter()\n\n\n"
        '@router.post("/checkout/", response_model=PaymentResponse)\n'
        "def checkout(body: PaymentRequest) -> PaymentResponse:\n"
        "    service = PaymentService()\n"
        "    result = service.process(body.amount)\n"
        "    return PaymentResponse(status=result)\n",
    )
    _write(
        tmp_path,
        "main.py",
        "from fastapi import FastAPI\n\n"
        "from payments.routers import router\n\n"
        "app = FastAPI()\n"
        'app.include_router(router, prefix="/payments")\n',
    )


def test_handler_is_tagged_not_duplicated(tmp_path: Path, system_id: str) -> None:
    _minimal_project(tmp_path)
    run = _run(tmp_path, system_id)
    by_qn = {e.qualified_name: e for e in run.entities}

    handler = by_qn["payments.routers.checkout"]
    assert handler.type is EntityType.SYMBOL  # still fundamentally a Python function
    assert handler.metadata["framework"] == "fastapi"
    assert handler.metadata["fastapi_kind"] == "route_handler"
    assert len([e for e in run.entities if e.qualified_name == handler.qualified_name]) == 1


def test_request_and_response_models_are_recorded_and_tagged(
    tmp_path: Path, system_id: str
) -> None:
    _minimal_project(tmp_path)
    run = _run(tmp_path, system_id)
    by_qn = {e.qualified_name: e for e in run.entities}

    handler = by_qn["payments.routers.checkout"]
    assert handler.metadata["request_model_qualified_name"] == "payments.schemas.PaymentRequest"
    assert handler.metadata["response_model_qualified_name"] == "payments.schemas.PaymentResponse"

    request_model = by_qn["payments.schemas.PaymentRequest"]
    assert request_model.metadata["framework"] == "fastapi"
    assert request_model.metadata["fastapi_kind"] == "schema"
    response_model = by_qn["payments.schemas.PaymentResponse"]
    assert response_model.metadata["fastapi_kind"] == "schema"


def test_route_exposes_the_handler_with_the_composed_path(tmp_path: Path, system_id: str) -> None:
    _minimal_project(tmp_path)
    run = _run(tmp_path, system_id)
    by_qn = {e.qualified_name: e for e in run.entities}

    route = by_qn["payments.routers.router:POST /payments/checkout/"]
    assert route.type is EntityType.INTERFACE
    assert route.metadata["http_method"] == "POST"
    assert route.metadata["path"] == "/payments/checkout/"

    handler = by_qn["payments.routers.checkout"]
    exposes = [
        r
        for r in run.relationships
        if r.type is RelationshipType.EXPOSES and r.source_entity_id == route.id
    ]
    assert any(r.target_entity_id == handler.id for r in exposes)


def test_depends_produces_depends_on_not_calls(tmp_path: Path, system_id: str) -> None:
    _write(
        tmp_path,
        "deps.py",
        "def get_service():\n    pass\n",
    )
    _write(
        tmp_path,
        "main.py",
        "from fastapi import Depends, FastAPI\n\n"
        "from deps import get_service\n\n"
        "app = FastAPI()\n\n\n"
        '@app.post("/checkout/")\n'
        "def checkout(service=Depends(get_service)):\n    pass\n",
    )
    run = _run(tmp_path, system_id)
    by_qn = {e.qualified_name: e for e in run.entities}
    handler = by_qn["main.checkout"]
    dep = by_qn["deps.get_service"]

    depends_on = [
        r
        for r in run.relationships
        if r.type is RelationshipType.DEPENDS_ON
        and r.source_entity_id == handler.id
        and r.target_entity_id == dep.id
    ]
    assert len(depends_on) == 1
    calls = [
        r
        for r in run.relationships
        if r.type is RelationshipType.CALLS
        and r.source_entity_id == handler.id
        and r.target_entity_id == dep.id
    ]
    assert calls == []


def test_the_full_impact_chain_is_reachable_from_the_service_method(
    tmp_path: Path, system_id: str
) -> None:
    """FastAPI's analogue of Django's killer test: walking backward from
    `PaymentService.process` through non-structural edges reaches the
    handler that calls it directly, and -- since a plain function handler
    has no intervening class the way a Django view does -- the route itself
    in the same unbroken chain (see `test_cross_framework_equivalence.py`
    for why that is a genuine, expected architectural difference, not a
    discrepancy)."""
    _minimal_project(tmp_path)
    run = _run(tmp_path, system_id)
    by_qn = {e.qualified_name: e for e in run.entities}
    by_id = {e.id: e for e in run.entities}

    method = by_qn["payments.services.PaymentService.process"]
    seen = {method.id}
    frontier = [method.id]
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

    affected = {by_id[i].qualified_name for i in seen if i != method.id}
    assert affected == {
        "payments.routers.checkout",
        "payments.routers.router:POST /payments/checkout/",
    }


def test_unresolved_route_handler_stays_unresolved(tmp_path: Path, system_id: str) -> None:
    """A defensive case mirroring Django's equivalent: if the handler
    somehow isn't in this run's candidates, the normalizer must not crash
    or invent one -- the observation is just left unresolved."""
    from hashira.core import Observation, Origin

    obs = Observation(
        system_id=system_id,
        adapter="fastapi@0.1.0",
        origin=Origin.STATIC_ANALYSIS,
        kind="fastapi.route",
        payload={
            "var_qualified_name": "nowhere.app",
            "http_method": "GET",
            "path": "/nope",
            "handler_qualified_name": "nowhere.handler",
            "request_model_qualified_name": None,
            "response_model_qualified_name": None,
        },
    )
    run = normalize([obs], system_id=system_id, revision="rev1")
    assert run.entities == []
    assert obs in run.unresolved


def test_boring_reindex_is_boring_for_fastapi_entities_too(tmp_path: Path, system_id: str) -> None:
    """The same DECLARATION_ANCHOR-based MATCHED invariant Python/Django
    entities already have must hold for FastAPI-minted route entities too."""
    from hashira.identity import resolve

    _minimal_project(tmp_path)
    run1 = _run(tmp_path, system_id)
    route1 = next(
        e
        for e in run1.entities
        if e.qualified_name == "payments.routers.router:POST /payments/checkout/"
    )

    run2 = _run(tmp_path, system_id)
    route2 = next(
        e
        for e in run2.entities
        if e.qualified_name == "payments.routers.router:POST /payments/checkout/"
    )

    decision = resolve(route2, existing=[route1])
    assert decision.outcome.value == "MATCHED"
    assert route1.status is EntityStatus.ACTIVE
