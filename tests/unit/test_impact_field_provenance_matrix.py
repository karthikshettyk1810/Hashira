"""Hashira 0.2 -- Resolution Integrity, R4: cross-adapter data/field
impact. R1-R3 hardened individual resolution mechanisms in isolation
(import binding, call resolution, instance/attribute provenance, dynamic
dispatch disclosure); this file asks the question none of those answer on
their own:

    Can Hashira follow a piece of data through the *whole* application --
    database column, ORM model, repository, service, API route -- using
    `application.impact.reverse_impact`/`forward_impact` (the real product
    surface an agent actually calls), not a hand-rolled graph walk?

Deliberately uses only the existing relationship vocabulary
(`CALLS`/`READS`/`WRITES`/`MAPS_TO`/`REFERENCES`/`EXPOSES`) -- R1-R3 already
proved every individual hop resolves correctly; this file's own job is
confirming the *chain* holds together end to end through
`IMPACT_EDGES` (`core/relationships.py`), and that two similarly-shaped
but unrelated chains never conflate.

| Pattern                                              | Outcome     |
|---------------------------------------------------------|-------------|
| constructor instance -> reverse_impact reaches accessor | CERTAIN chain |
| cross-module constructor -> reverse_impact               | CERTAIN chain |
| typed parameter -> reverse_impact                        | CERTAIN chain |
| return-value instance (repository) -> reverse_impact     | CERTAIN chain |
| self-attribute chain -> reverse_impact                   | CERTAIN chain |
| constructor keyword write -> reverse_impact               | CERTAIN chain |
| DB column -> service -> API route (full vertical slice) | CERTAIN chain |
| API route -> service -> DB column (forward_impact)       | CERTAIN chain |
| table -> ORM class via MAPS_TO                            | CERTAIN chain |
| FK column -> referencing column -> its own accessor       | CERTAIN chain |
| same field name, different models (negative)              | no conflation |
| unrelated same-named local var (negative)                  | no conflation |
| ORM field -> Pydantic response schema field                | correctly absent, disclosed |
| weakest_confidence across a fully-static chain              | stays CERTAIN |
| coverage.status on a multi-hop query, raw SQL elsewhere      | PARTIAL, disclosed |

No new relationship type was needed for any of this -- the existing
vocabulary (`IMPACT_EDGES` already includes `MAPS_TO`/`REFERENCES` since
the SQLAlchemy milestone) was sufficient for every row that is
architecturally reachable today. The one row that is *not* reachable (ORM
field -> Pydantic schema field) was already a known, disclosed
`FRAMEWORK_REFLECTION` gap (`docs/IR.md`'s "closing the disclosure gap"
entry) -- confirmed here as still correctly absent and still correctly
disclosed, not fixed, matching this milestone's own instruction not to
build framework-reflection resolution. A DB-column-to-outbox/event/consumer
chain (the second flow this milestone's own brief named) is not tested at
all: no adapter produces `PRODUCES`/`CONSUMES`/`TRIGGERS`/`PUBLISHES` edges
today (Celery/Kafka enrichers remain unbuilt, `docs/ROADMAP.md` Phase 2's
own open items) -- there is nothing to verify yet, and building a new
adapter is out of this milestone's scope by its own instruction.
"""

from __future__ import annotations

from pathlib import Path

from hashira.adapters._compose import compose_normalizers
from hashira.adapters.fastapi import FastAPIAdapter
from hashira.adapters.fastapi.normalizer import enrich_normalized_run as enrich_fastapi
from hashira.adapters.python import PythonAdapter
from hashira.adapters.sqlalchemy import SQLAlchemyAdapter
from hashira.adapters.sqlalchemy.normalizer import enrich_normalized_run as enrich_sqlalchemy
from hashira.application import IndexingService
from hashira.application.impact import CoverageStatus, forward_impact, reverse_impact
from hashira.core import Confidence, Entity, System
from hashira.ports.adapters import LimitationKind
from hashira.storage.memory import MemoryDatabase


def _write(root: Path, relpath: str, content: str) -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _service(db: MemoryDatabase) -> IndexingService:
    normalize = compose_normalizers(enrich_fastapi, enrich_sqlalchemy)
    return IndexingService(
        db.unit_of_work,
        [PythonAdapter()],
        normalize,
        framework_adapters=[FastAPIAdapter()],
        data_adapters=[SQLAlchemyAdapter()],
    )


