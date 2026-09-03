"""SQLAlchemy's Stage 2: sqlalchemy.* observations + Python's own entities
-> a cross-domain graph. Verifies the actual entity/relationship shapes, not
just that observations were produced."""

from __future__ import annotations

from pathlib import Path

import pytest

from hashira.adapters.python import PythonAdapter
from hashira.adapters.python.discovery import discover_python_files
from hashira.adapters.sqlalchemy import SQLAlchemyAdapter, normalize
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
    sqla_result = SQLAlchemyAdapter().enrich(
        tmp_path, python_result, system_id=system_id, revision="rev1"
    )
    observations = [*python_result.observations, *sqla_result.observations]
    return normalize(observations, system_id=system_id, revision="rev1")


def _minimal_project(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "payments/__init__.py",
        "",
    )
    _write(
        tmp_path,
        "payments/models.py",
        "from sqlalchemy import Column, ForeignKey, Integer, String\n"
        "from sqlalchemy.orm import declarative_base\n\n"
        "Base = declarative_base()\n\n\n"
        "class Account(Base):\n"
        '    __tablename__ = "accounts"\n\n'
        "    id = Column(Integer, primary_key=True)\n\n\n"
        "class Payment(Base):\n"
        '    __tablename__ = "payments"\n\n'
        "    id = Column(Integer, primary_key=True)\n"
        "    status = Column(String(20))\n"
        '    account_id = Column(Integer, ForeignKey("accounts.id"))\n',
    )
    _write(
        tmp_path,
        "payments/service.py",
        "from .models import Payment\n\n\n"
        "class PaymentService:\n"
        "    def process(self, amount):\n"
        "        payment = Payment()\n"
        '        payment.status = "pending"\n'
        "        return payment.status\n",
    )


def test_model_class_is_tagged_not_duplicated(tmp_path: Path, system_id: str) -> None:
    _minimal_project(tmp_path)
    run = _run(tmp_path, system_id)
    by_qn = {e.qualified_name: e for e in run.entities}

    model = by_qn["payments.models.Payment"]
    assert model.type is EntityType.SYMBOL  # still fundamentally a Python class
    assert model.metadata["framework"] == "sqlalchemy"
    assert model.metadata["sqlalchemy_kind"] == "model"
    assert len([e for e in run.entities if e.qualified_name == "payments.models.Payment"]) == 1


def test_table_is_a_distinct_data_entity_linked_by_maps_to(tmp_path: Path, system_id: str) -> None:
    """The one place this milestone's design discussion asked for two
    linked entities instead of one tagged entity: the ORM class and its
    table are not the same conceptual thing."""
    _minimal_project(tmp_path)
    run = _run(tmp_path, system_id)
    by_qn = {e.qualified_name: e for e in run.entities}

    model = by_qn["payments.models.Payment"]
    table = by_qn["payments"]
    assert table.type is EntityType.DATA_ENTITY
    assert table.id != model.id

    maps_to = [
        r
        for r in run.relationships
        if r.type is RelationshipType.MAPS_TO and r.source_entity_id == model.id
    ]
    assert any(r.target_entity_id == table.id for r in maps_to)


def test_columns_are_contained_by_their_table(tmp_path: Path, system_id: str) -> None:
    _minimal_project(tmp_path)
    run = _run(tmp_path, system_id)
    by_qn = {e.qualified_name: e for e in run.entities}

    table = by_qn["payments"]
    status_column = by_qn["payments.status"]
    assert status_column.type is EntityType.SYMBOL

    contains = [
        r
        for r in run.relationships
        if r.type is RelationshipType.CONTAINS and r.source_entity_id == table.id
    ]
    assert any(r.target_entity_id == status_column.id for r in contains)


def test_foreign_key_produces_a_references_edge_between_columns(
    tmp_path: Path, system_id: str
) -> None:
    _minimal_project(tmp_path)
    run = _run(tmp_path, system_id)
    by_qn = {e.qualified_name: e for e in run.entities}

    source = by_qn["payments.account_id"]
    target = by_qn["accounts.id"]
    assert target.metadata["is_primary_key"] is True

    references = [
        r
        for r in run.relationships
        if r.type is RelationshipType.REFERENCES and r.source_entity_id == source.id
    ]
    assert any(r.target_entity_id == target.id for r in references)


def test_field_access_produces_reads_and_writes(tmp_path: Path, system_id: str) -> None:
    _minimal_project(tmp_path)
    run = _run(tmp_path, system_id)
    by_qn = {e.qualified_name: e for e in run.entities}

    accessor = by_qn["payments.service.PaymentService.process"]
    column = by_qn["payments.status"]

    writes = [
        r
        for r in run.relationships
        if r.type is RelationshipType.WRITES
        and r.source_entity_id == accessor.id
        and r.target_entity_id == column.id
    ]
    reads = [
        r
        for r in run.relationships
        if r.type is RelationshipType.READS
        and r.source_entity_id == accessor.id
        and r.target_entity_id == column.id
    ]
    assert len(writes) == 1
    assert len(reads) == 1


def test_the_full_impact_chain_is_reachable_from_the_column(tmp_path: Path, system_id: str) -> None:
    """The actual killer test: walking from `payments.status` backward
    through non-structural edges reaches the service method that touches it
    directly -- entirely from the graph, no LLM involved. `CONTAINS` and
    `MAPS_TO` do not sit *between* the column and the code that touches it
    in this design (the WRITES/READS edge connects them directly), so this
    correctly does not need to pass through the table or the ORM class."""
    _minimal_project(tmp_path)
    run = _run(tmp_path, system_id)
    by_qn = {e.qualified_name: e for e in run.entities}
    by_id = {e.id: e for e in run.entities}

    column = by_qn["payments.status"]
    seen = {column.id}
    frontier = [column.id]
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

    affected = {by_id[i].qualified_name for i in seen if i != column.id}
    assert affected == {"payments.service.PaymentService.process"}


def test_unresolved_model_stays_unresolved(tmp_path: Path, system_id: str) -> None:
    """A defensive case mirroring Django's/FastAPI's equivalents: if the
    class somehow isn't in this run's candidates, the normalizer must not
    crash or invent one."""
    from hashira.core import Observation, Origin

    obs = Observation(
        system_id=system_id,
        adapter="sqlalchemy@0.1.0",
        origin=Origin.STATIC_ANALYSIS,
        kind="sqlalchemy.model",
        payload={
            "class_qualified_name": "nowhere.NoSuchModel",
            "module_qualified_name": "nowhere",
            "table_name": "nowhere_table",
            "base_qualified_name": "sqlalchemy.orm.DeclarativeBase",
        },
    )
    run = normalize([obs], system_id=system_id, revision="rev1")
    assert run.entities == []
    assert obs in run.unresolved


def test_boring_reindex_is_boring_for_sqlalchemy_entities_too(
    tmp_path: Path, system_id: str
) -> None:
    """The same DECLARATION_ANCHOR-based MATCHED invariant every other
    adapter's minted entities already have must hold for SQLAlchemy's table
    and column entities too."""
    from hashira.identity import resolve

    _minimal_project(tmp_path)
    run1 = _run(tmp_path, system_id)
    table1 = next(e for e in run1.entities if e.qualified_name == "payments")

    run2 = _run(tmp_path, system_id)
    table2 = next(e for e in run2.entities if e.qualified_name == "payments")

    decision = resolve(table2, existing=[table1])
    assert decision.outcome.value == "MATCHED"
    assert table1.status is EntityStatus.ACTIVE
