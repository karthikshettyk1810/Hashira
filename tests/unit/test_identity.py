"""The identity resolution ladder (spec §10): matching, lineage, and refusal to guess."""

from __future__ import annotations

from hashira.core import (
    Confidence,
    Entity,
    EntityStatus,
    EntityType,
    IdentityClaim,
    IdentityClaimKind,
    KnowledgeClass,
    Origin,
    RelationshipType,
    System,
)
from hashira.identity import ResolutionOutcome, apply, merge_into, resolve
from hashira.identity.resolver import ResolutionDecision


def _claim(
    kind: IdentityClaimKind, value: str, confidence: Confidence = Confidence.CERTAIN
) -> IdentityClaim:
    return IdentityClaim(kind=kind, value=value, origin=Origin.PARSER, confidence=confidence)


def _entity(
    system: System, *, name: str = "process", type: EntityType = EntityType.SYMBOL, claims=()
) -> Entity:
    return Entity(system_id=system.id, type=type, name=name, identity_claims=list(claims))


# --- resolve(): NEW ----------------------------------------------------------


def test_empty_pool_is_new(system: System) -> None:
    candidate = _entity(system, claims=[_claim(IdentityClaimKind.SYMBOL_ID, "sym#1")])
    decision = resolve(candidate, existing=[])
    assert decision.outcome is ResolutionOutcome.NEW
    assert decision.matched_entity_id is None


def test_no_shared_signal_is_new(system: System) -> None:
    candidate = _entity(system, claims=[_claim(IdentityClaimKind.SYMBOL_ID, "sym#new")])
    existing = _entity(system, claims=[_claim(IdentityClaimKind.SYMBOL_ID, "sym#old")])
    decision = resolve(candidate, existing=[existing])
    assert decision.outcome is ResolutionOutcome.NEW


def test_different_entity_type_is_excluded(system: System) -> None:
    """Identity never crosses types: a function cannot resolve to a table."""
    shared_qn = _claim(IdentityClaimKind.QUALIFIED_NAME, "payments.Payment")
    candidate = _entity(system, type=EntityType.SYMBOL, claims=[shared_qn])
    existing = _entity(system, type=EntityType.DATA_ENTITY, claims=[shared_qn])
    decision = resolve(candidate, existing=[existing])
    assert decision.outcome is ResolutionOutcome.NEW


def test_weak_signal_alone_is_new(system: System) -> None:
    """Structural similarity alone never even earns lineage — too easy to be coincidence."""
    candidate = _entity(
        system, claims=[_claim(IdentityClaimKind.STRUCTURAL_SIMILARITY, "fn(payment_id)")]
    )
    existing = _entity(
        system, claims=[_claim(IdentityClaimKind.STRUCTURAL_SIMILARITY, "fn(payment_id)")]
    )
    decision = resolve(candidate, existing=[existing])
    assert decision.outcome is ResolutionOutcome.NEW


# --- resolve(): SUPERSEDES ----------------------------------------------------


def test_qualified_name_alone_is_supersedes_not_matched(system: System) -> None:
    """A name can be reused across an unrelated file; one signal isn't enough to merge."""
    qn = _claim(IdentityClaimKind.QUALIFIED_NAME, "payments.services.PaymentService.process")
    candidate = _entity(system, claims=[qn])
    existing = _entity(system, claims=[qn])
    decision = resolve(candidate, existing=[existing])
    assert decision.outcome is ResolutionOutcome.SUPERSEDES
    assert decision.matched_entity_id == existing.id
    assert decision.winning_kind is IdentityClaimKind.QUALIFIED_NAME


def test_git_rename_alone_is_supersedes(system: System) -> None:
    rename = _claim(IdentityClaimKind.GIT_RENAME, "payments/services.py -> payments/core.py")
    candidate = _entity(system, claims=[rename])
    existing = _entity(system, claims=[rename])
    decision = resolve(candidate, existing=[existing])
    assert decision.outcome is ResolutionOutcome.SUPERSEDES


def test_weak_signal_does_not_promote_a_corroborating_one(system: System) -> None:
    """Structural similarity riding along with one real signal still doesn't merge."""
    qn = _claim(IdentityClaimKind.QUALIFIED_NAME, "payments.services.PaymentService.process")
    weak = _claim(IdentityClaimKind.STRUCTURAL_SIMILARITY, "fn(payment_id)")
    candidate = _entity(system, claims=[qn, weak])
    existing = _entity(system, claims=[qn, weak])
    decision = resolve(candidate, existing=[existing])
    assert decision.outcome is ResolutionOutcome.SUPERSEDES


