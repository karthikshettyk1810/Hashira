"""Revision-scoped queries: what did the graph look like at revision R? (§13)

Deliberately the boring implementation the design discussion asked for: no
incremental graph reconstruction, no graph-versioning engine, no branch-aware
merge semantics. `query_at_revision` loads today's fully materialized graph
(everything Hashira has ever recorded for the system) and *filters* it
against each record's revision-keyed validity, using real ancestry
(`core.revisions.RevisionGraph`) rather than string equality or wall-clock
time to decide whether revision `R` had "arrived" at a given fact.

Two revision-keyed facts already exist on every record and are the entire
input to the filter:

* An entity's `first_seen_revision` says when it entered the graph. If it was
  later superseded (`identity.resolver.apply`'s ``SUPERSEDES`` branch), the
  lineage `Relationship` recording that carries `valid_from_revision` — the
  entity's *end* point, which `Entity` itself does not track directly (§10:
  superseding never rewrites the old entity beyond flipping its status).
* A relationship's `valid_from_revision`/`valid_until_revision` say when an
  edge started and (if ever) stopped being observed (`_reconcile_relationships`
  in `application/indexing.py`).

"Was R at or after X" is ancestry, not string equality or `<=` on shas — a
sha string carries no order on its own. `RevisionGraph.is_ancestor_or_self`
is what makes that comparison correct across branches instead of merely
looking plausible on a linear history.

This is a second, higher-level mechanism, deliberately not a retrofit of
`GraphRepository.find_entities(revision=...)` / `get_relationships(revision=...)`
— those port methods still raise `NotImplementedError` (see the storage
backends): they are reserved for a future, storage-native, indexed
implementation that does not need to load an entire system's graph into
memory per query. This module gets the semantics right first, at
fixture/demo scale, per the explicit instruction not to optimize yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.entities import Entity
from ..core.enums import EntityStatus, RelationshipType
from ..core.ids import SystemID
from ..core.relationships import Relationship
from ..core.revisions import RevisionGraph
from ..ports.repositories import UnitOfWork

__all__ = ["HistoricalGraph", "query_at_revision"]

#: Matches `application.indexing._ALL_ENTITIES_LIMIT`: fixture/demo scale,
#: not yet meant for a repository large enough to need pagination here.
_ALL_ENTITIES_LIMIT = 1_000_000


@dataclass(frozen=True, slots=True)
class HistoricalGraph:
    """The graph as of one revision — or, when `revision` is ``None``,
    today's current graph (the same set an ordinary, non-revision-scoped
    query already returns)."""

    revision: str | None
    entities: list[Entity] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)

    def entity(self, qualified_name: str) -> Entity | None:
        return next((e for e in self.entities if e.qualified_name == qualified_name), None)


def query_at_revision(
    uow: UnitOfWork, *, system_id: SystemID, revision: str | None
) -> HistoricalGraph:
    """Materialize the graph as of `revision`.

    `revision=None` means today's current state: entities not superseded,
    edges still `is_current` — exactly what an ordinary query already means.
    A concrete revision instead asks "as of the point in history identified
    by this sha, using recorded Git ancestry to place it relative to every
    fact's own revision-keyed validity" — not "as of today, restricted to
    files that happen to still exist," and not a wall-clock comparison.
    """
    all_entities = list(uow.graph.find_entities(system_id, limit=_ALL_ENTITIES_LIMIT))
    all_relationships = [
        rel for e in all_entities for rel in uow.graph.get_relationships(e.id, direction="out")
    ]

    if revision is None:
        entities = [
            e
            for e in all_entities
            if e.status not in (EntityStatus.SUPERSEDED, EntityStatus.REMOVED)
        ]
        relationships = [rel for rel in all_relationships if rel.is_current]
        return _bounded(revision, entities, relationships)

    graph = RevisionGraph(list(uow.revisions.find(system_id)))
    superseded_at = _superseded_at_revision(all_relationships)

    def entity_present(entity: Entity) -> bool:
        if entity.first_seen_revision is None:
            # No revision info to filter on -- an absent fact is not
            # grounds to exclude it (§12); it is present at every revision.
            return True
        if not graph.is_ancestor_or_self(entity.first_seen_revision, revision):
            return False
        closed_at = superseded_at.get(entity.id)
        if closed_at is not None and graph.is_ancestor_or_self(closed_at, revision):
            return False
        return not (
            entity.status is EntityStatus.REMOVED
            and isinstance(entity.metadata.get("removed_at_revision"), str)
            and graph.is_ancestor_or_self(str(entity.metadata["removed_at_revision"]), revision)
        )

    def relationship_present(rel: Relationship) -> bool:
        if rel.valid_from_revision is None:
            return True
        if not graph.is_ancestor_or_self(rel.valid_from_revision, revision):
            return False
        if rel.valid_until_revision is None:
            return True
        return not graph.is_ancestor_or_self(rel.valid_until_revision, revision)

    entities = [e for e in all_entities if entity_present(e)]
    relationships = [rel for rel in all_relationships if relationship_present(rel)]
    return _bounded(revision, entities, relationships)


def _superseded_at_revision(relationships: list[Relationship]) -> dict[str, str]:
    """Entity id -> the revision its ``SUPERSEDES`` lineage edge recorded as
    the point it stopped being current. `Entity.status` alone cannot answer
    this: superseding an entity flips its status but leaves
    `last_seen_revision` untouched (`identity/resolver.py::apply`) —
    the lineage relationship is the only record of *when*."""
    result: dict[str, str] = {}
    for rel in relationships:
        if rel.type is RelationshipType.SUPERSEDES and rel.valid_from_revision is not None:
            result[rel.target_entity_id] = rel.valid_from_revision
    return result


def _bounded(
    revision: str | None, entities: list[Entity], relationships: list[Relationship]
) -> HistoricalGraph:
    """Drop edges whose endpoint was filtered out — an edge is only part of
    the historical graph if both entities it connects were too.

    ``SUPERSEDES`` is the deliberate exception: its entire purpose is to
    point from a currently-present entity back at one that, by definition,
    is not (§10) — requiring the target to be present too would silently
    drop every lineage edge the moment a query reaches or passes the
    revision it was minted at, which is exactly when it matters most.
    """
    present_ids = {e.id for e in entities}
    kept = [
        rel
        for rel in relationships
        if rel.source_entity_id in present_ids
        and (rel.type is RelationshipType.SUPERSEDES or rel.target_entity_id in present_ids)
    ]
    return HistoricalGraph(revision=revision, entities=entities, relationships=kept)
