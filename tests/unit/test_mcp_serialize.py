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
from hashira.ports.adapters import Limitation, LimitationKind, LimitationScope

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
        limitations=(
            Limitation(
                kind=LimitationKind.RAW_SQL,
                scope=LimitationScope.RAW_SQL_REFERENCES,
                detail="raw SQL is not analyzed",
            ),
        ),
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
        "limitations": [
            {
                "kind": "RAW_SQL",
                "scope": "RAW_SQL_REFERENCES",
                "detail": "raw SQL is not analyzed",
            }
        ],
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


def test_serialize_entity_neighborhood() -> None:
    from hashira.application.graph import (
        CompactEntityRef,
        CompactEvidenceRef,
        CompactNeighborEdge,
        EntityNeighborhood,
    )
    from hashira.mcp.serialize import serialize_entity_neighborhood

    target = CompactEntityRef(
        id="ent_1", name="status", qualified_name="Payment.status", type="SYMBOL"
    )
    caller = CompactEntityRef(
        id="ent_2", name="checkout", qualified_name="CheckoutView", type="SYMBOL"
    )
    ev_ref = CompactEvidenceRef(source="views.py", line="12", summary="reads status")
    edge = CompactNeighborEdge(
        relationship_id="rel_1",
        type="READS",
        confidence="CERTAIN",
        entity=caller,
        evidence=(ev_ref,),
    )
    coverage = ImpactCoverage(status=CoverageStatus.COMPLETE, limitations=())
    neighborhood = EntityNeighborhood(
        entity=target,
        coverage=coverage,
        revision=None,
        incoming=(edge,),
        outgoing=(),
    )

    dumped = serialize_entity_neighborhood(neighborhood)

    assert dumped["coverage"]["status"] == "COMPLETE"
    assert dumped["target"]["id"] == "ent_1"
    assert dumped["target"]["qualified_name"] == "Payment.status"
    assert len(dumped["incoming"]) == 1
    assert dumped["incoming"][0]["relationship_id"] == "rel_1"
    assert dumped["incoming"][0]["type"] == "READS"
    assert dumped["incoming"][0]["confidence"] == "CERTAIN"
    assert dumped["incoming"][0]["entity"]["id"] == "ent_2"
    assert dumped["incoming"][0]["evidence"][0]["source"] == "views.py"
    assert dumped["outgoing"] == []


def test_serialize_impact_summary() -> None:
    from hashira.application.graph import CompactEntityRef, CompactEvidenceRef
    from hashira.application.impact import ImpactGroup, ImpactSummary, SemanticImpactItem
    from hashira.mcp.serialize import serialize_impact_summary

    target = CompactEntityRef(
        id="ent_1", name="status", qualified_name="Payment.status", type="SYMBOL"
    )
    caller = CompactEntityRef(
        id="ent_2", name="capture", qualified_name="capture_payment", type="SYMBOL"
    )
    ev_ref = CompactEvidenceRef(source="routers.py", line="45", summary="calls")
    item = SemanticImpactItem(
        entity=caller,
        relationship_id="rel_1",
        relationship_type="CALLS",
        confidence="CERTAIN",
        hops_count=1,
        evidence=(ev_ref,),
    )
    coverage = ImpactCoverage(status=CoverageStatus.COMPLETE, limitations=())
    group = ImpactGroup(
        key="app/routers",
        entity_count=1,
        path_count=1,
        representative_entity_id="ent_2",
        representative_display_name="capture_payment",
        entity_ids=("ent_2",),
    )
    summary = ImpactSummary(
        direction="reverse",
        target=target,
        start_id="ent_1",
        revision=None,
        resolved_from=None,
        affected_entity_count=1,
        path_count=1,
        coverage=coverage,
        direct_callers=(item,),
        direct_callees=(),
        readers=(),
        writers=(),
        framework_boundaries=(),
        indirect_dependencies=(),
        groups=(group,),
    )

    dumped = serialize_impact_summary(summary)

    assert dumped["coverage"]["status"] == "COMPLETE"
    assert dumped["target"]["id"] == "ent_1"
    assert len(dumped["direct_callers"]) == 1
    assert dumped["direct_callers"][0]["entity"]["name"] == "capture"
    assert dumped["direct_callers"][0]["relationship_type"] == "CALLS"
    assert dumped["direct_callers"][0]["evidence"][0]["source"] == "routers.py"
    assert dumped["groups"][0]["key"] == "app/routers"
