"""Attaching declaration-lineage evidence: precise pairing by qualified
name, explicit refusal to guess -- the sibling of `test_git_evidence.py`
for a rename Git's own file-level heuristic never sees."""

from __future__ import annotations

from hashira.core import Confidence, Entity, EntityType, IdentityClaimKind, System
from hashira.identity import DeclarationRename, attach_declaration_lineage_evidence


def _column(system: System, qualified_name: str) -> Entity:
    return Entity(
        system_id=system.id,
        type=EntityType.SYMBOL,
        name=qualified_name.rsplit(".", 1)[-1],
        qualified_name=qualified_name,
    )


def _claims_of(entity: Entity, kind: IdentityClaimKind) -> list[str]:
    return [c.value for c in entity.identity_claims if c.kind is kind]


def test_unambiguous_pair_gets_a_matching_claim_on_both_sides(system: System) -> None:
    old_column = _column(system, "payments.status")
    new_column = _column(system, "payments.state")
    rename = DeclarationRename(
        container_qualified_name="payments",
        old_qualified_name="payments.status",
        new_qualified_name="payments.state",
        confidence=Confidence.CERTAIN,
    )

    candidates, existing = attach_declaration_lineage_evidence([new_column], [old_column], [rename])

    new_claims = _claims_of(candidates[0], IdentityClaimKind.DECLARATION_LINEAGE)
    old_claims = _claims_of(existing[0], IdentityClaimKind.DECLARATION_LINEAGE)
    assert new_claims == old_claims
    assert len(new_claims) == 1


def test_confidence_is_carried_through_from_the_proposed_rename(system: System) -> None:
    old_column = _column(system, "payments.status")
    new_column = _column(system, "payments.state")
    rename = DeclarationRename(
        container_qualified_name="payments",
        old_qualified_name="payments.status",
        new_qualified_name="payments.state",
        confidence=Confidence.LIKELY,
    )

    candidates, _existing = attach_declaration_lineage_evidence(
        [new_column], [old_column], [rename]
    )
    claim = next(
        c for c in candidates[0].identity_claims if c.kind is IdentityClaimKind.DECLARATION_LINEAGE
    )
    assert claim.confidence is Confidence.LIKELY


def test_a_second_candidate_with_the_new_name_gets_no_claim(system: System) -> None:
    """Two entities coincidentally sharing the proposed new qualified name
    -- pairing by name alone would be a guess, so neither is linked."""
    old_column = _column(system, "payments.status")
    new_column = _column(system, "payments.state")
    unrelated_duplicate = _column(system, "payments.state")  # same qualified_name, different entity
    rename = DeclarationRename(
        container_qualified_name="payments",
        old_qualified_name="payments.status",
        new_qualified_name="payments.state",
        confidence=Confidence.CERTAIN,
    )

    candidates, existing = attach_declaration_lineage_evidence(
        [new_column, unrelated_duplicate], [old_column], [rename]
    )
    by_id = {e.id: e for e in [*candidates, *existing]}
    assert _claims_of(by_id[new_column.id], IdentityClaimKind.DECLARATION_LINEAGE) == []
    assert _claims_of(by_id[unrelated_duplicate.id], IdentityClaimKind.DECLARATION_LINEAGE) == []
    assert _claims_of(by_id[old_column.id], IdentityClaimKind.DECLARATION_LINEAGE) == []


def test_the_old_name_absent_from_existing_gets_no_claim(system: System) -> None:
    """The adapter proposed a rename, but the pool being resolved this run
    does not actually contain an entity with the old qualified name (it may
    have already been superseded by something else, or never existed) --
    the proposal is re-checked against reality, not blindly trusted."""
    new_column = _column(system, "payments.state")
    rename = DeclarationRename(
        container_qualified_name="payments",
        old_qualified_name="payments.status",
        new_qualified_name="payments.state",
        confidence=Confidence.CERTAIN,
    )

    candidates, existing = attach_declaration_lineage_evidence([new_column], [], [rename])
    assert _claims_of(candidates[0], IdentityClaimKind.DECLARATION_LINEAGE) == []
    assert existing == []


def test_the_new_name_absent_from_candidates_gets_no_claim(system: System) -> None:
    old_column = _column(system, "payments.status")
    rename = DeclarationRename(
        container_qualified_name="payments",
        old_qualified_name="payments.status",
        new_qualified_name="payments.state",
        confidence=Confidence.CERTAIN,
    )

    candidates, existing = attach_declaration_lineage_evidence([], [old_column], [rename])
    assert candidates == []
    assert _claims_of(existing[0], IdentityClaimKind.DECLARATION_LINEAGE) == []


def test_no_renames_returns_inputs_unchanged(system: System) -> None:
    column = _column(system, "payments.status")
    candidates, existing = attach_declaration_lineage_evidence([column], [], [])
    assert candidates == [column]
    assert existing == []


def test_original_lists_are_not_mutated(system: System) -> None:
    old_column = _column(system, "payments.status")
    new_column = _column(system, "payments.state")
    rename = DeclarationRename(
        container_qualified_name="payments",
        old_qualified_name="payments.status",
        new_qualified_name="payments.state",
        confidence=Confidence.CERTAIN,
    )

    candidates_in = [new_column]
    existing_in = [old_column]
    attach_declaration_lineage_evidence(candidates_in, existing_in, [rename])
    assert candidates_in[0].identity_claims == []
    assert existing_in[0].identity_claims == []


def test_claim_value_is_specific_to_the_container_and_both_names(system: System) -> None:
    """Two unrelated renames in different tables must not collide on claim
    value even if the old/new field names happen to repeat."""
    accounts_old = _column(system, "accounts.status")
    accounts_new = _column(system, "accounts.state")
    payments_old = _column(system, "payments.status")
    payments_new = _column(system, "payments.state")
    renames = [
        DeclarationRename("accounts", "accounts.status", "accounts.state", Confidence.CERTAIN),
        DeclarationRename("payments", "payments.status", "payments.state", Confidence.CERTAIN),
    ]

    candidates, _existing = attach_declaration_lineage_evidence(
        [accounts_new, payments_new], [accounts_old, payments_old], renames
    )
    by_id = {e.id: e for e in candidates}
    accounts_claim = _claims_of(by_id[accounts_new.id], IdentityClaimKind.DECLARATION_LINEAGE)
    payments_claim = _claims_of(by_id[payments_new.id], IdentityClaimKind.DECLARATION_LINEAGE)
    assert accounts_claim != payments_claim