def _index(tmp_path: Path, name: str) -> tuple[MemoryDatabase, System, dict[str, Entity]]:
    db = MemoryDatabase()
    system = System(name=name, slug=name)
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()
    result = _service(db).index(tmp_path, system_id=system.id, revision="rev1")
    assert result.errors == []
    with db.unit_of_work() as uow:
        entities = {e.qualified_name: e for e in uow.graph.find_entities(system.id, limit=10_000)}
    return db, system, entities


_MODEL = (
    "from sqlalchemy import Column, Integer, String\n"
    "from sqlalchemy.orm import declarative_base\n\n"
    "Base = declarative_base()\n\n\n"
    "class Payment(Base):\n"
    '    __tablename__ = "payments"\n\n'
    "    id = Column(Integer, primary_key=True)\n"
    "    status = Column(String(20))\n"
)


def _reverse_impact_endpoints(db: MemoryDatabase, system: System, entity_id: str) -> set[str]:
    with db.unit_of_work() as uow:
        result = reverse_impact(uow, system_id=system.id, entity_id=entity_id)
    return {e.qualified_name for e in result.affected_entities if e.qualified_name}


# --- End-to-end: every provenance form reaches a real caller ----------------


def test_constructor_instance_reaches_accessor(tmp_path: Path) -> None:
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "payments/service.py",
        "from payments.models import Payment\n\n\n"
        "def process():\n"
        "    payment = Payment()\n"
        '    payment.status = "captured"\n'
        "    return payment.status\n",
    )
    db, system, entities = _index(tmp_path, "constructor")
    column = entities["payments.status"]
    assert "payments.service.process" in _reverse_impact_endpoints(db, system, column.id)


def test_typed_parameter_reaches_accessor(tmp_path: Path) -> None:
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "payments/service.py",
        "from payments.models import Payment\n\n\n"
        "def close(payment: Payment) -> str:\n"
        "    return payment.status\n",
    )
    db, system, entities = _index(tmp_path, "typed-param")
    column = entities["payments.status"]
    assert "payments.service.close" in _reverse_impact_endpoints(db, system, column.id)


def test_return_value_instance_reaches_accessor(tmp_path: Path) -> None:
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "payments/repository.py",
        "from payments.models import Payment\n\n\n"
        "class PaymentRepository:\n"
        "    def get(self, payment_id) -> Payment:\n"
        "        return Payment()\n",
    )
    _write(
        tmp_path,
        "payments/service.py",
        "from payments.repository import PaymentRepository\n\n\n"
        "def close(repo: PaymentRepository, payment_id) -> str:\n"
        "    payment = repo.get(payment_id)\n"
        "    return payment.status\n",
    )
    db, system, entities = _index(tmp_path, "return-value")
    column = entities["payments.status"]
    assert "payments.service.close" in _reverse_impact_endpoints(db, system, column.id)


def test_self_attribute_chain_reaches_accessor(tmp_path: Path) -> None:
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "payments/service.py",
        "from payments.models import Payment\n\n\n"
        "class Service:\n"
        "    def __init__(self):\n"
        "        self._payment = Payment()\n\n"
        "    def status(self):\n"
        "        return self._payment.status\n",
    )
    db, system, entities = _index(tmp_path, "self-attr")
    column = entities["payments.status"]
    assert "payments.service.Service.status" in _reverse_impact_endpoints(db, system, column.id)


def test_constructor_keyword_write_reaches_accessor(tmp_path: Path) -> None:
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "payments/service.py",
        "from payments.models import Payment\n\n\n"
        "def create():\n    return Payment(status='pending')\n",
    )
    db, system, entities = _index(tmp_path, "ctor-kwarg")
    column = entities["payments.status"]
    assert "payments.service.create" in _reverse_impact_endpoints(db, system, column.id)


# --- Full vertical slice: DB column <-> API route ----------------------------