def test_low_confidence_strong_signal_alone_is_supersedes_not_matched(system: System) -> None:
    """A speculative symbol-id match still counts as one real signal — lineage, not a merge."""
    candidate = _entity(
        system, claims=[_claim(IdentityClaimKind.SYMBOL_ID, "sym#1", Confidence.SPECULATIVE)]
    )
    existing = _entity(
        system, claims=[_claim(IdentityClaimKind.SYMBOL_ID, "sym#1", Confidence.SPECULATIVE)]
    )
    decision = resolve(candidate, existing=[existing], match_floor=Confidence.LIKELY)
    assert decision.outcome is ResolutionOutcome.SUPERSEDES
    assert decision.confidence is Confidence.SPECULATIVE


# --- resolve(): MATCHED -------------------------------------------------------


def test_symbol_id_match_is_matched(system: System) -> None:
    sym = _claim(IdentityClaimKind.SYMBOL_ID, "sym#1")
    candidate = _entity(system, claims=[sym])
    existing = _entity(system, claims=[sym])
    decision = resolve(candidate, existing=[existing])
    assert decision.outcome is ResolutionOutcome.MATCHED
    assert decision.matched_entity_id == existing.id
    assert decision.confidence is Confidence.CERTAIN


def test_user_declared_alone_is_matched(system: System) -> None:
    """A human's explicit mapping is authoritative on its own."""
    declared = _claim(IdentityClaimKind.USER_DECLARED, "this-is-the-same-thing")
    candidate = _entity(system, claims=[declared])
    existing = _entity(system, claims=[declared])
    decision = resolve(candidate, existing=[existing])
    assert decision.outcome is ResolutionOutcome.MATCHED


def test_two_corroborating_signals_are_matched() -> None:
    """Qualified name plus git rename, together, are enough to merge outright."""
    system = System(name="s", slug="s")
    qn = _claim(IdentityClaimKind.QUALIFIED_NAME, "payments.services.PaymentService.process")
    rename = _claim(IdentityClaimKind.GIT_RENAME, "payments/services.py -> payments/core.py")
    candidate = _entity(system, claims=[qn, rename])
    existing = _entity(system, claims=[qn, rename])
    decision = resolve(candidate, existing=[existing])
    assert decision.outcome is ResolutionOutcome.MATCHED
    assert "GIT_RENAME" in decision.rationale
    assert "QUALIFIED_NAME" in decision.rationale


def test_candidate_never_matches_itself(system: System) -> None:
    """If the pool already contains the candidate (by id), it must be ignored."""
    sym = _claim(IdentityClaimKind.SYMBOL_ID, "sym#1")
    candidate = _entity(system, claims=[sym])
    decision = resolve(candidate, existing=[candidate])
    assert decision.outcome is ResolutionOutcome.NEW


# --- resolve(): AMBIGUOUS ------------------------------------------------------


def test_tied_matches_are_ambiguous(system: System) -> None:
    sym = _claim(IdentityClaimKind.SYMBOL_ID, "sym#dup")
    candidate = _entity(system, claims=[sym])
    a = _entity(system, name="a", claims=[sym])
    b = _entity(system, name="b", claims=[sym])
    decision = resolve(candidate, existing=[a, b])
    assert decision.outcome is ResolutionOutcome.AMBIGUOUS
    assert decision.matched_entity_id is None
    assert set(decision.tied_entity_ids) == {a.id, b.id}
    assert decision.confidence is Confidence.SPECULATIVE


def test_a_stronger_tier_breaks_a_tie_at_a_weaker_one(system: System) -> None:
    """Two entities share a weak-tier signal with the candidate, but only one also
    shares a strong one — that one should win outright, not trigger AMBIGUOUS."""
    qn = _claim(IdentityClaimKind.QUALIFIED_NAME, "payments.services.PaymentService.process")
    sym = _claim(IdentityClaimKind.SYMBOL_ID, "sym#1")
    candidate = _entity(system, claims=[qn, sym])
    strong_match = _entity(system, name="a", claims=[sym])
    weak_match = _entity(system, name="b", claims=[qn])
    decision = resolve(candidate, existing=[weak_match, strong_match])
    assert decision.outcome is ResolutionOutcome.MATCHED
    assert decision.matched_entity_id == strong_match.id


# --- apply(): NEW --------------------------------------------------------------


