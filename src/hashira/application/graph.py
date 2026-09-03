"""Direct, id-keyed graph lookups (§24's `get_entity`/`get_relationships`
primitives) -- the small, boring queries every richer capability in this
package (`history.py`, `impact.py`) is ultimately built out of, pulled out
on their own so a transport layer (`hashira.mcp`) has something to call
that is neither "the whole storage port" nor "a bespoke traversal".

Kept deliberately thin: `get_entity` is a direct passthrough to
`GraphRepository.get_entity`, and `get_relationships` is `query_at_revision`
plus a direction/type filter -- the same pattern `application.impact`
already established for revision-aware queries, reused here instead of
re-derived.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..core.entities import Entity
from ..core.enums import RelationshipType
from ..core.ids import SystemID
from ..core.relationships import Relationship
from ..ports.repositories import UnitOfWork
from .history import query_at_revision

__all__ = ["get_entity", "get_entity_at_revision", "get_relationships"]


def get_entity(uow: UnitOfWork, *, entity_id: str) -> Entity | None:
    """The entity's own stored state, by its opaque id -- whatever status
    it currently carries (`ACTIVE`, `SUPERSEDED`, ...), not a revision-scoped
    reconstruction. Use `get_entity_at_revision` to ask "was this present,
    and how, as of revision X"."""
    return uow.graph.get_entity(entity_id)


def get_entity_at_revision(
    uow: UnitOfWork, *, system_id: SystemID, entity_id: str, revision: str | None
) -> Entity | None:
    """Whether `entity_id` was present as of `revision` (`None` means
    today), and its state then, per `application.history.query_at_revision`
    -- `None` if it was not present at that point, distinct from `get_entity`
    which always returns whatever is in storage regardless of revision."""
    graph = query_at_revision(uow, system_id=system_id, revision=revision)
    return next((e for e in graph.entities if e.id == entity_id), None)


def get_relationships(
    uow: UnitOfWork,
    *,
    system_id: SystemID,
    entity_id: str,
    direction: str = "out",
    types: Sequence[RelationshipType] | None = None,
    revision: str | None = None,
) -> list[Relationship]:
    """Every relationship touching `entity_id`, in the given direction
    (`"out"`, `"in"`, or anything else meaning both), optionally restricted
    to specific types, as of `revision` (`None` means current) -- built on
    `query_at_revision` for both cases uniformly rather than a separate,
    non-historical code path."""
    graph = query_at_revision(uow, system_id=system_id, revision=revision)

    def matches(rel: Relationship) -> bool:
        if direction == "out" and rel.source_entity_id != entity_id:
            return False
        if direction == "in" and rel.target_entity_id != entity_id:
            return False
        if direction not in ("out", "in") and entity_id not in (
            rel.source_entity_id,
            rel.target_entity_id,
        ):
            return False
        return types is None or rel.type in types

    return [rel for rel in graph.relationships if matches(rel)]
