"""Attaching Git rename evidence: precise pairing, explicit refusal to guess."""

from __future__ import annotations

from hashira.core import Entity, EntityType, IdentityClaimKind, SourceLocation, System
from hashira.identity import GitRename, attach_rename_evidence


def _module(system: System, file: str, qualified_name: str) -> Entity:
    return Entity(
        system_id=system.id,
        type=EntityType.MODULE,
        name=qualified_name.rsplit(".", 1)[-1],
        qualified_name=qualified_name,
        source=SourceLocation(file=file),
    )


def _symbol(system: System, file: str, name: str, qualified_name: str) -> Entity:
    return Entity(
        system_id=system.id,
        type=EntityType.SYMBOL,
        name=name,
        qualified_name=qualified_name,
        source=SourceLocation(file=file),
    )


def _claims_of(entity: Entity, kind: IdentityClaimKind) -> list[str]:
    return [c.value for c in entity.identity_claims if c.kind is kind]


# --- module pairing --------------------------------------------------------


def test_module_rename_pairs_by_file_path(system: System) -> None:
    old_module = _module(system, "shop/payments.py", "shop.payments")
    new_module = _module(system, "shop/billing.py", "shop.billing")
    rename = GitRename(old_path="shop/payments.py", new_path="shop/billing.py", similarity=1.0)

    candidates, existing = attach_rename_evidence([new_module], [old_module], [rename])

    new_claims = _claims_of(candidates[0], IdentityClaimKind.GIT_RENAME)
    old_claims = _claims_of(existing[0], IdentityClaimKind.GIT_RENAME)
    assert new_claims == old_claims
    assert len(new_claims) == 1


def test_below_threshold_similarity_is_not_attached(system: System) -> None:
    old_module = _module(system, "shop/payments.py", "shop.payments")
    new_module = _module(system, "shop/billing.py", "shop.billing")
    rename = GitRename(old_path="shop/payments.py", new_path="shop/billing.py", similarity=0.5)

    candidates, existing = attach_rename_evidence(
        [new_module], [old_module], [rename], min_similarity=0.9
    )
    assert _claims_of(candidates[0], IdentityClaimKind.GIT_RENAME) == []
    assert _claims_of(existing[0], IdentityClaimKind.GIT_RENAME) == []


def test_similarity_exactly_at_the_threshold_is_attached(system: System) -> None:
    old_module = _module(system, "shop/payments.py", "shop.payments")
    new_module = _module(system, "shop/billing.py", "shop.billing")
    rename = GitRename(old_path="shop/payments.py", new_path="shop/billing.py", similarity=0.9)

    candidates, _existing = attach_rename_evidence(
        [new_module], [old_module], [rename], min_similarity=0.9
    )
    assert _claims_of(candidates[0], IdentityClaimKind.GIT_RENAME) != []


def test_near_exact_similarity_is_certain_lower_similarity_is_likely(system: System) -> None:
    from hashira.core import Confidence

    old_a = _module(system, "a.py", "a")
    new_a = _module(system, "b.py", "b")
    old_c = _module(system, "c.py", "c")
    new_c = _module(system, "d.py", "d")

    exact = GitRename(old_path="a.py", new_path="b.py", similarity=1.0)
    modest = GitRename(old_path="c.py", new_path="d.py", similarity=0.91)

    candidates, existing = attach_rename_evidence([new_a, new_c], [old_a, old_c], [exact, modest])
    by_id = {e.id: e for e in [*candidates, *existing]}
    exact_claim = next(
        c for c in by_id[new_a.id].identity_claims if c.kind is IdentityClaimKind.GIT_RENAME
    )
    modest_claim = next(
        c for c in by_id[new_c.id].identity_claims if c.kind is IdentityClaimKind.GIT_RENAME
    )
    assert exact_claim.confidence is Confidence.CERTAIN
    assert modest_claim.confidence is Confidence.LIKELY


# --- symbol pairing ----------------------------------------------------------


def test_symbol_with_unchanged_name_pairs_across_the_move(system: System) -> None:
    old_module = _module(system, "shop/payments.py", "shop.payments")
    old_symbol = _symbol(
        system, "shop/payments.py", "PaymentService", "shop.payments.PaymentService"
    )
    new_module = _module(system, "shop/billing.py", "shop.billing")
    new_symbol = _symbol(system, "shop/billing.py", "PaymentService", "shop.billing.PaymentService")
    rename = GitRename(old_path="shop/payments.py", new_path="shop/billing.py", similarity=1.0)

    candidates, existing = attach_rename_evidence(
        [new_module, new_symbol], [old_module, old_symbol], [rename]
    )
    by_id = {e.id: e for e in [*candidates, *existing]}
    new_symbol_claims = _claims_of(by_id[new_symbol.id], IdentityClaimKind.GIT_RENAME)
    old_symbol_claims = _claims_of(by_id[old_symbol.id], IdentityClaimKind.GIT_RENAME)
    assert new_symbol_claims == old_symbol_claims
    assert len(new_symbol_claims) == 1
    # The symbol's own claim value must be distinct from the module's.
    module_claim = _claims_of(by_id[new_module.id], IdentityClaimKind.GIT_RENAME)
    assert module_claim != new_symbol_claims