def _vertical_slice_project(tmp_path: Path) -> None:
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "payments/repository.py",
        "from payments.models import Payment\n\n\n"
        "class PaymentRepository:\n"
        "    def get(self, payment_id) -> Payment:\n"
        "        return Payment()\n",
    )
    _write(
        tmp_path,
        "payments/service.py",
        "from payments.repository import PaymentRepository\n\n\n"
        "class PaymentService:\n"
        "    def __init__(self):\n"
        "        self._repo = PaymentRepository()\n\n"
        "    def close(self, payment_id) -> str:\n"
        "        payment = self._repo.get(payment_id)\n"
        '        payment.status = "closed"\n'
        "        return payment.status\n",
    )
    _write(
        tmp_path,
        "payments/schemas.py",
        "from pydantic import BaseModel\n\n\nclass PaymentOut(BaseModel):\n    status: str\n",
    )
    _write(
        tmp_path,
        "payments/routers.py",
        "from fastapi import APIRouter\n\n"
        "from .schemas import PaymentOut\n"
        "from .service import PaymentService\n\n"
        "router = APIRouter()\n\n\n"
        '@router.post("/close/", response_model=PaymentOut)\n'
        "def close(payment_id: int) -> PaymentOut:\n"
        "    service = PaymentService()\n"
        "    result = service.close(payment_id)\n"
        "    return PaymentOut(status=result)\n",
    )
    _write(
        tmp_path,
        "main.py",
        "from fastapi import FastAPI\n\nfrom payments.routers import router\n\n"
        "app = FastAPI()\n"
        'app.include_router(router, prefix="/payments")\n',
    )


def test_reverse_impact_from_db_column_reaches_the_api_route(tmp_path: Path) -> None:
    """The vertical slice this whole milestone is about: a column's own
    `reverse_impact` result reaches the repository, the service, and --
    through `EXPOSES` -- the route itself, entirely from existing edge
    types."""
    _vertical_slice_project(tmp_path)
    db, system, entities = _index(tmp_path, "vertical-slice")
    column = entities["payments.status"]
    endpoints = _reverse_impact_endpoints(db, system, column.id)
    assert "payments.service.PaymentService.close" in endpoints
    assert "payments.routers.close" in endpoints
    # The INTERFACE route entity itself, not just the handler function.
    assert "payments.routers.router:POST /payments/close/" in endpoints


def test_forward_impact_from_the_route_reaches_the_db_column(tmp_path: Path) -> None:
    """The same slice, walked the other direction: what does this route
    depend on, all the way down to the database column."""
    _vertical_slice_project(tmp_path)
    db, system, entities = _index(tmp_path, "vertical-slice-fwd")
    handler = entities["payments.routers.close"]
    with db.unit_of_work() as uow:
        result = forward_impact(uow, system_id=system.id, entity_id=handler.id)
    endpoints = {e.qualified_name for e in result.affected_entities if e.qualified_name}
    assert "payments.status" in endpoints


# --- Structural chains: MAPS_TO and REFERENCES -------------------------------


def test_reverse_impact_from_table_reaches_the_orm_class(tmp_path: Path) -> None:
    _write(tmp_path, "payments/models.py", _MODEL)
    db, system, entities = _index(tmp_path, "maps-to")
    table = entities["payments"]
    endpoints = _reverse_impact_endpoints(db, system, table.id)
    assert "payments.models.Payment" in endpoints


def test_reverse_impact_follows_a_foreign_key_to_its_own_accessor(tmp_path: Path) -> None:
    """`orders.payment_id` references `payments.id` -- `reverse_impact` on
    the referenced column must reach whoever touches the *referencing*
    column, via `REFERENCES`."""
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "orders/models.py",
        "from sqlalchemy import Column, ForeignKey, Integer\n"
        "from sqlalchemy.orm import declarative_base\n\n"
        "Base = declarative_base()\n\n\n"
        "class Order(Base):\n"
        '    __tablename__ = "orders"\n\n'
        "    id = Column(Integer, primary_key=True)\n"
        '    payment_id = Column(Integer, ForeignKey("payments.id"))\n',
    )
    _write(
        tmp_path,
        "orders/service.py",
        "from orders.models import Order\n\n\n"
        "def link(order_id):\n"
        "    order = Order()\n"
        "    return order.payment_id\n",
    )
    db, system, entities = _index(tmp_path, "fk-chain")
    payment_id_col = entities["payments.id"]
    endpoints = _reverse_impact_endpoints(db, system, payment_id_col.id)
    assert "orders.payment_id" in endpoints
    assert "orders.service.link" in endpoints


# --- Negative cases: no conflation -------------------------------------------


