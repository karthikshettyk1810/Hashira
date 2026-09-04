"""Losslessly turning `application/` results into JSON-safe dicts.

The one rule this file exists to enforce: *"MCP must not make Hashira less
trustworthy than its Python API."* Every `core` IR record already has a
proven, lossless JSON form (`IRModel.model_dump(mode="json")` -- the exact
serialization `storage/sqlite` already trusts for persistence); this module
reuses that, never a hand-picked subset of fields. A `Relationship` crossing
this boundary still carries its `knowledge_class`, `confidence`, `origin`,
`evidence_ids`, and `valid_from_revision`/`valid_until_revision` -- the
epistemic state is the point, not an afterthought to summarize away (see
`application/impact.py`'s own module docstring on why).

The dataclasses `application/impact.py` defines (`ImpactResult`, `ImpactPath`,
`ImpactHop`, `LineageResult`, `LineageHop`) are not pydantic models, so they
need an explicit shape here -- built by hand, once, so every tool in
`server.py` returns the same structure for the same kind of result.
"""

from __future__ import annotations

from typing import Any

from ..application.history import HistoricalGraph
from ..application.impact import ImpactHop, ImpactPath, ImpactResult, LineageHop, LineageResult
from ..core.entities import Entity
from ..core.evidence import Evidence
from ..core.relationships import Relationship

__all__ = [
    "serialize_entity",
    "serialize_evidence",
    "serialize_historical_graph",
    "serialize_impact_result",
    "serialize_lineage_result",
    "serialize_relationship",
]


def serialize_entity(entity: Entity) -> dict[str, Any]:
    return entity.model_dump(mode="json")


def serialize_relationship(relationship: Relationship) -> dict[str, Any]:
    return relationship.model_dump(mode="json")


def serialize_evidence(evidence: Evidence) -> dict[str, Any]:
    return evidence.model_dump(mode="json")


def _serialize_impact_hop(hop: ImpactHop) -> dict[str, Any]:
    return {
        "relationship": serialize_relationship(hop.relationship),
        "source": serialize_entity(hop.source),
        "target": serialize_entity(hop.target),
        "evidence": [serialize_evidence(e) for e in hop.evidence],
    }


def _serialize_impact_path(path: ImpactPath) -> dict[str, Any]:
    return {
        "hops": [_serialize_impact_hop(h) for h in path.hops],
        "endpoint": serialize_entity(path.endpoint),
        "weakest_confidence": path.weakest_confidence.value,
        "has_speculative_hop": path.has_speculative_hop,
    }


def serialize_impact_result(result: ImpactResult) -> dict[str, Any]:
    """Every path, every hop -- no path enumeration limit imposed here
    beyond what `application.impact` itself already applies (one shortest
    path per reached entity). No prose, no ranking, no collapsing to a flat
    list of names.

    `coverage` crosses the wire too, and for the same reason everything
    else here does: a zero/short `paths` list must never read as "nothing
    else exists" when it might really mean "known analytical limitations
    apply" -- `application/impact.py`'s "coverage is not confidence" is a
    product decision, not just an internal one; an agent reading this JSON
    is exactly who needs to see the difference."""
    return {
        "direction": result.direction,
        "start": serialize_entity(result.start),
        "revision": result.revision,
        "resolved_from": result.resolved_from,
        "paths": [_serialize_impact_path(p) for p in result.paths],
        "affected_entity_ids": [e.id for e in result.affected_entities],
        "coverage": {
            "status": result.coverage.status.value,
            "unresolved_access_count": result.coverage.unresolved_access_count,
            "limitations": list(result.coverage.limitations),
        },
    }


def _serialize_lineage_hop(hop: LineageHop) -> dict[str, Any]:
    return {
        "relationship": serialize_relationship(hop.relationship),
        "predecessor": serialize_entity(hop.predecessor),
        "successor": serialize_entity(hop.successor),
        "evidence": [serialize_evidence(e) for e in hop.evidence],
    }


def serialize_lineage_result(result: LineageResult) -> dict[str, Any]:
    return {
        "start": serialize_entity(result.start),
        "predecessors": [_serialize_lineage_hop(h) for h in result.predecessors],
        "successors": [_serialize_lineage_hop(h) for h in result.successors],
    }


def serialize_historical_graph(graph: HistoricalGraph) -> dict[str, Any]:
    return {
        "revision": graph.revision,
        "entities": [serialize_entity(e) for e in graph.entities],
        "relationships": [serialize_relationship(r) for r in graph.relationships],
    }
