"""Turning an adapter's before/after declaration comparison into identity
claims (spec §10) — the sibling of `git_evidence.py` for the case that
module deliberately refuses to handle: a construct renamed *within* a file
that itself was never renamed, so Git's own file-level heuristic has
nothing to detect at all.

## Where this evidence actually comes from

`GitAdapter` reports raw facts only — a file's content *before* a commit,
attached to a `git.file_change` observation when that file was merely
`MODIFIED` (`adapters/git/adapter.py`). It does not know what a
"declaration" is, and it never will; that is deliberately out of its
vocabulary, matching this project's whole architecture (`ports/adapters.py`:
"an adapter... reports what it saw" [§18]).

An adapter that *does* understand its own language's declarations (today,
`adapters/sqlalchemy/adapter.py`, for SQLAlchemy columns) re-parses that old
content with the exact same extraction logic it already runs on the current
source, and compares: which declarations disappeared, which appeared, in
which containing entity (a table, for SQLAlchemy). When there is exactly one
of each on either side — the containing entity itself unambiguous too — that
adapter reports a `DeclarationRename` fact to `attach_declaration_lineage_evidence`
below, which is this module's entire job: pair the specific entities on
either side and attach a matching `DECLARATION_LINEAGE` claim, exactly the
way `git_evidence.py::attach_rename_evidence` pairs entities across a file
move. Neither function decides `MATCHED` vs `SUPERSEDES` — that stays the
resolver's call (`DECLARATION_LINEAGE` sits at corroborating tier, so this
alone produces `SUPERSEDES` with lineage, never an outright merge).

## Deliberately not fuzzy matching

The evidence this module accepts is structural, not textual: "this was the
only candidate that disappeared here, and that was the only candidate that
appeared here" — never "these two names/types look similar enough". A
second candidate on either side, or an unresolved containing entity, means
no claim at all, on either side — the conservative, correct answer is to
leave both entities exactly as every other adapter's evidence-free case
already leaves them: the old one orphaned (still `ACTIVE`, no lineage), the
new one a disconnected `NEW`. This module never invents a threshold to
paper over that; producing a `DeclarationRename` in the first place is
entirely the calling adapter's judgment call, made with its own domain
knowledge (see `adapters/sqlalchemy/adapter.py`'s module docstring for
exactly which cases it does, and deliberately does not, propose one for).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.entities import Entity, IdentityClaim
from ..core.enums import Confidence, IdentityClaimKind, Origin

__all__ = ["DeclarationRename", "attach_declaration_lineage_evidence"]


@dataclass(frozen=True, slots=True)
class DeclarationRename:
    """One adapter-proposed correspondence between a declaration that
    disappeared and one that appeared, in the same revision, in the same
    containing entity. The minimal shape this module needs, decoupled from
    any one adapter's own observation payload shape."""

    container_qualified_name: str
    """The containing entity both declarations belong to (e.g. a table's
    qualified name) — carried through onto the claim's value for a
    human-readable audit trail, not itself re-checked here."""
    old_qualified_name: str
    new_qualified_name: str
    confidence: Confidence


def _claim(rename: DeclarationRename) -> IdentityClaim:
    return IdentityClaim(
        kind=IdentityClaimKind.DECLARATION_LINEAGE,
        value=(
            f"declaration::{rename.container_qualified_name}::"
            f"{rename.old_qualified_name}::{rename.new_qualified_name}"
        ),
        origin=Origin.STATIC_ANALYSIS,
        confidence=rename.confidence,
    )


def _with_claim(entity: Entity, claim: IdentityClaim) -> Entity:
    return entity.model_copy(update={"identity_claims": [*entity.identity_claims, claim]})


def _by_qualified_name(entities: dict[str, Entity], qualified_name: str) -> list[Entity]:
    return [e for e in entities.values() if e.qualified_name == qualified_name]


def attach_declaration_lineage_evidence(
    candidates: list[Entity], existing: list[Entity], renames: list[DeclarationRename]
) -> tuple[list[Entity], list[Entity]]:
    """Return updated copies of ``candidates`` and ``existing`` where
    entities on either side of a proposed declaration correspondence carry
    a matching ``DECLARATION_LINEAGE`` claim. Neither input list is mutated
    in place. A ``rename`` whose named entities are not each unambiguously
    present (exactly one match, on each side, in the pool actually being
    resolved this run) is silently skipped — the adapter's own proposal is
    not blindly trusted; it is re-checked against the real pool, same
    discipline as `git_evidence.py::_pair_symbols`.
    """
    if not renames:
        return candidates, existing

    updated_candidates = {c.id: c for c in candidates}
    updated_existing = {e.id: e for e in existing}
    for rename in renames:
        new_matches = _by_qualified_name(updated_candidates, rename.new_qualified_name)
        old_matches = _by_qualified_name(updated_existing, rename.old_qualified_name)
        if len(new_matches) != 1 or len(old_matches) != 1:
            continue  # ambiguous or absent on either side -- do not guess
        claim = _claim(rename)
        updated_candidates[new_matches[0].id] = _with_claim(new_matches[0], claim)
        updated_existing[old_matches[0].id] = _with_claim(old_matches[0], claim)

    return list(updated_candidates.values()), list(updated_existing.values())
