"""Impact Analysis v0.1: can Hashira reason over the system it has indexed,
not merely store it? (§25)

Deterministic system reasoning, not LLM reasoning. Two directions over the
same graph `application/history.py` already knows how to materialize at any
revision:

* `reverse_impact(x)` -- what, transitively, depends on `x`? ("if I change
  `Payment.status`, what breaks?")
* `forward_impact(x)` -- what does `x`, transitively, depend on? ("if I
  change `CheckoutView`, what does it touch downstream?")

Both walk `core.relationships.IMPACT_EDGES` only (§25: "structural containment
and history are deliberately excluded — everything CONTAINS everything
eventually") -- the exact exclusion every ad-hoc `_reverse_impact` test
helper across this codebase (`test_django_identity.py`, `test_fastapi_
sqlalchemy_together.py`, ...) has been reimplementing by hand since the
Django milestone. This module is that helper, finally made real, revision-
aware, and identity-lineage-aware.

## Explainable paths, not a set of names

A result is not `{"affected": ["Payment.status", "Payment"]}`. It is a
sequence of `ImpactHop`s -- one per traversed `Relationship` -- each
carrying the entities on both ends, the relationship's own type, evidence,
origin, revision, and epistemic status (`knowledge_class`/`confidence`)
exactly as stored, unmodified. Hashira's differentiator was never "what it
believes"; it is "why the system representation says that" -- and that
"why" only survives if this layer stops summarizing it away.

## Path confidence is not a thing this module invents

`Relationship.confidence` already exists, deliberately ordinal
(`core/enums.py::Confidence` — `docs/IR.md`'s reasoning against a float
scheme applies here too). Collapsing a path's several, independently-sourced
per-hop confidences into one number for the whole path would smuggle back
exactly the false precision that ordinal type was built to refuse: a
CERTAIN `CALLS` hop followed by a SPECULATIVE `RELATED_TO` hop must not
read as one "pretty confident" path. `ImpactPath` exposes the hops; the one
convenience this module adds, `weakest_confidence`, is a plain minimum over
the *existing* per-hop values (`Confidence.rank`, whose own docstring says
it exists "for sorting and thresholds only") -- never a new score.

## Identity-lineage-aware, on purpose

A caller may ask about an entity id that a later revision superseded (a
rename, e.g.). `resolve_identity` follows `SUPERSEDES` lineage forward to
whatever currently represents that construct at the requested revision, so
`reverse_impact`/`forward_impact` keep working across a rename instead of
silently returning nothing. `SUPERSEDES` is deliberately *not* one of
`IMPACT_EDGES` (lineage is not the same claim as "affects") — this is a
separate, explicit resolution step, reported on the result
(`ImpactResult.resolved_from`) rather than folded invisibly into the walk.

## Where this lives, and why

`application/`, not `core/`. Core knows entities, relationships, evidence,
revisions, snapshots. This module knows how to *traverse* those primitives
to answer a system question — an application concern, not a domain one
(§18's boundary applied one layer up). `ImpactAnalyzer` is a thin, stateful
convenience over the module-level functions below (matching the shape a
caller actually wants: `analyzer.reverse_impact(entity_id=...)`), not a
second implementation of them.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ..core.entities import Entity
from ..core.enums import Confidence, RelationshipType
from ..core.evidence import Evidence
from ..core.ids import SystemID
from ..core.relationships import IMPACT_EDGES, Relationship
from ..ports.repositories import UnitOfWork
from .history import HistoricalGraph, query_at_revision

__all__ = [
    "ImpactAnalyzer",
    "ImpactHop",
    "ImpactPath",
    "ImpactResult",
    "forward_impact",
    "resolve_identity",
    "reverse_impact",
]


@dataclass(frozen=True, slots=True)
class ImpactHop:
    """One traversed edge: the relationship exactly as stored, plus the
    entities on both ends and whatever evidence backs it, resolved once so
    a caller never has to look either up separately. `source`/`target`
    always match `relationship.source_entity_id`/`target_entity_id` -- a
    reverse-impact hop is never flipped to read "start to affected"; the
    edge means what it always meant."""

    relationship: Relationship
    source: Entity
    target: Entity
    evidence: tuple[Evidence, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ImpactPath:
    """One shortest path from the query's start entity to one affected (or
    depended-on) entity. v0.1 keeps exactly one path per reached entity
    (the first BFS finds); it does not enumerate every path between two
    entities, matching this milestone's "boring first" scope."""

    hops: tuple[ImpactHop, ...]
    endpoint: Entity
    """The entity this path reaches -- not necessarily `hops[-1].target`:
    for a reverse-impact path, the newly-reached entity at each hop is the
    relationship's *source* (walking backward), so `endpoint` is computed
    once at construction rather than asking the caller to know which field
    to read for which direction."""

    @property
    def weakest_confidence(self) -> Confidence:
        """The minimum `Confidence` among this path's own hops -- not a
        synthesized path-level score (see the module docstring)."""
        return min(hop.relationship.confidence or Confidence.CERTAIN for hop in self.hops)

    @property
    def has_speculative_hop(self) -> bool:
        return self.weakest_confidence is Confidence.SPECULATIVE


