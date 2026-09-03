"""Stage 2 for FastAPI: `fastapi.*` observations + Python's own normalized
entities -> a cross-domain graph.

Runs *after* `adapters.python.normalizer.normalize`, exactly like Django's
own normalizer and for the same reason: linking a route to its handler
requires that handler to already exist as a candidate entity with a real
qualified name to look up.

`normalize` (this module's public export) is a drop-in `Normalizer` -- same
signature as `adapters.python.normalizer.normalize` and
`adapters.django.normalizer.normalize` -- so it can be handed directly to
`IndexingService(..., normalize=hashira.adapters.fastapi.normalize)`.

**The identity discipline this milestone was built to test.** A route
handler is *tagged*, not duplicated: it stays whatever Python already
extracted it as (`EntityType.SYMBOL`), with `metadata.framework`/
`fastapi_kind` set -- the same rule `adapters.django.normalizer` applies to
model/view classes (`core/base.py`'s rule that technology detail belongs in
metadata, never a reshaped core envelope). A route itself gets a freshly
minted `INTERFACE` entity, exactly like Django's `django.url_route` --
`/checkout/ [POST]` has independent system meaning and no Python-symbol
identity of its own, so it is not a tag on anything, it *is* something.

**`DEPENDS_ON` is not `CALLS`.** A `Depends(get_payment_service)` default
parameter is a graph edge in its own right (`fastapi.dependency`
observations, from `adapter.py`), kept separate from the `CALLS` edges
Python's own normalizer already produces for the handler's actual function
body -- FastAPI's dependency-injection semantics are stronger than "this
syntax happens to contain a call expression."

**Request/response model association stays basic, and stays out of the
graph as edges.** `adapter.py` already resolved which Pydantic model (if
any) a handler's request body and return type refer to; this normalizer
records that as metadata on the handler entity (`request_model_qualified_name`
/ `response_model_qualified_name`) and tags the model class itself
(`fastapi_kind="schema"`), rather than minting a new relationship type for
it. Neither `CONSUMES`/`PRODUCES` (§11's existing pair means message-queue
direction elsewhere in this codebase -- see `docs/ARCHITECTURE.md`'s Celery
example) nor `DEPENDS_ON` (reserved for `Depends()` above) is the right fit,
and inventing a new type for two adapters' first pass at this is exactly the
premature taxonomy the milestone's own design discussion warned against.
"""

from __future__ import annotations

from collections.abc import Sequence

from ...core.entities import Entity
from ...core.enums import EntityType, Origin, RelationshipType
from ...core.evidence import Observation
from ...core.ids import SystemID
from ...core.relationships import Relationship
from .._dedup import deduplicate_relationships
from .._identity_claims import declaration_anchor_claim, qualified_name_claim
from ..python.normalizer import NormalizedRun
from ..python.normalizer import normalize as normalize_python

__all__ = ["enrich_normalized_run", "normalize"]


def normalize(
    observations: Sequence[Observation], *, system_id: SystemID, revision: str | None
) -> NormalizedRun:
    python_run = normalize_python(observations, system_id=system_id, revision=revision)
    return enrich_normalized_run(python_run, observations, system_id=system_id, revision=revision)


def enrich_normalized_run(
    python_run: NormalizedRun,
    observations: Sequence[Observation],
    *,
    system_id: SystemID,
    revision: str | None,
) -> NormalizedRun:
    """`normalize`'s actual logic, taking an already-computed Python-level
    `NormalizedRun` instead of deriving one itself -- what a generic
    multi-enricher composer (`adapters._compose.compose_normalizers`) calls
    directly, so two enrichers can layer onto *one* shared entity pool
    instead of each re-deriving (and re-minting fresh ids for) the same
    underlying Python constructs. Not part of the `Normalizer` protocol
    itself; `normalize` above is what satisfies that."""
    by_qn: dict[str, Entity] = {
        e.qualified_name: e for e in python_run.entities if e.qualified_name
    }

    new_entities: list[Entity] = []
    new_relationships: list[Relationship] = []
    unresolved: list[Observation] = list(python_run.unresolved)

    for obs in observations:
        if obs.kind != "fastapi.route":
            continue
        handler_qn = str(obs.payload["handler_qualified_name"])
        handler = by_qn.get(handler_qn)
        if handler is None:
            unresolved.append(obs)
            continue

        request_qn = obs.payload.get("request_model_qualified_name")
        response_qn = obs.payload.get("response_model_qualified_name")
        handler_metadata = {
            **handler.metadata,
            "framework": "fastapi",
            "fastapi_kind": "route_handler",
        }
        if request_qn:
            handler_metadata["request_model_qualified_name"] = request_qn
            _tag_schema(by_qn, str(request_qn))
        if response_qn:
            handler_metadata["response_model_qualified_name"] = response_qn
            _tag_schema(by_qn, str(response_qn))
        handler = handler.model_copy(update={"metadata": handler_metadata})
        by_qn[handler_qn] = handler

        method = str(obs.payload["http_method"])
        path = str(obs.payload["path"])
        var_qn = str(obs.payload["var_qualified_name"])
        route_qn = f"{var_qn}:{method} {path}"
        route_entity = Entity(
            system_id=system_id,
            type=EntityType.INTERFACE,
            name=f"{method} {path}",
            qualified_name=route_qn,
            source=handler.source,
            technology=handler.technology,
            metadata={"kind": "fastapi_route", "http_method": method, "path": path},
            identity_claims=[
                qualified_name_claim(route_qn),
                declaration_anchor_claim(
                    (handler.source.file if handler.source else None) or "",
                    route_qn,
                    "fastapi_route",
                ),
            ],
            first_seen_revision=revision,
            last_seen_revision=revision,
        )
        new_entities.append(route_entity)
        new_relationships.append(
            Relationship(
                system_id=system_id,
                source_entity_id=route_entity.id,
                target_entity_id=handler.id,
                type=RelationshipType.EXPOSES,
                origin=Origin.STATIC_ANALYSIS,
                evidence_ids=list(obs.evidence_ids),
                valid_from_revision=revision,
            )
        )

    for obs in observations:
        if obs.kind != "fastapi.dependency":
            continue
        dependent = by_qn.get(str(obs.payload["dependent_qualified_name"]))
        dependency_qn = obs.payload.get("resolved_dependency_qualified_name")
        dependency = by_qn.get(str(dependency_qn)) if dependency_qn else None
        if dependent is None or dependency is None:
            unresolved.append(obs)
            continue
        new_relationships.append(
            Relationship(
                system_id=system_id,
                source_entity_id=dependent.id,
                target_entity_id=dependency.id,
                type=RelationshipType.DEPENDS_ON,
                origin=Origin.STATIC_ANALYSIS,
                evidence_ids=list(obs.evidence_ids),
                valid_from_revision=revision,
            )
        )

    final_entities = [*by_qn.values(), *new_entities]
    final_relationships = deduplicate_relationships([*python_run.relationships, *new_relationships])
    return NormalizedRun(
        entities=final_entities, relationships=final_relationships, unresolved=unresolved
    )


def _tag_schema(by_qn: dict[str, Entity], qualified_name: str) -> None:
    """Mark a Pydantic model class as a FastAPI request/response schema, in
    place -- same entity, same id, same identity claims (it is still
    fundamentally "a Python class", `core/base.py`'s own rule)."""
    entity = by_qn.get(qualified_name)
    if entity is None:
        return
    by_qn[qualified_name] = entity.model_copy(
        update={"metadata": {**entity.metadata, "framework": "fastapi", "fastapi_kind": "schema"}}
    )
