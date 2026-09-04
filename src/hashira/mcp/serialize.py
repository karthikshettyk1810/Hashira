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
from ..application.impact import (
    ImpactCoverage,
    ImpactHop,
    ImpactPath,
    ImpactResult,
    ImpactSummary,
    LineageHop,
    LineageResult,
)
from ..core.entities import Entity
from ..core.evidence import Evidence
from ..core.relationships import Relationship

__all__ = [
    "serialize_entity",
    "serialize_evidence",
    "serialize_historical_graph",
    "serialize_impact_result",
    "serialize_impact_summary",
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


def _serialize_coverage(coverage: ImpactCoverage) -> dict[str, Any]:
    return {
        "status": coverage.status.value,
        "limitations": [
            {
                "kind": limitation.kind.value,
                "scope": limitation.scope.value,
                "detail": limitation.detail,
            }
            for limitation in coverage.limitations
        ],
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
    is exactly who needs to see the difference. `limitations` is a list of
    typed categories (`{"kind", "scope", "detail"}`), not a count or free
    text -- useful whether one entity or ten thousand are affected by a
    given kind; group/filter on `kind`, read `detail` for a human.

    Deliberately still lossless, byte cost and all -- `serialize_impact_summary`
    (below) is the projection for a caller that wants a small, navigable
    result instead; this stays the authoritative, detailed form."""
    return {
        "direction": result.direction,
        "start": serialize_entity(result.start),
        "revision": result.revision,
        "resolved_from": result.resolved_from,
        "paths": [_serialize_impact_path(p) for p in result.paths],
        "affected_entity_ids": [e.id for e in result.affected_entities],
        "coverage": _serialize_coverage(result.coverage),
    }


def serialize_impact_summary(summary: ImpactSummary) -> dict[str, Any]:
    """`coverage` first, deliberately, matching `ImpactSummary`'s own
    field order: a caller's first question should be "how much should I
    trust this" before "what's in it" (`application/impact.py`'s "Impact
    Presentation v0.1" entry). Every entity named in `groups` is a bare
    `{"entity_id", "display_name"}` pair, never a full record -- `get_entity`/
    `get_relationships` are the drill-down, not this response."""
    return {
        "direction": summary.direction,
        "start_id": summary.start_id,
        "revision": summary.revision,
        "resolved_from": summary.resolved_from,
        "coverage": _serialize_coverage(summary.coverage),
        "affected_entity_count": summary.affected_entity_count,
        "path_count": summary.path_count,
        "groups": [
            {
                "key": group.key,
                "entity_count": group.entity_count,
                "path_count": group.path_count,
                "representative": {
                    "entity_id": group.representative_entity_id,
                    "display_name": group.representative_display_name,
                },
                "entity_ids": list(group.entity_ids),
            }
            for group in summary.groups
        ],
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