@dataclass(frozen=True, slots=True)
class ImpactResult:
    direction: str
    """"reverse" or "forward"."""
    start: Entity
    revision: str | None
    paths: tuple[ImpactPath, ...]
    resolved_from: str | None = None
    """Set when the requested `entity_id` was not present at `revision` and
    `resolve_identity` followed `SUPERSEDES` lineage to `start` instead --
    the original id asked for, kept so the caller can see the substitution
    happened rather than silently querying something else."""

    @property
    def affected_entities(self) -> tuple[Entity, ...]:
        return tuple(path.endpoint for path in self.paths)


def resolve_identity(graph: HistoricalGraph, entity_id: str) -> Entity | None:
    """Whatever currently (as of `graph.revision`) represents `entity_id` --
    itself, if present in `graph`; otherwise whatever a chain of
    `SUPERSEDES` edges leads to. `graph.relationships` still carries a
    `SUPERSEDES` edge even when its (superseded) target was filtered out of
    `graph.entities` (`application.history._bounded`'s documented exemption
    for lineage edges) -- that is what makes this resolvable without a
    second, unfiltered query.

    Returns `None` if `entity_id` names nothing reachable from `graph` at
    all -- it never existed, or lineage runs out without reaching anything
    present at this revision.
    """
    present_by_id = {e.id: e for e in graph.entities}
    if entity_id in present_by_id:
        return present_by_id[entity_id]

    seen = {entity_id}
    current_id = entity_id
    while True:
        successor = next(
            (
                rel
                for rel in graph.relationships
                if rel.type is RelationshipType.SUPERSEDES and rel.target_entity_id == current_id
            ),
            None,
        )
        if successor is None or successor.source_entity_id in seen:
            return None
        current_id = successor.source_entity_id
        seen.add(current_id)
        if current_id in present_by_id:
            return present_by_id[current_id]


def reverse_impact(
    uow: UnitOfWork,
    *,
    system_id: SystemID,
    entity_id: str,
    revision: str | None = None,
    edge_types: frozenset[RelationshipType] | None = None,
) -> ImpactResult:
    """What, directly or transitively, depends on `entity_id`? Walks edges
    backward: for `A --CALLS--> B`, asking about `B` reaches `A`."""
    return _impact(
        uow,
        system_id=system_id,
        entity_id=entity_id,
        revision=revision,
        edge_types=edge_types,
        direction="reverse",
    )


def forward_impact(
    uow: UnitOfWork,
    *,
    system_id: SystemID,
    entity_id: str,
    revision: str | None = None,
    edge_types: frozenset[RelationshipType] | None = None,
) -> ImpactResult:
    """What does `entity_id`, directly or transitively, depend on? Walks
    edges forward: for `A --CALLS--> B`, asking about `A` reaches `B`."""
    return _impact(
        uow,
        system_id=system_id,
        entity_id=entity_id,
        revision=revision,
        edge_types=edge_types,
        direction="forward",
    )


