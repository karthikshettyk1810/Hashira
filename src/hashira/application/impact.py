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

## Coverage is not confidence (Impact Analysis v0.2/v0.3)

A correct traversal is not automatically a complete answer. `reverse_impact`
returning two paths for `Payment.status` is a true statement about the
edges Hashira's adapters actually captured — it is not the same claim as
"nothing else in the system touches `Payment.status`", and collapsing those
two claims into one silent result is exactly the failure a real agent
experiment (`docs/ROADMAP.md`'s entry on this milestone) surfaced: a
capable agent had to redo the entire investigation by hand because a
too-small result gave no signal that it might be too small.

`Confidence` (`core/enums.py`) answers "how strong is the evidence *for a
relationship that exists*" — it says nothing about relationships that were
never captured at all. `ImpactCoverage` is the missing, orthogonal axis:
"how much of the relevant surface did this analysis actually examine."
Three states, not two, describe any one candidate access site:

* **FOUND** — a relationship was captured; it appears in `paths` with its
  own, unmodified `Confidence`.
* **NOT_OBSERVABLE** — the surface is structurally invisible to every
  configured adapter (raw SQL, an unindexed external SDK, a dynamically
  computed attribute name) — reported as a `Limitation` (`ports/adapters.py`)
  in `ImpactCoverage.limitations`, sourced from `AdapterCapabilities.
  known_limitations`, never silently absent.
* **recognized-but-unresolved** — an adapter saw *something* plausibly
  relevant (an attribute name matching a real column) but could not
  determine the accessing object's type through any provenance form it
  supports — also a `Limitation` in `ImpactCoverage.limitations`, one per
  distinct *kind* of gap triggered for this entity (`UNTYPED_PARAMETER`,
  `RETURN_VALUE_PROVENANCE`, ...), never a raw count. This is the honest,
  narrower stand-in for the ideal "examined and confirmed absent"
  (`NOT_FOUND_AFTER_COVERAGE`) state described in `docs/IR.md`: not a
  rigorous exhaustive-search proof, only real, adapter-reported categories
  of access recognized as ambiguous rather than absent — see that doc's
  entry for the full three-state framework this is a first, partial
  implementation of.

**A limitation describes a capability boundary, not an individual missed
edge** (v0.3, replacing v0.2's flat `unresolved_access_count`). A category
like `RETURN_VALUE_PROVENANCE` stays exactly as useful whether it applies
to one access or ten thousand — a count invites the same false-precision
reading `Confidence`'s own ordinal design refuses (see below), just one
level up: "3 unresolved" reads as "probably fine," which is not a claim
this module is entitled to make.

**Two invariants this module (and every caller) must keep**, per
`docs/IR.md`:

1. Every `ImpactResult` communicates whether its paths represent complete
   analysis or known analytical limitations — zero paths never reads as
   "nothing else exists" without `coverage` saying so explicitly.
2. Coverage must never modify an individual relationship's `Confidence`. A
   `CERTAIN` `CALLS` edge stays `CERTAIN` even inside a result whose overall
   `coverage.status` is `PARTIAL` — weakening per-edge confidence to stand
   in for incomplete analysis would quietly turn `Confidence` into the
   "junk drawer" it was explicitly designed never to become.

## Impact Presentation v0.1: projection, not a second source of truth

A stress-test agent experiment (`docs/IR.md`'s entry on this milestone) put
a 35-path `reverse_impact` result in front of a real agent and watched it
build its own tooling in response: a script to flatten 373KB of
per-hop-duplicated entity envelopes into something readable, and a second,
manual pass grouping the flattened list by directory because the flat
path list gave it no structure to lean on. It said as much, unprompted:
*"a named-cluster grouping would likely have both sped up my synthesis
and made the one real gap I found easier to spot sooner."*

`summarize_impact` (this module) and the `summarize_impact` MCP tool exist
to do what that agent built by hand, so it doesn't have to. Two rules keep
this from becoming a second, competing notion of truth:

1. **`ImpactResult` does not change.** `reverse_impact`/`forward_impact`
   stay exactly as lossless as before — every path, every hop, every full
   entity, unmodified. `summarize_impact` is computed *from* an existing
   `ImpactResult`, never a new traversal, and never invents a relationship
   the traversal didn't already find. It can group, deduplicate, count,
   and reference; it cannot infer.
2. **Every entity a summary names is a bare id + qualified name, not a
   full record.** A summary is for deciding *where to look next*, not for
   reading in place — `get_entity`/`get_relationships` (already-proven MCP
   tools) are the drill-down, so a summary never needs to carry
   `identity_claims`/`metadata`/evidence just to be useful. The
   invariant this guarantees, and that
   `test_summarize_impact_never_invents_an_entity_id`
   exists to hold permanently: every id a summary names is recoverable,
   unchanged, from the canonical `ImpactResult` it was computed from.

**Grouping is structural, not a layer taxonomy.** `_group_key` groups
affected entities by the directory their own source file lives in
(`Entity.source.file`'s parent) — already-present IR data, not a new
`API_LAYER`/`SERVICE_LAYER` vocabulary invented because one stress
fixture happened to cluster into routers/services/repositories/workers/
tests. A codebase organized around commands/events/consumers/projections
groups just as naturally, by its own directory structure, with zero
change here. No importance ranking, either — `ImpactGroup` orders by
`entity_count` descending and nothing else; deciding a service matters
more than a test is the agent's call, not this module's to make for it.
"""

from __future__ import annotations

import posixpath
from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum

from ..core.entities import Entity
from ..core.enums import Confidence, RelationshipType
from ..core.evidence import Evidence
from ..core.ids import SystemID
from ..core.relationships import IMPACT_EDGES, Relationship
from ..ports.adapters import Limitation, LimitationKind, LimitationScope
from ..ports.repositories import UnitOfWork
from .graph import CompactEntityRef, CompactEvidenceRef
from .history import HistoricalGraph, query_at_revision

__all__ = [
    "CoverageStatus",
    "ImpactAnalyzer",
    "ImpactCoverage",
    "ImpactGroup",
    "ImpactHop",
    "ImpactPath",
    "ImpactResult",
    "ImpactSummary",
    "LineageHop",
    "LineageResult",
    "SemanticImpactItem",
    "follow_lineage",
    "forward_impact",
    "get_coverage_for",
    "resolve_identity",
    "reverse_impact",
    "summarize_impact",
]

#: Matches `application.indexing._ALL_ENTITIES_LIMIT`: fixture/demo scale.
_ALL_ENTITIES_LIMIT = 1_000_000


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
    coverage: ImpactCoverage
    resolved_from: str | None = None
    """Set when the requested `entity_id` was not present at `revision` and
    `resolve_identity` followed `SUPERSEDES` lineage to `start` instead --
    the original id asked for, kept so the caller can see the substitution
    happened rather than silently querying something else."""

    @property
    def affected_entities(self) -> tuple[Entity, ...]:
        return tuple(path.endpoint for path in self.paths)


class CoverageStatus(StrEnum):
    """`COMPLETE`: `limitations` is empty -- nothing in this run's evidence
    points at an unresolved or structurally-invisible surface relevant to
    this query. `PARTIAL`: at least one `Limitation` applies. `PARTIAL` is
    the expected, honest answer for almost any real system today (raw SQL
    blindness alone guarantees it whenever a SQLAlchemy-backed system is
    involved) -- it is not an alarm, it is the truth this module refuses
    to hide (module docstring's "coverage is not confidence")."""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


@dataclass(frozen=True, slots=True)
class ImpactCoverage:
    """What this analysis actually examined, distinct from what it found.

    `limitations` combines two sources, undifferentiated -- a caller
    should not need to know which produced which:

    - *Conditional* limitations, specific to `start`: read off `start`'s
      own `metadata["coverage_limitation_kinds"]` (set at indexing time by
      `application.indexing` from adapter-reported
      `*.unresolved_field_access` observations -- see
      `adapters/sqlalchemy/adapter.py`'s module docstring), each a real,
      adapter-reported *category* of access recognized as plausibly
      relevant to this entity but not resolvable through any supported
      provenance form. Never a promise that every such access was found --
      see `ImpactResult`'s own "coverage is not confidence" note.
    - *Structural* limitations, true regardless of entity: the
      deduplicated `known_limitations` of every adapter configured for
      this system's indexing pipeline (`AdapterCapabilities`,
      `ports/adapters.py`), read off the latest complete `Snapshot`'s
      `diagnostics` (`application.indexing` writes them there, JSON-encoded,
      at index time). Deliberately pipeline-wide, not scoped to this
      specific query's entities -- a simplification; see `docs/IR.md`'s
      entry on this milestone for why over-warning was chosen over the
      complexity of per-query adapter attribution."""

    status: CoverageStatus
    limitations: tuple[Limitation, ...]


@dataclass(frozen=True, slots=True)
class ImpactGroup:
    """Every affected entity whose source file shares one directory --
    see `summarize_impact`'s own docstring for why a directory, not an
    invented layer name, is the grouping key. `entity_ids` is the
    complete membership (for `get_entity`/`get_relationships` drill-down,
    one call per id of interest, never all of them at once);
    `representative_entity_id`/`_display_name` names one member (the
    alphabetically first by `Entity.display_name` -- a deterministic
    tie-break, not a claim that this member matters more than the
    others)."""

    key: str
    """The directory this group's entities share, e.g. `"app/services"` --
    `"(unknown)"` if an entity has no recorded source file, `"(root)"` if
    the file has no parent directory."""
    entity_count: int
    path_count: int
    representative_entity_id: str
    representative_display_name: str
    entity_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SemanticImpactItem:
    """A semantically categorized entity reached during impact traversal,
    preserving relationship type, confidence, hop distance, and evidence."""

    entity: CompactEntityRef
    relationship_id: str | None
    relationship_type: str | None
    confidence: str
    hops_count: int
    evidence: tuple[CompactEvidenceRef, ...]


@dataclass(frozen=True, slots=True)
class ImpactSummary:
    """A projection of an `ImpactResult`, never a second source of truth
    for it -- see the module docstring's "Impact Presentation v0.1" entry.
    Every id named anywhere is drawn unchanged from the `ImpactResult`
    `summarize_impact` computed this from; nothing here is inferred, only
    grouped, counted, and referenced.

    `coverage` is carried through verbatim and placed ahead of categorized
    results: a caller's first question should be 'how much should I trust
    this,' not 'what's in it.'"""

    direction: str
    target: CompactEntityRef
    start_id: str
    revision: str | None
    resolved_from: str | None
    affected_entity_count: int
    path_count: int
    coverage: ImpactCoverage
    direct_callers: tuple[SemanticImpactItem, ...]
    direct_callees: tuple[SemanticImpactItem, ...]
    readers: tuple[SemanticImpactItem, ...]
    writers: tuple[SemanticImpactItem, ...]
    framework_boundaries: tuple[SemanticImpactItem, ...]
    indirect_dependencies: tuple[SemanticImpactItem, ...]
    groups: tuple[ImpactGroup, ...]
    """Ordered by `entity_count` descending -- the only ordering this
    module imposes. Not a relevance ranking: a group with one entity may
    matter far more than one with ten; deciding that is the caller's job,
    never this module's (module docstring: "let the agent decide what
    matters")."""


def _group_key(entity: Entity) -> str:
    file = entity.source.file if entity.source else None
    if not file:
        return "(unknown)"
    directory = posixpath.dirname(file)
    return directory or "(root)"


def summarize_impact(result: ImpactResult) -> ImpactSummary:
    """Project `result` into semantically organized impact categories
    (callers, callees, readers, writers, framework boundaries, indirect
    dependencies) alongside directory-level groups.

    Computed *from* an already-produced `ImpactResult`, never a new traversal.
    """
    target = CompactEntityRef(
        id=result.start.id,
        name=result.start.name,
        qualified_name=result.start.qualified_name,
        type=result.start.type.value,
    )

    direct_callers: list[SemanticImpactItem] = []
    direct_callees: list[SemanticImpactItem] = []
    readers: list[SemanticImpactItem] = []
    writers: list[SemanticImpactItem] = []
    framework_boundaries: list[SemanticImpactItem] = []
    indirect_dependencies: list[SemanticImpactItem] = []

    entities_by_key: dict[str, list[Entity]] = defaultdict(list)
    path_counts: dict[str, int] = defaultdict(int)

    for path in result.paths:
        key = _group_key(path.endpoint)
        entities_by_key[key].append(path.endpoint)
        path_counts[key] += 1

        endpoint_ref = CompactEntityRef(
            id=path.endpoint.id,
            name=path.endpoint.name,
            qualified_name=path.endpoint.qualified_name,
            type=path.endpoint.type.value,
        )

        if len(path.hops) == 1:
            hop = path.hops[0]
            rel = hop.relationship
            evidence = tuple(
                CompactEvidenceRef(
                    source=e.source.reference if e.source else None,
                    line=e.locator,
                    summary=e.summary,
                )
                for e in hop.evidence
            )
            item = SemanticImpactItem(
                entity=endpoint_ref,
                relationship_id=rel.id,
                relationship_type=rel.type.value,
                confidence=rel.confidence.value if rel.confidence is not None else "CERTAIN",
                hops_count=1,
                evidence=evidence,
            )
            if rel.type is RelationshipType.CALLS:
                if result.direction == "reverse":
                    direct_callers.append(item)
                else:
                    direct_callees.append(item)
            elif rel.type is RelationshipType.READS:
                readers.append(item)
            elif rel.type is RelationshipType.WRITES:
                writers.append(item)
            elif rel.type in (RelationshipType.EXPOSES, RelationshipType.IMPORTS):
                framework_boundaries.append(item)
            else:
                framework_boundaries.append(item)
        elif len(path.hops) > 1:
            first_hop = path.hops[0]
            first_rel = first_hop.relationship
            evidence = tuple(
                CompactEvidenceRef(
                    source=e.source.reference if e.source else None,
                    line=e.locator,
                    summary=e.summary,
                )
                for e in first_hop.evidence
            )
            item = SemanticImpactItem(
                entity=endpoint_ref,
                relationship_id=first_rel.id,
                relationship_type=first_rel.type.value,
                confidence=path.weakest_confidence.value,
                hops_count=len(path.hops),
                evidence=evidence,
            )
            indirect_dependencies.append(item)

    groups = []
    for key, entities in entities_by_key.items():
        ordered = sorted(entities, key=lambda e: e.display_name)
        representative = ordered[0]
        groups.append(
            ImpactGroup(
                key=key,
                entity_count=len(ordered),
                path_count=path_counts[key],
                representative_entity_id=representative.id,
                representative_display_name=representative.display_name,
                entity_ids=tuple(e.id for e in ordered),
            )
        )
    groups.sort(key=lambda g: g.entity_count, reverse=True)

    return ImpactSummary(
        direction=result.direction,
        target=target,
        start_id=result.start.id,
        revision=result.revision,
        resolved_from=result.resolved_from,
        affected_entity_count=len(result.affected_entities),
        path_count=len(result.paths),
        coverage=result.coverage,
        direct_callers=tuple(direct_callers),
        direct_callees=tuple(direct_callees),
        readers=tuple(readers),
        writers=tuple(writers),
        framework_boundaries=tuple(framework_boundaries),
        indirect_dependencies=tuple(indirect_dependencies),
        groups=tuple(groups),
    )


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


@dataclass(frozen=True, slots=True)
class LineageHop:
    """One `SUPERSEDES` edge in a lineage chain: the relationship exactly
    as stored, plus both entities it connects and whatever evidence backs
    it. `predecessor`/`successor` name the *conceptual* direction (the
    older entity, the newer one) regardless of which one `follow_lineage`
    was asked about -- unlike `ImpactHop`, which deliberately never
    reinterprets `source`/`target`, lineage only has one honest reading:
    `successor` is always `relationship.source_entity_id` (`SUPERSEDES`
    always points new -> old), so naming it plainly here costs nothing."""

    relationship: Relationship
    predecessor: Entity
    successor: Entity
    evidence: tuple[Evidence, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class LineageResult:
    """The full `SUPERSEDES` chain for one entity, in both directions.

    Deliberately not revision-scoped, unlike everything else in this
    module: lineage is not "what was true at a moment", it is the
    permanent record of which entities are the same evolving concept.
    `predecessors[0]` is what `start` most directly superseded (if
    anything); `successors[0]` is whatever most directly superseded
    `start` (if anything) -- each list continues transitively, oldest or
    newest last. v0.1 walks a linear chain (one predecessor, one successor
    per hop) -- see `follow_lineage`'s own docstring on why a fork is left
    unresolved rather than guessed at.
    """

    start: Entity
    predecessors: tuple[LineageHop, ...]
    successors: tuple[LineageHop, ...]


def follow_lineage(uow: UnitOfWork, *, system_id: SystemID, entity_id: str) -> LineageResult:
    """Walk `SUPERSEDES` edges in both directions from `entity_id`, across
    all of history -- "what happened to the thing I was looking at",
    answered with the actual evidence-backed chain rather than "that
    entity doesn't exist anymore" (a superseded entity is never deleted,
    only marked, per §10).

    v0.1 assumes a linear chain: if more than one entity supersedes the
    same predecessor (or vice versa), the walk in that direction stops
    rather than picking one arbitrarily -- the same "do not guess" rule
    `identity.resolve` itself follows for a genuine tie, applied here to
    lineage traversal instead of a single resolution decision.
    """
    all_entities = list(uow.graph.find_entities(system_id, limit=_ALL_ENTITIES_LIMIT))
    by_id = {e.id: e for e in all_entities}
    start = by_id.get(entity_id)
    if start is None:
        start = uow.graph.get_entity(entity_id)
        if start is None:
            raise KeyError(f"{entity_id!r} is not a known entity in system {system_id!r}")
        by_id[start.id] = start

    superseders: dict[str, list[Relationship]] = defaultdict(list)  # target_id -> edges into it
    superseded: dict[str, list[Relationship]] = defaultdict(list)  # source_id -> edges out of it
    for entity in all_entities:
        for rel in uow.graph.get_relationships(entity.id, direction="out"):
            if rel.type is not RelationshipType.SUPERSEDES:
                continue
            superseders[rel.target_entity_id].append(rel)
            superseded[rel.source_entity_id].append(rel)

    def walk(
        current_id: str, edges_by_id: dict[str, list[Relationship]], *, forward: bool
    ) -> list[LineageHop]:
        hops: list[LineageHop] = []
        seen = {current_id}
        while True:
            candidates = edges_by_id.get(current_id, [])
            unseen = [
                rel
                for rel in candidates
                if (rel.source_entity_id if forward else rel.target_entity_id) not in seen
            ]
            if len(unseen) != 1:
                break  # nothing further, or a fork -- do not guess which branch
            rel = unseen[0]
            other_id = rel.source_entity_id if forward else rel.target_entity_id
            other = by_id.get(other_id)
            if other is None:
                break
            predecessor = by_id[current_id] if forward else other
            successor = other if forward else by_id[current_id]
            hops.append(LineageHop(relationship=rel, predecessor=predecessor, successor=successor))
            seen.add(other_id)
            current_id = other_id
        return hops

    successor_hops = walk(start.id, superseders, forward=True)
    predecessor_hops = walk(start.id, superseded, forward=False)

    all_evidence_ids = {
        eid for hop in (*successor_hops, *predecessor_hops) for eid in hop.relationship.evidence_ids
    }
    evidence_by_id = (
        {ev.id: ev for ev in uow.evidence.get_many(list(all_evidence_ids))}
        if all_evidence_ids
        else {}
    )

    def with_evidence(hop: LineageHop) -> LineageHop:
        if not hop.relationship.evidence_ids:
            return hop
        return LineageHop(
            relationship=hop.relationship,
            predecessor=hop.predecessor,
            successor=hop.successor,
            evidence=tuple(
                evidence_by_id[eid]
                for eid in hop.relationship.evidence_ids
                if eid in evidence_by_id
            ),
        )

    return LineageResult(
        start=start,
        predecessors=tuple(with_evidence(h) for h in predecessor_hops),
        successors=tuple(with_evidence(h) for h in successor_hops),
    )


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
        coverage=_coverage_for(uow, system_id=system_id, start=start),
        resolved_from=resolved_from,
    )


_LIMITATION_PREFIX = "limitation: "

#: Detail text for each *conditional* `LimitationKind` -- the ones an
#: entity's own metadata names only by kind (`application.indexing`
#: stores a set of kind strings, not full `Limitation` objects, since the
#: detail text is the same every time a given kind is triggered). The
#: *structural* kinds (today, `RAW_SQL`) need no entry here: those come
#: fully formed off the snapshot's `diagnostics`, written by whichever
#: adapter declared them.
_CONDITIONAL_LIMITATION_TEXT: dict[LimitationKind, tuple[LimitationScope, str]] = {
    LimitationKind.UNTYPED_PARAMETER: (
        LimitationScope.ORM_ATTRIBUTE_ACCESS,
        "This entity is accessed through at least one function parameter "
        "whose type could not be determined -- some reads/writes may be "
        "missing.",
    ),
    LimitationKind.RETURN_VALUE_PROVENANCE: (
        LimitationScope.ORM_ATTRIBUTE_ACCESS,
        "This entity is accessed through at least one local variable "
        "assigned from a function or method's return value (including via "
        "self/cls, or chained through another such variable), whose return "
        "type could not be resolved -- some reads/writes may be missing.",
    ),
    LimitationKind.DYNAMIC_ATTRIBUTE_ACCESS: (
        LimitationScope.ORM_ATTRIBUTE_ACCESS,
        "This entity is accessed through at least one getattr()/setattr() "
        "call naming it by a literal string -- dynamic attribute access is "
        "not resolved into an edge, so some reads/writes may be missing.",
    ),
}


def get_coverage_for(uow: UnitOfWork, *, system_id: SystemID, start: Entity) -> ImpactCoverage:
    """Conditional limitations read `start`'s own indexing-time metadata
    (per-entity, precise); structural limitations read the latest complete
    snapshot's `diagnostics` (pipeline-wide -- see `ImpactCoverage`'s own
    docstring). Combined into one flat tuple; a caller should not need to
    know which source produced which."""
    conditional_kinds = start.metadata.get("coverage_limitation_kinds", [])
    conditional: list[Limitation] = []
    for raw_kind in conditional_kinds if isinstance(conditional_kinds, list) else ():
        if not isinstance(raw_kind, str):
            continue
        try:
            kind = LimitationKind(raw_kind)
        except ValueError:
            continue
        scope, detail = _CONDITIONAL_LIMITATION_TEXT[kind]
        conditional.append(Limitation(kind=kind, scope=scope, detail=detail))
    snapshot = uow.snapshots.latest_complete(system_id)
    structural = tuple(
        Limitation.model_validate_json(diagnostic.removeprefix(_LIMITATION_PREFIX))
        for diagnostic in (snapshot.diagnostics if snapshot else ())
        if diagnostic.startswith(_LIMITATION_PREFIX)
    )
    limitations = (*conditional, *structural)
    status = CoverageStatus.PARTIAL if limitations else CoverageStatus.COMPLETE
    return ImpactCoverage(status=status, limitations=limitations)


_coverage_for = get_coverage_for


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

    def follow_lineage(self, entity_id: str) -> LineageResult:
        return follow_lineage(self.uow, system_id=self.system_id, entity_id=entity_id)