def test_apply_new_returns_the_candidate_untouched(system: System) -> None:
    candidate = _entity(system, claims=[_claim(IdentityClaimKind.SYMBOL_ID, "sym#1")])
    decision = resolve(candidate, existing=[])
    result = apply(candidate, decision, existing=[])
    assert result.entity is candidate
    assert result.superseded is None
    assert result.relationship is None
    assert result.inference is None


# --- apply(): MATCHED ------------------------------------------------------


def test_apply_matched_merges_into_the_existing_entity(system: System) -> None:
    sym = _claim(IdentityClaimKind.SYMBOL_ID, "sym#1")
    existing = Entity(
        system_id=system.id,
        type=EntityType.SYMBOL,
        name="process",
        qualified_name="old.path.process",
        identity_claims=[sym],
    )
    candidate = Entity(
        system_id=system.id,
        type=EntityType.SYMBOL,
        name="process",
        qualified_name="payments.services.PaymentService.process",
        last_seen_revision="def456",
        identity_claims=[sym],
    )
    decision = resolve(candidate, existing=[existing])
    result = apply(candidate, decision, existing=[existing])

    assert result.entity.id == existing.id, "the existing entity's id survives a merge"
    assert result.entity.id != candidate.id
    assert result.entity.qualified_name == "payments.services.PaymentService.process"
    assert result.entity.last_seen_revision == "def456"
    assert result.entity.status is EntityStatus.ACTIVE
    assert result.superseded is None
    assert result.relationship is None


def test_merge_into_accumulates_claims_without_duplicating(system: System) -> None:
    old_claim = _claim(IdentityClaimKind.QUALIFIED_NAME, "old.path.process")
    shared_claim = _claim(IdentityClaimKind.SYMBOL_ID, "sym#1")
    existing = _entity(system, claims=[old_claim, shared_claim])
    candidate = _entity(system, claims=[shared_claim])

    merged = merge_into(existing, candidate)
    assert len(merged.identity_claims) == 2
    assert old_claim in merged.identity_claims


# --- apply(): SUPERSEDES -----------------------------------------------------


def test_apply_supersedes_mints_a_new_entity_and_closes_the_old_one(system: System) -> None:
    qn = _claim(IdentityClaimKind.QUALIFIED_NAME, "payments.services.PaymentService.process")
    existing = _entity(system, claims=[qn])
    candidate = _entity(system, claims=[qn])

    decision = resolve(candidate, existing=[existing])
    result = apply(candidate, decision, existing=[existing], revision="def456")

    assert result.entity.id == candidate.id, "a superseding entity is genuinely new"
    assert result.superseded is not None
    assert result.superseded.id == existing.id
    assert result.superseded.status is EntityStatus.SUPERSEDED
    # The model's own invariant: a SUPERSEDED entity must keep the claims that
    # justified it. Constructing `result.superseded` must not have violated it.
    assert result.superseded.identity_claims == existing.identity_claims

    assert result.relationship is not None
    assert result.relationship.source_entity_id == candidate.id
    assert result.relationship.target_entity_id == existing.id
    assert result.relationship.type is RelationshipType.SUPERSEDES
    assert result.relationship.knowledge_class is KnowledgeClass.DERIVATION
    assert result.relationship.valid_from_revision == "def456"
    assert result.inference is None


# --- apply(): AMBIGUOUS -------------------------------------------------------


def test_apply_ambiguous_produces_a_speculative_hypothesis_not_a_merge(system: System) -> None:
    sym = _claim(IdentityClaimKind.SYMBOL_ID, "sym#dup")
    candidate = _entity(system, claims=[sym])
    a = _entity(system, name="a", claims=[sym])
    b = _entity(system, name="b", claims=[sym])

    decision = resolve(candidate, existing=[a, b])
    result = apply(candidate, decision, existing=[a, b])

    assert result.entity.id == candidate.id
    assert result.superseded is None
    assert result.relationship is None
    assert result.inference is not None
    assert result.inference.knowledge_class is KnowledgeClass.HYPOTHESIS
    assert result.inference.confidence is Confidence.SPECULATIVE
    assert not result.inference.is_actionable
    assert set(result.inference.subject_entity_ids) == {candidate.id, a.id, b.id}


def test_decision_is_immutable() -> None:
    decision = ResolutionDecision(
        outcome=ResolutionOutcome.NEW,
        matched_entity_id=None,
        confidence=Confidence.CERTAIN,
        winning_kind=None,
        rationale="no existing entity shares an identity signal",
    )
    try:
        decision.outcome = ResolutionOutcome.MATCHED  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("ResolutionDecision must be frozen")
