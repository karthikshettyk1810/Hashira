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
moved file, a symbol is paired by exact simple-name equality when that
pairing is unambiguous (exactly one candidate, exactly one existing entity
with that name, on either side of the move). When the class name also changed,
we still allow a symbol-level claim only if there is exactly one relevant
top-level class on each side and its direct method names and parameter
signatures are an exact match, which is enough to preserve a legitimate Git
rename without turning the rename layer into a fuzzy similarity system.
If the file move is ambiguous or the structural invariants do not match, the
symbol is left alone: no claim, no guess, and the resolver's existing
behavior (a disconnected `NEW`, per `adapters/python/normalizer.py`) stands.
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


def _top_level_classes(symbols: list[Entity]) -> list[Entity]:
    return [
        entity
        for entity in symbols
        if entity.metadata.get("kind") == "class" and entity.metadata.get("parent_kind") == "module"
    ]


def _method_signature(
    class_entity: Entity, symbols: list[Entity]
) -> tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] | None:
    """Return exact direct method names and parameter names/kinds for a class.

    Missing or malformed parameter metadata is insufficient evidence; this
    deliberately fails closed rather than comparing only whatever fields
    happened to be present.
    """
    class_qn = class_entity.qualified_name
    if class_qn is None:
        return None

    members: list[tuple[str, str, tuple[tuple[str, str], ...]]] = []
    for entity in symbols:
        qualified_name = entity.qualified_name
        metadata = entity.metadata
        method_kind = metadata.get("kind")
        if (
            qualified_name is None
            or not qualified_name.startswith(f"{class_qn}.")
            or "." in qualified_name[len(class_qn) + 1 :]
            or metadata.get("parent_kind") != "class"
            or not isinstance(method_kind, str)
            or method_kind not in {"method", "async_function"}
        ):
            continue

        raw_parameters = metadata.get("parameters")
        if not isinstance(raw_parameters, list):
            return None
        parameters: list[tuple[str, str]] = []
        for parameter in raw_parameters:
            if not isinstance(parameter, dict):
                return None
            kind, name = parameter.get("kind"), parameter.get("name")
            if not isinstance(kind, str) or not isinstance(name, str):
                return None
            parameters.append((kind, name))
        members.append((entity.name, method_kind, tuple(parameters)))

    if not members or len({name for name, _, _ in members}) != len(members):
        return None
    return tuple(sorted(members))


def _pair_symbols(
    candidates: dict[str, Entity], existing: dict[str, Entity], rename: GitRename
) -> None:
    new_symbols = _by_file(candidates, EntityType.SYMBOL, rename.new_path)
    old_symbols = _by_file(existing, EntityType.SYMBOL, rename.old_path)
    new_by_name: dict[str, list[Entity]] = {}
    for entity in new_symbols:
        new_by_name.setdefault(entity.name, []).append(entity)
    old_by_name: dict[str, list[Entity]] = {}
    for entity in old_symbols:
        old_by_name.setdefault(entity.name, []).append(entity)

    for name, new_matches in new_by_name.items():
        old_matches = old_by_name.get(name)
        if not old_matches or len(new_matches) != 1 or len(old_matches) != 1:
            continue  # absent or ambiguous on either side -- do not guess
        claim = _claim(f"symbol::{rename.old_path}::{rename.new_path}::{name}", rename.similarity)
        candidates[new_matches[0].id] = _with_claim(new_matches[0], claim)
        existing[old_matches[0].id] = _with_claim(old_matches[0], claim)

    new_classes = _top_level_classes(new_symbols)
    old_classes = _top_level_classes(old_symbols)
    if len(new_classes) == 1 and len(old_classes) == 1:
        new_class = new_classes[0]
        old_class = old_classes[0]
        if new_class.name == old_class.name:
            return
        old_members = _method_signature(old_class, old_symbols)
        new_members = _method_signature(new_class, new_symbols)
        if old_members is None or new_members is None or old_members != new_members:
            return
        claim = _claim(
            f"symbol::{rename.old_path}::{rename.new_path}::{old_class.name}->{new_class.name}",
            rename.similarity,
        )
        candidates[new_class.id] = _with_claim(new_class, claim)
        existing[old_class.id] = _with_claim(old_class, claim)


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
