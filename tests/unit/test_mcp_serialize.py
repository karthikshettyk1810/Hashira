"""`mcp.serialize`: every result crossing the MCP boundary keeps its full
epistemic shape -- `.model_dump(mode="json")`, never a hand-picked subset."""

from __future__ import annotations

from hashira.application.history import HistoricalGraph
from hashira.application.impact import (
    CoverageStatus,
    ImpactCoverage,
    ImpactHop,
    ImpactPath,
    ImpactResult,
    LineageHop,
    LineageResult,
)
from hashira.core import (
    Confidence,
    Entity,
    EntityType,
    Evidence,
    KnowledgeClass,
    Origin,
    Relationship,
    RelationshipType,
    SourceRef,
    System,
)
from hashira.mcp.serialize import (
    serialize_entity,
    serialize_evidence,
    serialize_historical_graph,
    serialize_impact_result,
    serialize_lineage_result,
    serialize_relationship,
)

_SYSTEM = System(name="Checkout", slug="checkout-serialize")


def _entity(name: str) -> Entity:
    return Entity(system_id=_SYSTEM.id, type=EntityType.SYMBOL, name=name, qualified_name=name)


def _evidence() -> Evidence:
    return Evidence(
        system_id=_SYSTEM.id,
        origin=Origin.STATIC_ANALYSIS,
        source=SourceRef(provider="python-ast", reference="app.py"),
        summary="observed",
        locator="app.py:1",
    )


def _rel(source: Entity, target: Entity, evidence: Evidence) -> Relationship:
    return Relationship(
        system_id=_SYSTEM.id,
        source_entity_id=source.id,
        target_entity_id=target.id,
        type=RelationshipType.CALLS,
        origin=Origin.STATIC_ANALYSIS,
        evidence_ids=[evidence.id],
    )


def test_serialize_entity_is_lossless() -> None:
    entity = _entity("a")
    dumped = serialize_entity(entity)
    assert dumped == entity.model_dump(mode="json")
    assert dumped["id"] == entity.id
    assert dumped["type"] == "SYMBOL"


def test_serialize_relationship_keeps_epistemic_fields() -> None:
    a, b = _entity("a"), _entity("b")
    ev = _evidence()
    rel = _rel(a, b, ev)
    dumped = serialize_relationship(rel)
    assert dumped["knowledge_class"] == KnowledgeClass.OBSERVATION.value
    assert dumped["confidence"] == Confidence.CERTAIN.value
    assert dumped["evidence_ids"] == [ev.id]
    assert dumped["valid_from_revision"] == rel.valid_from_revision
    assert dumped["valid_until_revision"] == rel.valid_until_revision


def test_serialize_evidence_is_lossless() -> None:
    ev = _evidence()
    assert serialize_evidence(ev) == ev.model_dump(mode="json")


def test_serialize_impact_result_preserves_every_hop() -> None:
    a, b, c = _entity("a"), _entity("b"), _entity("c")
    ev = _evidence()
    hop1 = ImpactHop(relationship=_rel(a, b, ev), source=a, target=b, evidence=(ev,))
    hop2 = ImpactHop(relationship=_rel(b, c, ev), source=b, target=c, evidence=(ev,))
    path = ImpactPath(hops=(hop1, hop2), endpoint=c)
    coverage = ImpactCoverage(
        status=CoverageStatus.PARTIAL,
        unresolved_access_count=2,
        limitations=("raw SQL is not analyzed",),
    )
    result = ImpactResult(
        direction="forward",
        start=a,
        revision=None,
        paths=(path,),
        coverage=coverage,
        resolved_from=None,
    )

    dumped = serialize_impact_result(result)

    assert dumped["direction"] == "forward"
    assert dumped["start"]["id"] == a.id
    assert dumped["resolved_from"] is None
    assert len(dumped["paths"]) == 1
    assert len(dumped["paths"][0]["hops"]) == 2
    assert dumped["paths"][0]["hops"][0]["evidence"][0]["id"] == ev.id
    assert dumped["paths"][0]["endpoint"]["id"] == c.id
    assert dumped["affected_entity_ids"] == [c.id]
    assert dumped["coverage"] == {
        "status": "PARTIAL",
        "unresolved_access_count": 2,
        "limitations": ["raw SQL is not analyzed"],
    }


def test_serialize_lineage_result_preserves_predecessors_and_successors() -> None:
    old, new = _entity("old"), _entity("new")
    ev = _evidence()
    rel = Relationship(
        system_id=_SYSTEM.id,
        source_entity_id=new.id,
        target_entity_id=old.id,
        type=RelationshipType.SUPERSEDES,
        origin=Origin.STATIC_ANALYSIS,
        evidence_ids=[ev.id],
    )
    hop = LineageHop(relationship=rel, predecessor=old, successor=new, evidence=(ev,))
    result = LineageResult(start=old, predecessors=(), successors=(hop,))

    dumped = serialize_lineage_result(result)

    assert dumped["start"]["id"] == old.id
    assert dumped["predecessors"] == []
    assert len(dumped["successors"]) == 1
    assert dumped["successors"][0]["successor"]["id"] == new.id
    assert dumped["successors"][0]["evidence"][0]["id"] == ev.id


def test_serialize_historical_graph() -> None:
    a, b = _entity("a"), _entity("b")
    ev = _evidence()
    rel = _rel(a, b, ev)
    graph = HistoricalGraph(revision="deadbeef", entities=[a, b], relationships=[rel])

    dumped = serialize_historical_graph(graph)

    assert dumped["revision"] == "deadbeef"
    assert [e["id"] for e in dumped["entities"]] == [a.id, b.id]
    assert dumped["relationships"][0]["id"] == rel.id