def test_same_field_name_on_a_different_model_does_not_conflate(tmp_path: Path) -> None:
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "orders/models.py",
        "from sqlalchemy import Column, Integer, String\n"
        "from sqlalchemy.orm import declarative_base\n\n"
        "Base = declarative_base()\n\n\n"
        "class Order(Base):\n"
        '    __tablename__ = "orders"\n\n'
        "    id = Column(Integer, primary_key=True)\n"
        "    status = Column(String(20))\n",
    )
    _write(
        tmp_path,
        "payments/service.py",
        "from payments.models import Payment\n\n\n"
        "def touch_payment():\n"
        "    payment = Payment()\n"
        "    return payment.status\n",
    )
    _write(
        tmp_path,
        "orders/service.py",
        "from orders.models import Order\n\n\n"
        "def touch_order():\n"
        "    order = Order()\n"
        "    return order.status\n",
    )
    db, system, entities = _index(tmp_path, "no-conflate-models")
    payment_status = entities["payments.status"]
    endpoints = _reverse_impact_endpoints(db, system, payment_status.id)
    assert "payments.service.touch_payment" in endpoints
    assert "orders.service.touch_order" not in endpoints


def test_unrelated_same_named_local_variable_does_not_conflate(tmp_path: Path) -> None:
    """A plain, non-model object's own `.status` attribute must never be
    mistaken for the real `Payment.status` column just because the name
    matches."""
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "unrelated/service.py",
        "class Result:\n    def __init__(self):\n        self.status = 'ok'\n\n\n"
        "def check():\n    r = Result()\n    return r.status\n",
    )
    db, system, entities = _index(tmp_path, "no-conflate-unrelated")
    payment_status = entities["payments.status"]
    endpoints = _reverse_impact_endpoints(db, system, payment_status.id)
    assert "unrelated.service.check" not in endpoints


# --- Disclosed, not resolved: ORM field -> Pydantic schema field ------------


def test_orm_field_to_response_schema_field_is_correctly_absent_and_disclosed(
    tmp_path: Path,
) -> None:
    """`PaymentOut(status=result)` -- Pydantic's own construction, not an
    ORM attribute access -- correctly produces no edge from
    `Payment.status` to `PaymentOut.status` at all (this is deliberately
    unresolved -- known, disclosed `FRAMEWORK_REFLECTION`, not this
    milestone's job to fix). Confirms the disclosure survives all the way
    through `reverse_impact`'s own `coverage.limitations`, not just the
    adapter's own capabilities() declaration."""
    _vertical_slice_project(tmp_path)
    db, system, entities = _index(tmp_path, "schema-boundary")
    column = entities["payments.status"]
    # `PaymentOut` the *class* is tagged `fastapi_kind="schema"` (an
    # existing, already-tagged Python entity, per the FastAPI adapter's own
    # reuse discipline) -- but no *field*-level entity is ever minted for
    # one of its fields, and no edge connects it to `Payment.status` at all.
    assert "payments.schemas.PaymentOut.status" not in entities
    endpoints = _reverse_impact_endpoints(db, system, column.id)
    assert "payments.schemas.PaymentOut" not in endpoints

    with db.unit_of_work() as uow:
        result = reverse_impact(uow, system_id=system.id, entity_id=column.id)

    limitations = result.coverage.limitations
    assert any(lim.kind is LimitationKind.FRAMEWORK_REFLECTION for lim in limitations)


# --- Confidence and coverage propagate correctly through the whole chain ----


def test_weakest_confidence_stays_certain_across_a_fully_static_chain(tmp_path: Path) -> None:
    _vertical_slice_project(tmp_path)
    db, system, entities = _index(tmp_path, "confidence-chain")
    column = entities["payments.status"]
    with db.unit_of_work() as uow:
        result = reverse_impact(uow, system_id=system.id, entity_id=column.id)
    route_qn = "payments.routers.router:POST /payments/close/"
    route_paths = [p for p in result.paths if p.endpoint.qualified_name == route_qn]
    assert route_paths
    assert route_paths[0].weakest_confidence is Confidence.CERTAIN


def test_coverage_stays_partial_on_a_multi_hop_query_when_raw_sql_touches_the_same_table(
    tmp_path: Path,
) -> None:
    _vertical_slice_project(tmp_path)
    _write(
        tmp_path,
        "payments/reporting.py",
        "from sqlalchemy import text\n\n\n"
        "def count_pending(session):\n"
        "    return session.execute(\n"
        "        text(\"SELECT count(*) FROM payments WHERE status = 'pending'\")\n"
        "    ).scalar()\n",
    )
    db, system, entities = _index(tmp_path, "coverage-chain")
    column = entities["payments.status"]
    with db.unit_of_work() as uow:
        result = reverse_impact(uow, system_id=system.id, entity_id=column.id)
    assert result.coverage.status is CoverageStatus.PARTIAL