def _impact(
    uow: UnitOfWork,
    *,
    system_id: SystemID,
    entity_id: str,
    revision: str | None,
    edge_types: frozenset[RelationshipType] | None,
    direction: str,
) -> ImpactResult:
    graph = query_at_revision(uow, system_id=system_id, revision=revision)
    types = edge_types if edge_types is not None else IMPACT_EDGES

    resolved_from: str | None = None
    present_by_id = {e.id: e for e in graph.entities}
    start = present_by_id.get(entity_id)
    if start is None:
        start = resolve_identity(graph, entity_id)
        if start is None:
            raise KeyError(
                f"{entity_id!r} is not a known entity in system {system_id!r}"
                + (f" at revision {revision!r}" if revision else "")
            )
        resolved_from = entity_id

    by_id = present_by_id
    adjacency: dict[str, list[Relationship]] = defaultdict(list)
    for rel in graph.relationships:
        if rel.type not in types:
            continue
        key = rel.target_entity_id if direction == "reverse" else rel.source_entity_id
        adjacency[key].append(rel)

    hops_by_entity_id: dict[str, tuple[ImpactHop, ...]] = {start.id: ()}
    frontier = [start.id]
    while frontier:
        next_frontier: list[str] = []
        for current_id in frontier:
            current_hops = hops_by_entity_id[current_id]
            for rel in adjacency[current_id]:
                neighbor_id = (
                    rel.source_entity_id if direction == "reverse" else rel.target_entity_id
                )
                if neighbor_id in hops_by_entity_id:
                    continue
                if neighbor_id not in by_id:
                    continue
                hop = ImpactHop(
                    relationship=rel,
                    source=by_id[rel.source_entity_id],
                    target=by_id[rel.target_entity_id],
                )
                hops_by_entity_id[neighbor_id] = (*current_hops, hop)
                next_frontier.append(neighbor_id)
        frontier = next_frontier

    del hops_by_entity_id[start.id]

    all_evidence_ids = {
        eid
        for hops in hops_by_entity_id.values()
        for hop in hops
        for eid in hop.relationship.evidence_ids
    }
    evidence_by_id = (
        {ev.id: ev for ev in uow.evidence.get_many(list(all_evidence_ids))}
        if all_evidence_ids
        else {}
    )

    def _with_evidence(hop: ImpactHop) -> ImpactHop:
        if not hop.relationship.evidence_ids:
            return hop
        return ImpactHop(
            relationship=hop.relationship,
            source=hop.source,
            target=hop.target,
            evidence=tuple(
                evidence_by_id[eid]
                for eid in hop.relationship.evidence_ids
                if eid in evidence_by_id
            ),
        )

    paths = tuple(
        ImpactPath(hops=tuple(_with_evidence(hop) for hop in hops), endpoint=by_id[entity_id_])
        for entity_id_, hops in hops_by_entity_id.items()
    )

    return ImpactResult(
        direction=direction,
        start=start,
        revision=revision,
        paths=paths,
        resolved_from=resolved_from,
    )


@dataclass(frozen=True, slots=True)
class ImpactAnalyzer:
    """A thin, stateful convenience over the module-level functions above,
    bound to one system -- `analyzer.reverse_impact(entity_id=...)` rather
    than threading `uow`/`system_id` through every call. The functions
    themselves remain the real implementation and stay independently
    testable/importable (matching `application.history`'s own style)."""

    uow: UnitOfWork
    system_id: SystemID

    def reverse_impact(
        self,
        entity_id: str,
        *,
        revision: str | None = None,
        edge_types: frozenset[RelationshipType] | None = None,
    ) -> ImpactResult:
        return reverse_impact(
            self.uow,
            system_id=self.system_id,
            entity_id=entity_id,
            revision=revision,
            edge_types=edge_types,
        )

    def forward_impact(
        self,
        entity_id: str,
        *,
        revision: str | None = None,
        edge_types: frozenset[RelationshipType] | None = None,
    ) -> ImpactResult:
        return forward_impact(
            self.uow,
            system_id=self.system_id,
            entity_id=entity_id,
            revision=revision,
            edge_types=edge_types,
        )
