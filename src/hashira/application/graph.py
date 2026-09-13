"""Direct, id-keyed graph lookups (§24's `get_entity`/`get_relationships`
primitives) -- the small, boring queries every richer capability in this
package (`history.py`, `impact.py`) is ultimately built out of, pulled out
on their own so a transport layer (`hashira.mcp`) has something to call
that is neither "the whole storage port" nor "a bespoke traversal".

Kept deliberately thin: `get_entity` is a direct passthrough to
`GraphRepository.get_entity`, and `get_relationships` is `query_at_revision`
plus a direction/type filter -- the same pattern `application.impact`
already established for revision-aware queries, reused here instead of
re-derived.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..core.entities import Entity
from ..core.enums import RelationshipType
from ..core.ids import SystemID
from ..core.relationships import Relationship
from ..ports.repositories import UnitOfWork
from .history import query_at_revision

if TYPE_CHECKING:
    from .impact import ImpactCoverage

__all__ = [
    "CompactEntityRef",
    "CompactEvidenceRef",
    "CompactNeighborEdge",
    "EntityNeighborhood",
    "get_entity",
    "get_entity_at_revision",
    "get_entity_neighborhood",
    "get_relationships",
]


@dataclass(frozen=True, slots=True)
class CompactEntityRef:
    """A minimal, non-hydrated entity reference for compact projections."""

    id: str
    name: str
    qualified_name: str | None
    type: str


@dataclass(frozen=True, slots=True)
class CompactEvidenceRef:
    """A minimal evidence reference pointing to source location."""

    source: str | None
    line: str | None
    summary: str | None


@dataclass(frozen=True, slots=True)
class CompactNeighborEdge:
    """A single edge in an entity's 1-hop neighborhood."""

    relationship_id: str
    type: str
    confidence: str
    entity: CompactEntityRef
    evidence: tuple[CompactEvidenceRef, ...]


@dataclass(frozen=True, slots=True)
class EntityNeighborhood:
    """The 1-hop semantic neighborhood of an entity, including incoming/outgoing
    edges with compact endpoints, confidence, evidence, and coverage."""

    entity: CompactEntityRef
    coverage: ImpactCoverage
    revision: str | None
    incoming: tuple[CompactNeighborEdge, ...]
    outgoing: tuple[CompactNeighborEdge, ...]


def get_entity(uow: UnitOfWork, *, entity_id: str) -> Entity | None:
    """The entity's own stored state, by its opaque id -- whatever status
    it currently carries (`ACTIVE`, `SUPERSEDED`, ...), not a revision-scoped
    reconstruction. Use `get_entity_at_revision` to ask "was this present,
    and how, as of revision X"."""
    return uow.graph.get_entity(entity_id)


def get_entity_at_revision(
    uow: UnitOfWork, *, system_id: SystemID, entity_id: str, revision: str | None
) -> Entity | None:
    """Whether `entity_id` was present as of `revision` (`None` means
    today), and its state then, per `application.history.query_at_revision`
    -- `None` if it was not present at that point, distinct from `get_entity`
    which always returns whatever is in storage regardless of revision."""
    graph = query_at_revision(uow, system_id=system_id, revision=revision)
    return next((e for e in graph.entities if e.id == entity_id), None)


def get_relationships(
    uow: UnitOfWork,
    *,
    system_id: SystemID,
    entity_id: str,
    direction: str = "out",
    types: Sequence[RelationshipType] | None = None,
    revision: str | None = None,
) -> list[Relationship]:
    """Every relationship touching `entity_id`, in the given direction
    (`"out"`, `"in"`, or anything else meaning both), optionally restricted
    to specific types, as of `revision` (`None` means current) -- built on
    `query_at_revision` for both cases uniformly rather than a separate,
    non-historical code path."""
    graph = query_at_revision(uow, system_id=system_id, revision=revision)

    def matches(rel: Relationship) -> bool:
        if direction == "out" and rel.source_entity_id != entity_id:
            return False
        if direction == "in" and rel.target_entity_id != entity_id:
            return False
        if direction not in ("out", "in") and entity_id not in (
            rel.source_entity_id,
            rel.target_entity_id,
        ):
            return False
        return types is None or rel.type in types

    return [rel for rel in graph.relationships if matches(rel)]


def get_entity_neighborhood(
    uow: UnitOfWork,
    *,
    system_id: SystemID,
    entity_id: str,
    revision: str | None = None,
) -> EntityNeighborhood | None:
    """Return the 1-hop semantic neighborhood of `entity_id` as of `revision`:
    its own identity, incoming and outgoing edges with compact endpoints,
    relationship confidence, compact evidence, and coverage."""
    from .impact import get_coverage_for

    graph = query_at_revision(uow, system_id=system_id, revision=revision)
    target = next((e for e in graph.entities if e.id == entity_id), None)
    if target is None:
        target = uow.graph.get_entity(entity_id)
        if target is None:
            return None

    coverage = get_coverage_for(uow, system_id=system_id, start=target)
    target_ref = CompactEntityRef(
        id=target.id,
        name=target.name,
        qualified_name=target.qualified_name,
        type=target.type.value,
    )

    incoming_rels = [r for r in graph.relationships if r.target_entity_id == entity_id]
    outgoing_rels = [r for r in graph.relationships if r.source_entity_id == entity_id]

    entities_by_id = {e.id: e for e in graph.entities}
    needed_entity_ids = {
        r.source_entity_id for r in incoming_rels
    } | {
        r.target_entity_id for r in outgoing_rels
    }
    for missing_id in needed_entity_ids - entities_by_id.keys():
        if (found := uow.graph.get_entity(missing_id)) is not None:
            entities_by_id[missing_id] = found

    evidence_ids = [eid for r in (*incoming_rels, *outgoing_rels) for eid in r.evidence_ids]
    evidences = uow.evidence.get_many(evidence_ids) if evidence_ids else ()
    evidence_by_id = {ev.id: ev for ev in evidences}

    def _compact_edge(rel: Relationship, other_entity_id: str) -> CompactNeighborEdge:
        other = entities_by_id.get(other_entity_id)
        if other is not None:
            other_ref = CompactEntityRef(
                id=other.id,
                name=other.name,
                qualified_name=other.qualified_name,
                type=other.type.value,
            )
        else:
            other_ref = CompactEntityRef(
                id=other_entity_id,
                name=other_entity_id,
                qualified_name=None,
                type="UNKNOWN",
            )
        edge_evidence = tuple(
            CompactEvidenceRef(
                source=ev.source.reference if ev.source else None,
                line=ev.locator,
                summary=ev.summary,
            )
            for eid in rel.evidence_ids
            if (ev := evidence_by_id.get(eid)) is not None
        )
        return CompactNeighborEdge(
            relationship_id=rel.id,
            type=rel.type.value,
            confidence=rel.confidence.value if rel.confidence is not None else "CERTAIN",
            entity=other_ref,
            evidence=edge_evidence,
        )

    incoming = tuple(_compact_edge(r, r.source_entity_id) for r in incoming_rels)
    outgoing = tuple(_compact_edge(r, r.target_entity_id) for r in outgoing_rels)

    return EntityNeighborhood(
        entity=target_ref,
        coverage=coverage,
        revision=revision,
        incoming=incoming,
        outgoing=outgoing,
    )

