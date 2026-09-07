"""A golden correctness benchmark -- internal engineering signal, not a
marketing claim. Every milestone through R4 has counted tests, entities,
relationships, and impact-path counts; none of those numbers answer the
actual question that matters: when Hashira reports "167 paths," are they
167 *correct* paths, or merely 167 paths?

This file hand-verifies, against real source semantics (not against
whatever Hashira happens to produce), the complete set of
impact-meaningful relationships (`CALLS`/`READS`/`WRITES`/`MAPS_TO`/
`EXPOSES`/`REFERENCES` -- the subset of `IMPACT_EDGES` an application's
actual behavior produces, excluding the purely structural `DEFINES`/
`IMPORTS`/`EXTENDS` edges every file trivially has) for one deliberately
cross-layer fixture: a SQLAlchemy model, a repository, a
constructor-composed service, a Pydantic response schema, and a FastAPI
route. Every one of the ten expected edges below was checked by hand
against the fixture's own source, once, before this file was written --
not derived from Hashira's own output and then asserted back.

    recall    = |expected ∩ found| / |expected|   -- did we miss anything?
    precision = |expected ∩ found| / |found|       -- did we invent anything?

Both are exact-set assertions here (100% is the bar for a fixture this
size and this well-understood), not a threshold with slack -- a
regression in either direction fails this file immediately. A known,
disclosed gap (the Pydantic `PaymentOut(status=result)` construction,
`FRAMEWORK_REFLECTION`) is deliberately *not* in the expected set: recall
is measured against what Hashira's adapters actually claim to resolve,
not against a hypothetical complete graph no version of this project has
ever promised.

As the corpus of real, cross-layer fixtures grows (this file's own
natural next step), recall/precision stop being a single hand-verified
snapshot and become a real regression signal: a future change that
silently drops or fabricates an edge here fails a specific, named
assertion, not a vague "fewer tests are green than before."
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
from hashira.core import Entity, Relationship, RelationshipType, System
from hashira.storage.memory import MemoryDatabase

# The relationship types an application's own runtime behavior produces --
# excludes DEFINES/IMPORTS/EXTENDS, which every file has trivially and
# which R1's own import/call matrices already hold to their own standard.
_IMPACT_MEANINGFUL_TYPES = frozenset(
    {
        RelationshipType.CALLS,
        RelationshipType.READS,
        RelationshipType.WRITES,
        RelationshipType.MAPS_TO,
        RelationshipType.EXPOSES,
        RelationshipType.REFERENCES,
    }
)

# Hand-verified against the fixture's own source, once, before this file
# was written -- (type, source qualified name, target qualified name).
_EXPECTED: frozenset[tuple[RelationshipType, str, str]] = frozenset(
    {
        (
            RelationshipType.CALLS,
            "payments.repository.PaymentRepository.get",
            "payments.models.Payment",
        ),
        (RelationshipType.CALLS, "payments.routers.close", "payments.service.PaymentService"),
        (
            RelationshipType.CALLS,
            "payments.routers.close",
            "payments.service.PaymentService.close",
        ),
        (RelationshipType.CALLS, "payments.routers.close", "payments.schemas.PaymentOut"),
        (
            RelationshipType.CALLS,
            "payments.service.PaymentService.__init__",
            "payments.repository.PaymentRepository",
        ),
        (
            RelationshipType.CALLS,
            "payments.service.PaymentService.close",
            "payments.repository.PaymentRepository.get",
        ),
        (
            RelationshipType.EXPOSES,
            "payments.routers.router:POST /payments/close/",
            "payments.routers.close",
        ),
        (RelationshipType.MAPS_TO, "payments.models.Payment", "payments"),
        (RelationshipType.READS, "payments.service.PaymentService.close", "payments.status"),
        (RelationshipType.WRITES, "payments.service.PaymentService.close", "payments.status"),
    }
)


def _write(root: Path, relpath: str, content: str) -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _build_fixture(root: Path) -> None:
    _write(
        root,
        "payments/models.py",
        "from sqlalchemy import Column, Integer, String\n"
        "from sqlalchemy.orm import declarative_base\n\n"
        "Base = declarative_base()\n\n\n"
        "class Payment(Base):\n"
        '    __tablename__ = "payments"\n\n'
        "    id = Column(Integer, primary_key=True)\n"
        "    status = Column(String(20))\n",
    )
    _write(
        root,
        "payments/repository.py",
        "from payments.models import Payment\n\n\n"
        "class PaymentRepository:\n"
        "    def get(self, payment_id) -> Payment:\n"
        "        return Payment()\n",
    )
    _write(
        root,
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
        root,
        "payments/schemas.py",
        "from pydantic import BaseModel\n\n\nclass PaymentOut(BaseModel):\n    status: str\n",
    )
    _write(
        root,
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
        root,
        "main.py",
        "from fastapi import FastAPI\n\nfrom payments.routers import router\n\n"
        "app = FastAPI()\n"
        'app.include_router(router, prefix="/payments")\n',
    )


def _index(tmp_path: Path) -> tuple[dict[str, Entity], list[Relationship]]:
    db = MemoryDatabase()
    system = System(name="golden", slug="golden")
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()
    normalize = compose_normalizers(enrich_fastapi, enrich_sqlalchemy)
    service = IndexingService(
        db.unit_of_work,
        [PythonAdapter()],
        normalize,
        framework_adapters=[FastAPIAdapter()],
        data_adapters=[SQLAlchemyAdapter()],
    )
    result = service.index(tmp_path, system_id=system.id, revision="rev1")
    assert result.errors == []
    with db.unit_of_work() as uow:
        entities = {e.id: e for e in uow.graph.find_entities(system.id, limit=10_000)}
        relationships = [
            rel
            for entity in entities.values()
            for rel in uow.graph.get_relationships(entity.id, direction="out")
        ]
    by_id = {eid: e.qualified_name for eid, e in entities.items() if e.qualified_name}
    return by_id, relationships


def test_golden_recall_and_precision_are_both_exact(tmp_path: Path) -> None:
    _build_fixture(tmp_path)
    by_id, relationships = _index(tmp_path)

    found: set[tuple[RelationshipType, str, str]] = {
        (rel.type, by_id[rel.source_entity_id], by_id[rel.target_entity_id])
        for rel in relationships
        if rel.type in _IMPACT_MEANINGFUL_TYPES
        and rel.source_entity_id in by_id
        and rel.target_entity_id in by_id
    }

    missing = _EXPECTED - found  # false negatives: recall failures
    extra = found - _EXPECTED  # false positives: precision failures

    recall = 1.0 - (len(missing) / len(_EXPECTED))
    precision = 1.0 if not found else 1.0 - (len(extra) / len(found))

    assert missing == set(), f"recall={recall:.2f}, missing edges: {missing}"
    assert extra == set(), f"precision={precision:.2f}, fabricated edges: {extra}"
    assert recall == 1.0
    assert precision == 1.0
