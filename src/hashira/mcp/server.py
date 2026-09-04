"""The MCP read surface: a transport layer over `application/`, nothing more.

```
Agent -> MCP -> Application services -> System IR / Storage / History
```

Never the reverse of that last arrow. Every tool function below does exactly
one thing: open a `UnitOfWork`, call exactly one (or, for `query_at_revision`,
two closely related) `application.*` function, serialize the result
(`serialize.py`), and hand it back. Nothing here parses a SHA, walks a
graph, decides an identity outcome, or touches storage/adapter internals
directly -- if a tool needed to do any of that, the right fix is a new
`application/` function, not more logic in this file.

**Eight tools, matching the milestone's own scope**: `get_entity`,
`get_relationships`, `query_at_revision`, `reverse_impact`, `forward_impact`,
`summarize_impact`, `follow_lineage`, and `search_entities` (discovery, not
identity -- see `application/search.py`'s own docstring on why it is
deliberately the only name-based tool here). A typical agent flow chains
them: `search_entities` to find an id, `reverse_impact`/`forward_impact` to
see what that id touches, `summarize_impact` to turn a large result into a
navigable projection (grouped, deduplicated, coverage still up front --
`application/impact.py`'s "Impact Presentation v0.1" entry) before deciding
where to look closer, `get_entity`/`get_relationships` to inspect one node
or edge in full, `query_at_revision` to see the same shape at a different
point in history, `follow_lineage` to cross a rename.

`summarize_impact` is deliberately a separate tool from `reverse_impact`/
`forward_impact`, not a mode flag on them: `reverse_impact`/`forward_impact`
stay the authoritative, lossless analysis; `summarize_impact` is a
projection *of* that analysis, computed from it, never a second traversal
-- keeping them as distinct tools makes that distinction visible at the
protocol level, not just in a docstring an agent might not read.

**Read-only, on purpose.** Nothing here mutates a system, triggers an
index run, calls an LLM, or executes anything -- an agent can ask Hashira
questions; it cannot yet make Hashira do anything. That boundary is not an
oversight to fix in v0.2; see `docs/ROADMAP.md`'s entry on this milestone
for why it stays deliberate for now.

**Bound to one system at construction**, not per-call: every tool operates
on the `system_id` given to `build_server`, matching how `IndexingService`
itself is bound to one `uow_factory`. Multi-system/multi-tenant routing is
explicitly out of scope for a local, single-user stdio server.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer

from ..application import graph as graph_queries
from ..application import search as search_queries
from ..application.impact import follow_lineage as follow_lineage_
from ..application.impact import forward_impact as forward_impact_
from ..application.impact import reverse_impact as reverse_impact_
from ..application.impact import summarize_impact as summarize_impact_
from ..core.enums import RelationshipType
from ..core.ids import SystemID
from ..ports.repositories import UnitOfWork
from . import serialize

__all__ = ["build_server"]

_INSTRUCTIONS = (
    "Read-only access to one Hashira system: an evidence-backed graph of "
    "entities and relationships, queryable at any indexed revision. Start "
    "with search_entities to find an id if you do not already have one. "
    "Every relationship carries its own confidence and evidence -- prefer "
    "trusting what a tool actually returned over inferring from names alone. "
    "reverse_impact/forward_impact also return a coverage field -- check "
    "it before treating a short or empty result as proof nothing else is "
    "affected; PARTIAL coverage means known blind spots exist, not that "
    "the search came up empty."
)


def _parse_relationship_types(types: list[str] | None) -> tuple[RelationshipType, ...] | None:
    """Raises `ValueError` (caught by callers, turned into an error result)
    on a name that is not one of `core.enums.RelationshipType`'s members --
    never silently ignored, never guessed at."""
    if types is None:
        return None
    return tuple(RelationshipType(t) for t in types)


def build_server(
    uow_factory: Callable[[], UnitOfWork],
    *,
    system_id: SystemID,
    name: str = "hashira",
    version: str = "0.1.0",
) -> MCPServer:
    server: MCPServer = MCPServer(name=name, version=version, instructions=_INSTRUCTIONS)

    @server.tool()
    def get_entity(entity_id: str) -> dict[str, Any]:
        """Look up one entity by its opaque Hashira id -- its own stored
        state (type, qualified name, source, identity claims, first/last
        seen revision, status), not a revision-scoped reconstruction."""
        with uow_factory() as uow:
            entity = graph_queries.get_entity(uow, entity_id=entity_id)
        if entity is None:
            return {"found": False, "entity_id": entity_id}
        return {"found": True, "entity": serialize.serialize_entity(entity)}

    @server.tool()
    def get_relationships(
        entity_id: str,
        direction: str = "out",
        types: list[str] | None = None,
        revision: str | None = None,
    ) -> dict[str, Any]:
        """Every relationship touching `entity_id`. `direction` is "out",
        "in", or "both". `types` restricts to specific relationship type
        names (e.g. ["CALLS", "WRITES"]); omit for all types. `revision`
        asks "as of this revision" (omit for current)."""
        try:
            rel_types = _parse_relationship_types(types)
        except ValueError as exc:
            return {"error": str(exc)}
        with uow_factory() as uow:
            relationships = graph_queries.get_relationships(
                uow,
                system_id=system_id,
                entity_id=entity_id,
                direction=direction,
                types=rel_types,
                revision=revision,
            )
        return {
            "entity_id": entity_id,
            "direction": direction,
            "revision": revision,
            "relationships": [serialize.serialize_relationship(r) for r in relationships],
        }

    @server.tool()
    def query_at_revision(entity_id: str, revision: str) -> dict[str, Any]:
        """What this one entity looked like, and what it was connected to,
        as of `revision` -- the historical primitive
        (`application.history.query_at_revision`), scoped to one entity's
        neighborhood rather than the whole graph."""
        with uow_factory() as uow:
            entity = graph_queries.get_entity_at_revision(
                uow, system_id=system_id, entity_id=entity_id, revision=revision
            )
            if entity is None:
                return {"found": False, "entity_id": entity_id, "revision": revision}
            relationships = graph_queries.get_relationships(
                uow,
                system_id=system_id,
                entity_id=entity_id,
                direction="both",
                revision=revision,
            )
        return {
            "found": True,
            "revision": revision,
            "entity": serialize.serialize_entity(entity),
            "relationships": [serialize.serialize_relationship(r) for r in relationships],
        }

    @server.tool()
    def reverse_impact(
        entity_id: str,
        revision: str | None = None,
        edge_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """What, directly or transitively, depends on `entity_id`? Every
        path and every hop is returned losslessly -- relationship type,
        evidence, confidence, revision -- never collapsed to a flat list of
        names. `entity_id` may name an entity a later revision superseded;
        the result's `resolved_from` says so if lineage was followed to
        answer this.

        Always check `coverage` before treating `paths` as exhaustive.
        `coverage.status` is "PARTIAL" whenever `coverage.limitations` is
        non-empty -- a short or even empty `paths` list under PARTIAL
        coverage means "this is what was found," not "this is everything
        that exists." Each entry in `coverage.limitations` is a typed
        category (`kind`, `scope`, `detail`), e.g. raw SQL not being
        analyzed, or a variable's type not being resolvable through a
        supported form -- read `kind` to know *what class* of edge might
        be missing, not just that something might be."""
        try:
            types = _parse_relationship_types(edge_types)
        except ValueError as exc:
            return {"error": str(exc)}
        with uow_factory() as uow:
            try:
                result = reverse_impact_(
                    uow,
                    system_id=system_id,
                    entity_id=entity_id,
                    revision=revision,
                    edge_types=frozenset(types) if types is not None else None,
                )
            except KeyError as exc:
                return {"error": str(exc)}
        return serialize.serialize_impact_result(result)

    @server.tool()
    def forward_impact(
        entity_id: str,
        revision: str | None = None,
        edge_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """What does `entity_id`, directly or transitively, depend on? The
        mirror of `reverse_impact` -- same lossless path/hop shape, same
        identity-lineage-aware `entity_id` resolution, same `coverage`
        caveat: check it before treating `paths` as exhaustive."""
        try:
            types = _parse_relationship_types(edge_types)
        except ValueError as exc:
            return {"error": str(exc)}
        with uow_factory() as uow:
            try:
                result = forward_impact_(
                    uow,
                    system_id=system_id,
                    entity_id=entity_id,
                    revision=revision,
                    edge_types=frozenset(types) if types is not None else None,
                )
            except KeyError as exc:
                return {"error": str(exc)}
        return serialize.serialize_impact_result(result)

    @server.tool()
    def summarize_impact(
        entity_id: str,
        direction: str = "reverse",
        revision: str | None = None,
        edge_types: list[str] | None = None,
    ) -> dict[str, Any]:
        """The navigable projection of `reverse_impact`/`forward_impact`:
        same underlying analysis, computed the same way (`direction` selects
        which -- "reverse" for what depends on `entity_id`, "forward" for
        what it depends on), but grouped by the affected entities' own
        source-directory structure and deduplicated to bare ids instead of a
        flat list of fully-materialized paths. Use this first on a result
        you expect to be large; `get_entity`/`get_relationships` remain the
        way to inspect any specific entity or edge this surfaces in full.

        `coverage` is unchanged from the underlying result and still comes
        first in the response -- check it before treating `groups` as
        exhaustive, exactly as for `reverse_impact`/`forward_impact`.
        `groups` are ordered by `entity_count` descending only; that is not
        a relevance ranking, so decide what matters from the graph
        structure `key` (a directory) and `path_count`/`entity_count`
        expose, not from position in the list. Every `entity_id` named
        anywhere in `groups` is a real id from the underlying
        `reverse_impact`/`forward_impact` result, unchanged -- nothing here
        is inferred, only grouped, counted, and referenced."""
        if direction not in ("reverse", "forward"):
            return {"error": f"direction must be 'reverse' or 'forward', got {direction!r}"}
        try:
            types = _parse_relationship_types(edge_types)
        except ValueError as exc:
            return {"error": str(exc)}
        traversal = reverse_impact_ if direction == "reverse" else forward_impact_
        with uow_factory() as uow:
            try:
                result = traversal(
                    uow,
                    system_id=system_id,
                    entity_id=entity_id,
                    revision=revision,
                    edge_types=frozenset(types) if types is not None else None,
                )
            except KeyError as exc:
                return {"error": str(exc)}
        return serialize.serialize_impact_summary(summarize_impact_(result))

    @server.tool()
    def follow_lineage(entity_id: str) -> dict[str, Any]:
        """What happened to this entity, across all of history: every
        `SUPERSEDES` predecessor (what it replaced) and successor (what
        replaced it), each with the evidence that justified the lineage.
        Not revision-scoped -- lineage is a permanent fact, not a
        point-in-time one."""
        with uow_factory() as uow:
            try:
                result = follow_lineage_(uow, system_id=system_id, entity_id=entity_id)
            except KeyError as exc:
                return {"error": str(exc)}
        return serialize.serialize_lineage_result(result)

    @server.tool()
    def search_entities(query: str, limit: int = 20) -> dict[str, Any]:
        """Find an entity id by name -- exact/substring text matching only,
        no fuzzy ranking or semantic search. Discovery, not identity: use
        the returned ids for every other tool's `entity_id` argument
        afterward, rather than searching again."""
        with uow_factory() as uow:
            results = search_queries.search_entities(
                uow, system_id=system_id, query=query, limit=limit
            )
        return {"query": query, "results": [serialize.serialize_entity(e) for e in results]}

    return server