def test_symbol_renamed_during_the_move_gets_no_claim(system: System) -> None:
    """The adversarial case this design discussion called "the really
    interesting one": the file moved AND the symbol's name changed. Path and
    name are the only two things to pair on, and both changed at once --
    nothing here justifies connecting them, so neither gets a GIT_RENAME
    claim. They fall back to whatever the resolver already does (a
    disconnected NEW), which is correct."""
    old_module = _module(system, "shop/payments.py", "shop.payments")
    old_symbol = _symbol(
        system, "shop/payments.py", "PaymentService", "shop.payments.PaymentService"
    )
    new_module = _module(system, "shop/billing.py", "shop.billing")
    new_symbol = _symbol(
        system, "shop/billing.py", "PaymentProcessor", "shop.billing.PaymentProcessor"
    )
    rename = GitRename(old_path="shop/payments.py", new_path="shop/billing.py", similarity=1.0)

    candidates, existing = attach_rename_evidence(
        [new_module, new_symbol], [old_module, old_symbol], [rename]
    )
    by_id = {e.id: e for e in [*candidates, *existing]}
    assert _claims_of(by_id[new_symbol.id], IdentityClaimKind.GIT_RENAME) == []
    assert _claims_of(by_id[old_symbol.id], IdentityClaimKind.GIT_RENAME) == []
    # The module itself still pairs -- only the symbol-level rename is ambiguous.
    assert _claims_of(by_id[new_module.id], IdentityClaimKind.GIT_RENAME) != []


def test_ambiguous_name_collision_within_the_moved_file_gets_no_claim(system: System) -> None:
    """Two symbols share a name on one side of the move -- pairing by name
    alone would be a guess, so neither is linked."""
    old_module = _module(system, "shop/payments.py", "shop.payments")
    old_a = _symbol(system, "shop/payments.py", "Helper", "shop.payments.Helper")
    old_b = _symbol(system, "shop/payments.py", "OtherHelper", "shop.payments.OtherHelper")
    new_module = _module(system, "shop/billing.py", "shop.billing")
    new_a = _symbol(system, "shop/billing.py", "Helper", "shop.billing.Helper")
    new_b = _symbol(system, "shop/billing.py", "Helper", "shop.billing.Helper2")  # name collision
    rename = GitRename(old_path="shop/payments.py", new_path="shop/billing.py", similarity=1.0)

    candidates, existing = attach_rename_evidence(
        [new_module, new_a, new_b], [old_module, old_a, old_b], [rename]
    )
    by_id = {e.id: e for e in [*candidates, *existing]}
    assert _claims_of(by_id[new_a.id], IdentityClaimKind.GIT_RENAME) == []
    assert _claims_of(by_id[new_b.id], IdentityClaimKind.GIT_RENAME) == []
    assert _claims_of(by_id[old_a.id], IdentityClaimKind.GIT_RENAME) == []


def test_symbol_present_only_on_one_side_gets_no_claim(system: System) -> None:
    """A genuinely new symbol added in the same commit as an unrelated
    rename must not be swept into that rename's evidence."""
    old_module = _module(system, "shop/payments.py", "shop.payments")
    new_module = _module(system, "shop/billing.py", "shop.billing")
    brand_new_symbol = _symbol(system, "shop/billing.py", "NewThing", "shop.billing.NewThing")
    rename = GitRename(old_path="shop/payments.py", new_path="shop/billing.py", similarity=1.0)

    candidates, _existing = attach_rename_evidence(
        [new_module, brand_new_symbol], [old_module], [rename]
    )
    by_id = {e.id: e for e in candidates}
    assert _claims_of(by_id[brand_new_symbol.id], IdentityClaimKind.GIT_RENAME) == []


# --- overall shape -----------------------------------------------------------


def test_no_renames_returns_inputs_unchanged(system: System) -> None:
    module = _module(system, "a.py", "a")
    candidates, existing = attach_rename_evidence([module], [], [])
    assert candidates == [module]
    assert existing == []


def test_original_lists_are_not_mutated(system: System) -> None:
    old_module = _module(system, "shop/payments.py", "shop.payments")
    new_module = _module(system, "shop/billing.py", "shop.billing")
    rename = GitRename(old_path="shop/payments.py", new_path="shop/billing.py", similarity=1.0)

    candidates_in = [new_module]
    existing_in = [old_module]
    attach_rename_evidence(candidates_in, existing_in, [rename])
    assert candidates_in[0].identity_claims == []
    assert existing_in[0].identity_claims == []
