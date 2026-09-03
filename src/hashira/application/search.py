"""Basic textual entity search -- discovery, not identity.

Every other function in `application/` takes an opaque entity id as its
starting point, on purpose (§10: identity is not a name). This module is
the one, deliberately narrow exception: a way for a caller that does not
yet have an id -- a human, or an agent's first move in a conversation -- to
find one. Exact/prefix/substring matching on `name`/`qualified_name` only.
No fuzzy ranking, no semantic embeddings, no relevance scoring: a caller
that wants precision should already be past this function, using the id it
returned.
"""

from __future__ import annotations

from ..core.entities import Entity
from ..core.ids import SystemID
from ..ports.repositories import UnitOfWork

__all__ = ["search_entities"]

#: Matches `application.indexing._ALL_ENTITIES_LIMIT`: fixture/demo scale.
_ALL_ENTITIES_LIMIT = 1_000_000


def search_entities(
    uow: UnitOfWork, *, system_id: SystemID, query: str, limit: int = 20
) -> list[Entity]:
    """Entities whose `name` or `qualified_name` contains `query`
    (case-insensitive substring match), most-recently-updated first. An
    empty or blank `query` matches nothing -- deliberately not "return
    everything", which is what `application.graph`/direct storage queries
    are for."""
    needle = query.strip().lower()
    if not needle:
        return []

    candidates = uow.graph.find_entities(system_id, limit=_ALL_ENTITIES_LIMIT)
    matches = [
        e
        for e in candidates
        if needle in e.name.lower() or (e.qualified_name and needle in e.qualified_name.lower())
    ]
    matches.sort(key=lambda e: e.updated_at, reverse=True)
    return matches[:limit]
