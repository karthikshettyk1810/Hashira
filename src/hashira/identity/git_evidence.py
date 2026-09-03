"""Turning Git rename evidence into identity claims (spec §10).

`GitAdapter` reports what Git detected: "this path became that path, at this
similarity." This module is the next step before that evidence reaches
`hashira.identity.resolver` — it decides *whether* a detected rename is
trustworthy enough to attach as identity evidence at all, and pairs up the
specific entities on either side of the move. Git rename detection is a
similarity heuristic, not a fact (adapter.py's docstring); this module is
where that heuristic earns, or fails to earn, the right to influence identity.

What this module does **not** do: decide `MATCHED` vs `SUPERSEDES`. That
stays the resolver's call — `GIT_RENAME` sits at corroborating tier
(`identity/resolver.py`), so a bare rename alone still produces `SUPERSEDES`
with lineage, not an outright merge, unless something else corroborates it.
This module only makes the connection *visible* to the resolver; it never
forces the resolver's hand.

## Pairing, and where it deliberately refuses to guess

A file rename pairs cleanly at the module level — there is exactly one
module entity per file, so `old_path`/`new_path` alone is enough. Within a
moved file, a symbol only gets paired with its counterpart when the two share
the exact same simple name and that pairing is unambiguous (exactly one
candidate, exactly one existing entity with that name, on either side of the
move). If a symbol was *also* renamed as part of the move — the case the
design discussion called out as "the really interesting one" — there is
nothing here to pair it on, and it is left alone: no claim, no guess, and
the resolver's existing behavior (a disconnected `NEW`, per
`adapters/python/normalizer.py`) stands. That is the correct, conservative
answer when the only two signals available (path and name) both changed at
once.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.entities import Entity, IdentityClaim
from ..core.enums import Confidence, EntityType, IdentityClaimKind, Origin

__all__ = ["DEFAULT_MIN_SIMILARITY", "GitRename", "attach_rename_evidence"]

DEFAULT_MIN_SIMILARITY = 0.90
"""Below this, a "rename" reads as coincidence more than continuity. Git's
own default rename-detection threshold is 50% — generous enough to flag a
mostly-rewritten file as a "rename" of its former self. Requiring 90% here is
deliberately stricter than Git's own bar: "I don't know" is the better answer
than confidently linking two files that only vaguely resemble each other
(the adversarial "disguised replacement" case this project keeps testing
for). See `tests/unit/test_git_evidence.py` for where exactly that line
falls, empirically, against real Git output."""

_NEAR_EXACT_SIMILARITY = 0.98
"""Above this, treat the rename claim as CERTAIN rather than LIKELY -- the
content is essentially untouched, so the residual doubt is only "is a
heuristic match good enough," not "did the content actually change too."""


@dataclass(frozen=True, slots=True)
class GitRename:
    """One rename, as Git detected it — the minimal shape this module needs,
    decoupled from `Observation.payload`'s dict form."""

    old_path: str
    new_path: str
    similarity: float


def _confidence_for(similarity: float) -> Confidence:
    return Confidence.CERTAIN if similarity >= _NEAR_EXACT_SIMILARITY else Confidence.LIKELY


def _claim(value: str, similarity: float) -> IdentityClaim:
    return IdentityClaim(
        kind=IdentityClaimKind.GIT_RENAME,
        value=value,
        origin=Origin.GIT,
        confidence=_confidence_for(similarity),
    )


def _with_claim(entity: Entity, claim: IdentityClaim) -> Entity:
    return entity.model_copy(update={"identity_claims": [*entity.identity_claims, claim]})


def _by_file(entities: dict[str, Entity], entity_type: EntityType, file: str) -> list[Entity]:
    return [
        e
        for e in entities.values()
        if e.type is entity_type and e.source is not None and e.source.file == file
    ]


def _pair_modules(
    candidates: dict[str, Entity], existing: dict[str, Entity], rename: GitRename
) -> None:
    new_modules = _by_file(candidates, EntityType.MODULE, rename.new_path)
    old_modules = _by_file(existing, EntityType.MODULE, rename.old_path)
    if len(new_modules) != 1 or len(old_modules) != 1:
        return
    claim = _claim(f"module::{rename.old_path}::{rename.new_path}", rename.similarity)
    candidates[new_modules[0].id] = _with_claim(new_modules[0], claim)
    existing[old_modules[0].id] = _with_claim(old_modules[0], claim)


def _pair_symbols(
    candidates: dict[str, Entity], existing: dict[str, Entity], rename: GitRename
) -> None:
    new_by_name: dict[str, list[Entity]] = {}
    for entity in _by_file(candidates, EntityType.SYMBOL, rename.new_path):
        new_by_name.setdefault(entity.name, []).append(entity)
    old_by_name: dict[str, list[Entity]] = {}
    for entity in _by_file(existing, EntityType.SYMBOL, rename.old_path):
        old_by_name.setdefault(entity.name, []).append(entity)

    for name, new_matches in new_by_name.items():
        old_matches = old_by_name.get(name)
        if not old_matches or len(new_matches) != 1 or len(old_matches) != 1:
            continue  # absent or ambiguous on either side -- do not guess
        claim = _claim(f"symbol::{rename.old_path}::{rename.new_path}::{name}", rename.similarity)
        candidates[new_matches[0].id] = _with_claim(new_matches[0], claim)
        existing[old_matches[0].id] = _with_claim(old_matches[0], claim)


def attach_rename_evidence(
    candidates: list[Entity],
    existing: list[Entity],
    renames: list[GitRename],
    *,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
) -> tuple[list[Entity], list[Entity]]:
    """Return updated copies of ``candidates`` and ``existing`` where
    entities on either side of a sufficiently confident rename carry a
    matching ``GIT_RENAME`` claim. Neither input list is mutated in place —
    callers that don't need the result can simply ignore it.
    """
    trusted = [r for r in renames if r.similarity >= min_similarity]
    if not trusted:
        return candidates, existing

    updated_candidates = {c.id: c for c in candidates}
    updated_existing = {e.id: e for e in existing}
    for rename in trusted:
        _pair_modules(updated_candidates, updated_existing, rename)
        _pair_symbols(updated_candidates, updated_existing, rename)

    return list(updated_candidates.values()), list(updated_existing.values())
