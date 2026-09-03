"""Stage 2 for Django: `django.*` observations + Python's own normalized
entities -> a cross-domain graph.

This is what turns "a class extends `models.Model`" into an actual
`DATA_ENTITY`-flavored graph node with `CONTAINS` edges to its fields, and a
URL pattern into an `INTERFACE` entity that `EXPOSES` the view Python already
resolved. It runs *after* `adapters.python.normalizer.normalize` — on
purpose: linking a route to a view class requires that class to already
exist as a candidate entity with a real qualified name to look up, and only
Python's own normalizer produces that.

`normalize` (this module's public export) is a drop-in `Normalizer` — same
signature as `adapters.python.normalizer.normalize` — so it can be handed
directly to `IndexingService(..., normalize=hashira.adapters.django.normalize)`
in place of the plain Python one. Nothing in `IndexingService` needs to know
Django enrichment happened; the composition lives entirely here.

Model/view detection reuses the exact identity-claim shape Python's own
entities carry (`QUALIFIED_NAME` + `DECLARATION_ANCHOR`, see
`adapters/_identity_claims.py`) — a Django-produced entity that goes
unchanged across a re-index resolves as `MATCHED`, exactly like a Python one,
not a second-class citizen of the identity ladder.
"""

from __future__ import annotations

from collections.abc import Sequence

from ...core.base import SourceLocation, TechnologyInfo
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


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


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
    `NormalizedRun` instead of deriving one itself -- see
    `adapters.fastapi.normalizer.enrich_normalized_run`'s docstring for why
    this exists. Not part of the `Normalizer` protocol itself; `normalize`
    above is what satisfies that."""
    by_qn: dict[str, Entity] = {
        e.qualified_name: e for e in python_run.entities if e.qualified_name
    }

    _tag_frameworks(observations, by_qn)

    field_entities: dict[tuple[str, str], Entity] = {}
    new_entities: list[Entity] = []
    new_relationships: list[Relationship] = []
    unresolved: list[Observation] = list(python_run.unresolved)

    for obs in observations:
        if obs.kind != "django.model_field":
            continue
        model = by_qn.get(str(obs.payload["model_qualified_name"]))
        if model is None:
            unresolved.append(obs)
            continue
        field_name = str(obs.payload["field_name"])
        field_qn = f"{model.qualified_name}.{field_name}"
        field_entity = Entity(
            system_id=system_id,
            type=EntityType.SYMBOL,
            name=field_name,
            qualified_name=field_qn,
            source=SourceLocation(
                file=model.source.file if model.source else None,
                revision=revision,
                line_start=_int_or_none(obs.payload.get("line")),
            ),
            technology=TechnologyInfo(language="python", framework="django"),
            metadata={
                "kind": "django_model_field",
                "field_type": str(obs.payload["field_type_qualified_name"]).rsplit(".", 1)[-1],
                "field_type_qualified_name": obs.payload["field_type_qualified_name"],
            },
            identity_claims=[
                qualified_name_claim(field_qn),
                declaration_anchor_claim(
                    (model.source.file if model.source else None) or "",
                    field_qn,
                    "django_model_field",
                ),
            ],
            first_seen_revision=revision,
            last_seen_revision=revision,
        )
        new_entities.append(field_entity)
        field_entities[(model.qualified_name, field_name)] = field_entity  # type: ignore[index]
        new_relationships.append(
            Relationship(
                system_id=system_id,
                source_entity_id=model.id,
                target_entity_id=field_entity.id,
                type=RelationshipType.CONTAINS,
                origin=Origin.STATIC_ANALYSIS,
                evidence_ids=list(obs.evidence_ids),
                valid_from_revision=revision,
            )
        )

    for obs in observations:
        if obs.kind != "django.url_route":
            continue
        urls_module_qn = str(obs.payload["urls_module_qualified_name"])
        route = str(obs.payload["route"])
        route_qn = f"{urls_module_qn}:{route}"
        route_entity = Entity(
            system_id=system_id,
            type=EntityType.INTERFACE,
            name=route,
            qualified_name=route_qn,
            source=SourceLocation(
                file=_module_file(by_qn, urls_module_qn),
                revision=revision,
                line_start=_int_or_none(obs.payload.get("line")),
            ),
            technology=TechnologyInfo(language="python", framework="django"),
            metadata={"kind": "django_url_route", "url_name": obs.payload.get("name")},
            identity_claims=[
                qualified_name_claim(route_qn),
                declaration_anchor_claim(
                    _module_file(by_qn, urls_module_qn) or "", route_qn, "django_url_route"
                ),
            ],
            first_seen_revision=revision,
            last_seen_revision=revision,
        )
        new_entities.append(route_entity)

        view_qn = obs.payload.get("resolved_view_qualified_name")
        view = by_qn.get(str(view_qn)) if view_qn else None
        if view is not None:
            new_relationships.append(
                Relationship(
                    system_id=system_id,
                    source_entity_id=route_entity.id,
                    target_entity_id=view.id,
                    type=RelationshipType.EXPOSES,
                    origin=Origin.STATIC_ANALYSIS,
                    evidence_ids=list(obs.evidence_ids),
                    valid_from_revision=revision,
                )
            )
        else:
            unresolved.append(obs)

    for obs in observations:
        if obs.kind != "django.field_access":
            continue
        accessor = by_qn.get(str(obs.payload["accessor_qualified_name"]))
        field = field_entities.get(
            (str(obs.payload["model_qualified_name"]), str(obs.payload["field_name"]))
        )
        if accessor is None or field is None:
            unresolved.append(obs)
            continue
        rel_type = (
            RelationshipType.WRITES
            if obs.payload["access_kind"] == "WRITE"
            else RelationshipType.READS
        )
        new_relationships.append(
            Relationship(
                system_id=system_id,
                source_entity_id=accessor.id,
                target_entity_id=field.id,
                type=rel_type,
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


def _tag_frameworks(observations: Sequence[Observation], by_qn: dict[str, Entity]) -> None:
    """Mark model/view classes with `metadata.framework`/`django_kind` in
    place — same entity, same id, same identity claims; the class is still
    fundamentally "a Python class" (core/base.py's own rule: technology
    detail belongs in metadata, never in a reshaped core envelope).
    """
    for obs in observations:
        if obs.kind == "django.model":
            _tag(by_qn, str(obs.payload["class_qualified_name"]), "model")
        elif obs.kind == "django.view":
            _tag(by_qn, str(obs.payload["class_qualified_name"]), "view")


def _tag(by_qn: dict[str, Entity], qualified_name: str, django_kind: str) -> None:
    entity = by_qn.get(qualified_name)
    if entity is None:
        return
    by_qn[qualified_name] = entity.model_copy(
        update={"metadata": {**entity.metadata, "framework": "django", "django_kind": django_kind}}
    )


def _module_file(by_qn: dict[str, Entity], module_qualified_name: str) -> str | None:
    module = by_qn.get(module_qualified_name)
    return module.source.file if module is not None and module.source is not None else None
