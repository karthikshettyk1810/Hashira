"""Identity resolution: is this construct one we already know? (spec §10).

The spec's prose gives a list of signals to weigh — "language-level stable
symbol identifiers, package/module identity, repository and namespace, Git
rename history, framework metadata, database migration lineage, explicit user
declarations" — without an algorithm. This module is that algorithm: an
ordered, testable policy for turning a freshly observed candidate entity and a
pool of entities already in the graph into one of four decisions.

It is pure decision logic. It touches no storage and no adapter; a caller (the
future ingestion pipeline) owns persisting whatever it decides. The invariant
this module exists to enforce, from §10: "identity changes must be represented
as events or lineage relationships rather than silently overwriting history."
There is no path here that folds two entities together without leaving a
trace of why — a wrong merge must be explainable and reversible.

## The ladder

Signals are grouped into three tiers by how much a single hit is worth
trusting on its own:

* **Strong** — ``SYMBOL_ID``, ``USER_DECLARED``, ``DECLARATION_ANCHOR``. Any
  one, alone, at or above ``match_floor`` confidence, is enough to merge
  outright. ``DECLARATION_ANCHOR`` (file path + qualified name + kind, all at
  once) is not independent of ``QUALIFIED_NAME`` — it is derived from it —
  but is materially narrower: it only matches the exact declaration site a
  prior entity came from, not "a name I recognize somewhere." That is what
  makes an unchanged file re-index resolve as ``MATCHED`` instead of minting
  a new entity generation on every run, without weakening the guard against
  qualified names colliding by coincidence elsewhere in the graph.
* **Corroborating** — ``GIT_RENAME``, ``MIGRATION_LINEAGE``, ``DECLARATION_LINEAGE``,
  ``QUALIFIED_NAME``. One of these alone justifies recording lineage (a new
  entity, linked to the old one by ``SUPERSEDES``) but not an outright merge —
  a qualified name can be reused across an unrelated file, so identity must
  not hinge on it alone. Two independent signals from this tier (or one of
  these plus a low-confidence strong signal) corroborate each other and *do*
  justify a merge.
* **Weak** — ``STRUCTURAL_SIMILARITY``. Never enough by itself, and never
  promotes a corroborating signal either; it exists so an adapter can record a
  hunch without that hunch being mistaken for evidence.

## Outcomes

* ``NEW`` — no existing entity shares a signal. Mint the candidate as-is.
* ``SUPERSEDES`` — one corroborating signal points at exactly one existing
  entity. Mint the candidate as a new entity, close the old one out with a
  ``SUPERSEDES`` lineage edge, never silently overwrite it.
* ``MATCHED`` — a strong signal, or two corroborating ones, point at exactly
  one existing entity. Fold the candidate into it; the existing entity's id
  survives.
* ``AMBIGUOUS`` — the same top-tier signal points at more than one existing
  entity. Refuse to guess; surface a low-confidence hypothesis instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from ..core.base import utc_now
from ..core.entities import Entity, IdentityClaim
from ..core.enums import (
    Confidence,
    EntityStatus,
    IdentityClaimKind,
    KnowledgeClass,
    Origin,
    RelationshipType,
)
from ..core.evidence import Inference
from ..core.ids import EntityID
from ..core.relationships import Relationship

__all__ = [
    "IdentityResolution",
    "ResolutionDecision",
    "ResolutionOutcome",
    "apply",
    "merge_into",
    "resolve",
]


class ResolutionOutcome(StrEnum):
    """The four decisions ``resolve`` can reach. See module docstring."""

    NEW = "NEW"
    SUPERSEDES = "SUPERSEDES"
    MATCHED = "MATCHED"
    AMBIGUOUS = "AMBIGUOUS"


_STRONG: frozenset[IdentityClaimKind] = frozenset(
    {
        IdentityClaimKind.SYMBOL_ID,
        IdentityClaimKind.USER_DECLARED,
        IdentityClaimKind.DECLARATION_ANCHOR,
    }
)
_CORROBORATING: frozenset[IdentityClaimKind] = frozenset(
    {
        IdentityClaimKind.GIT_RENAME,
        IdentityClaimKind.MIGRATION_LINEAGE,
        IdentityClaimKind.DECLARATION_LINEAGE,
        IdentityClaimKind.QUALIFIED_NAME,
    }
)
_MEANINGFUL: frozenset[IdentityClaimKind] = _STRONG | _CORROBORATING
#: STRUCTURAL_SIMILARITY is deliberately absent from every tier set above: it
#: contributes to nothing except its own presence being visible in `rationale`.


@dataclass(frozen=True, slots=True)
class ResolutionDecision:
    """What the ladder concluded, and why — the whole point being that the
    "why" travels with the decision rather than living only in a log line."""

    outcome: ResolutionOutcome
    matched_entity_id: EntityID | None
    confidence: Confidence
    winning_kind: IdentityClaimKind | None
    rationale: str
    tied_entity_ids: tuple[EntityID, ...] = field(default_factory=tuple)
    """Populated only for AMBIGUOUS: the entities tied at the top tier."""


@dataclass(frozen=True, slots=True)
class IdentityResolution:
    """The records ``apply`` produces. Fields are ``None`` when not relevant
    to the outcome; a caller persists whichever are set."""

    decision: ResolutionDecision
    entity: Entity
    superseded: Entity | None = None
    relationship: Relationship | None = None
    inference: Inference | None = None


@dataclass(frozen=True, slots=True)
class _Hits:
    entity: Entity
    kinds: frozenset[IdentityClaimKind]
    best_kind: IdentityClaimKind
    best_confidence: Confidence


def _hits_for(candidate_claims: Sequence[IdentityClaim], entity: Entity) -> _Hits | None:
    """Which claim kinds the candidate and ``entity`` agree on, and how strongly."""
    matches: list[tuple[IdentityClaimKind, Confidence]] = [
        (cand.kind, min(cand.confidence, existing.confidence))
        for cand in candidate_claims
        for existing in entity.identity_claims
        if existing.kind == cand.kind and existing.value == cand.value
    ]
    if not matches:
        return None
    kinds = frozenset(kind for kind, _ in matches)

    def _rank(item: tuple[IdentityClaimKind, Confidence]) -> tuple[int, int]:
        kind, confidence = item
        tier = 2 if kind in _STRONG else 1 if kind in _CORROBORATING else 0
        return (tier, confidence.rank)

    best_kind, best_confidence = max(matches, key=_rank)
    return _Hits(entity=entity, kinds=kinds, best_kind=best_kind, best_confidence=best_confidence)


def _tier(hits: _Hits, match_floor: Confidence) -> int:
    """0 = too weak to act on, 1 = SUPERSEDES-eligible, 2 = MATCHED-eligible."""
    if hits.best_kind in _STRONG and hits.best_confidence >= match_floor:
        return 2
    meaningful = hits.kinds & _MEANINGFUL
    if len(meaningful) >= 2:
        return 2
    if len(meaningful) == 1:
        return 1
    return 0


def resolve(
    candidate: Entity,
    existing: Sequence[Entity],
    *,
    match_floor: Confidence = Confidence.LIKELY,
) -> ResolutionDecision:
    """Decide what ``candidate`` is, relative to what Hashira already knows.

    ``existing`` is the pool of entities to consider — typically every entity
    of ``candidate.type`` in the same system. Identity never crosses entity
    types: a function cannot resolve to a table.
    """
    scored: list[tuple[int, _Hits]] = []
    for entity in existing:
        if entity.type != candidate.type or entity.id == candidate.id:
            continue
        hits = _hits_for(candidate.identity_claims, entity)
        if hits is None:
            continue
        tier = _tier(hits, match_floor)
        if tier > 0:
            scored.append((tier, hits))

    if not scored:
        return ResolutionDecision(
            outcome=ResolutionOutcome.NEW,
            matched_entity_id=None,
            confidence=Confidence.CERTAIN,
            winning_kind=None,
            rationale="no existing entity shares an identity signal",
        )

    top_tier = max(tier for tier, _ in scored)
    top = [hits for tier, hits in scored if tier == top_tier]

    if len(top) > 1:
        tied = tuple(sorted(hits.entity.id for hits in top))
        return ResolutionDecision(
            outcome=ResolutionOutcome.AMBIGUOUS,
            matched_entity_id=None,
            confidence=Confidence.SPECULATIVE,
            winning_kind=None,
            rationale=(
                f"{len(top)} existing entities tie at the same signal strength; "
                "resolution refuses to guess between them"
            ),
            tied_entity_ids=tied,
        )

    winner = top[0]
    kinds = ", ".join(sorted(kind.value for kind in winner.kinds))
    outcome = ResolutionOutcome.MATCHED if top_tier == 2 else ResolutionOutcome.SUPERSEDES
    return ResolutionDecision(
        outcome=outcome,
        matched_entity_id=winner.entity.id,
        confidence=winner.best_confidence,
        winning_kind=winner.best_kind,
        rationale=f"matched on {kinds}",
    )


def merge_into(existing: Entity, candidate: Entity, *, at: datetime | None = None) -> Entity:
    """Fold a newly observed construct into the entity it resolved to.

    The existing entity's opaque id is authoritative and survives (§10).
    Location and technology move to whatever was most recently observed;
    identity claims accumulate rather than replace, so the full trail behind a
    merge decision stays inspectable later.
    """
    claims = list(existing.identity_claims)
    seen = {(claim.kind, claim.value) for claim in claims}
    for claim in candidate.identity_claims:
        key = (claim.kind, claim.value)
        if key not in seen:
            claims.append(claim)
            seen.add(key)

    return existing.model_copy(
        update={
            "qualified_name": candidate.qualified_name or existing.qualified_name,
            "source": candidate.source or existing.source,
            "technology": candidate.technology or existing.technology,
            "last_seen_revision": candidate.last_seen_revision or existing.last_seen_revision,
            "identity_claims": claims,
            "status": EntityStatus.ACTIVE,
            "updated_at": at or utc_now(),
        }
    )


def apply(
    candidate: Entity,
    decision: ResolutionDecision,
    existing: Sequence[Entity],
    *,
    revision: str | None = None,
    at: datetime | None = None,
) -> IdentityResolution:
    """Turn a ``ResolutionDecision`` into the records a caller should persist.

    ``existing`` must be the same pool (or a superset) passed to ``resolve``,
    so the entities named in ``decision`` can be looked up.
    """
    when = at or utc_now()

    if decision.outcome is ResolutionOutcome.NEW:
        return IdentityResolution(decision=decision, entity=candidate)

    if decision.outcome is ResolutionOutcome.AMBIGUOUS:
        hypothesis = Inference(
            system_id=candidate.system_id,
            knowledge_class=KnowledgeClass.HYPOTHESIS,
            statement=(
                f"{candidate.display_name} may be the same entity as one of "
                f"{len(decision.tied_entity_ids)} existing candidates, but identity "
                f"resolution could not choose ({decision.rationale})"
            ),
            subject_entity_ids=[candidate.id, *decision.tied_entity_ids],
            origin=Origin.DERIVED,
            confidence=Confidence.SPECULATIVE,
            created_at=when,
            updated_at=when,
        )
        return IdentityResolution(decision=decision, entity=candidate, inference=hypothesis)

    by_id = {entity.id: entity for entity in existing}
    assert decision.matched_entity_id is not None
    matched = by_id[decision.matched_entity_id]

    if decision.outcome is ResolutionOutcome.MATCHED:
        merged = merge_into(matched, candidate, at=when)
        return IdentityResolution(decision=decision, entity=merged)

    # SUPERSEDES: mint the candidate as a genuinely new entity, close the old
    # one out, and record why — never overwrite the old entity in place (§10).
    superseded = matched.model_copy(update={"status": EntityStatus.SUPERSEDED, "updated_at": when})
    lineage = Relationship(
        system_id=candidate.system_id,
        source_entity_id=candidate.id,
        target_entity_id=matched.id,
        type=RelationshipType.SUPERSEDES,
        knowledge_class=KnowledgeClass.DERIVATION,
        origin=Origin.DERIVED,
        confidence=decision.confidence,
        valid_from_revision=revision,
        valid_from=when,
        metadata={"rationale": decision.rationale},
    )
    return IdentityResolution(
        decision=decision, entity=candidate, superseded=superseded, relationship=lineage
    )
