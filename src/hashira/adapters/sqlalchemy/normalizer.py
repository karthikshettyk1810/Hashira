"""Stage 2 for SQLAlchemy: `sqlalchemy.*` observations + Python's own
normalized entities -> a cross-domain graph.

Runs *after* `adapters.python.normalizer.normalize`, exactly like Django's
and FastAPI's own normalizers and for the same reason: linking a foreign key
to the column it targets requires that column to already exist as a
candidate entity with a real qualified name to look up.

`normalize` (this module's public export) is a drop-in `Normalizer` -- same
signature as every other adapter's -- so it can be handed directly to
`IndexingService(..., normalize=hashira.adapters.sqlalchemy.normalize)`, or
composed with a framework normalizer (see
`tests/integration/test_fastapi_sqlalchemy_together.py` for how a caller
layers both without either adapter knowing the other exists).

**The one place this milestone's design discussion asked for two linked
entities instead of one tagged entity.** Every other enricher in this
codebase tags an existing Python entity in place (a Django view, a FastAPI
handler) because the framework construct *is* that Python construct. An ORM
class and the table it persists to are not the same conceptual thing -- one
is source structure, the other is a runtime/data structure with its own
independent identity (its name survives a class rename; a schema migration
can add a column no Python attribute mirrors yet). So the model class stays
tagged (`metadata.framework`/`sqlalchemy_kind`, the same rule as everywhere
else), *and* a fresh `EntityType.DATA_ENTITY` is minted for the table,
connected by `RelationshipType.MAPS_TO` -- a relationship added specifically
for this (see `core/enums.py`'s `RelationshipType` docstring for why no
existing type honestly covered it).

Columns are `EntityType.SYMBOL` entities `CONTAINS`-related to their table,
mirroring Django's model-field pattern. A `ForeignKey("table.column")`
argument becomes `RelationshipType.REFERENCES` between two column entities
-- resolved by a direct qualified-name lookup, since a column's qualified
name (`"table.column"`) and a `ForeignKey` string argument are the same
format by construction (`adapter.py`'s `sqlalchemy.column` payload already
carries the raw target string; no extra parsing happens here).
"""

from __future__ import annotations

from collections.abc import Sequence

from ...core.base import TechnologyInfo
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
    `NormalizedRun` instead of deriving one itself -- see
    `adapters.fastapi.normalizer.enrich_normalized_run`'s docstring for why
    this exists (the composability story is identical here). Not part of
    the `Normalizer` protocol itself; `normalize` above is what satisfies
    that."""
    by_qn: dict[str, Entity] = {
        e.qualified_name: e for e in python_run.entities if e.qualified_name
    }

    new_entities: list[Entity] = []
    new_relationships: list[Relationship] = []
    unresolved: list[Observation] = list(python_run.unresolved)

    tables: dict[str, Entity] = {}  # table_name -> DATA_ENTITY
    for obs in observations:
        if obs.kind != "sqlalchemy.model":
            continue
        class_qn = str(obs.payload["class_qualified_name"])
        model = by_qn.get(class_qn)
        if model is None:
            unresolved.append(obs)
            continue

        model = model.model_copy(
            update={
                "metadata": {
                    **model.metadata,
                    "framework": "sqlalchemy",
                    "sqlalchemy_kind": "model",
                }
            }
        )
        by_qn[class_qn] = model

        table_name = str(obs.payload["table_name"])
        table_entity = Entity(
            system_id=system_id,
            type=EntityType.DATA_ENTITY,
            name=table_name,
            qualified_name=table_name,
            source=model.source,
            technology=TechnologyInfo(language="sql", framework="sqlalchemy"),
            metadata={"kind": "sqlalchemy_table", "mapped_class_qualified_name": class_qn},
            identity_claims=[
                qualified_name_claim(table_name),
                declaration_anchor_claim(
                    (model.source.file if model.source else None) or "",
                    table_name,
                    "sqlalchemy_table",
                ),
            ],
            first_seen_revision=revision,
            last_seen_revision=revision,
        )
        new_entities.append(table_entity)
        tables[table_name] = table_entity
        new_relationships.append(
            Relationship(
                system_id=system_id,
                source_entity_id=model.id,
                target_entity_id=table_entity.id,
                type=RelationshipType.MAPS_TO,
                origin=Origin.STATIC_ANALYSIS,
                evidence_ids=list(obs.evidence_ids),
                valid_from_revision=revision,
            )
        )

    columns: dict[str, Entity] = {}  # "table.column" -> SYMBOL
    pending_foreign_keys: list[tuple[Observation, str, str]] = []
    for obs in observations:
        if obs.kind != "sqlalchemy.column":
            continue
        table_name = str(obs.payload["table_name"])
        owning_table = tables.get(table_name)
        if owning_table is None:
            unresolved.append(obs)
            continue

        field_name = str(obs.payload["field_name"])
        column_qn = f"{table_name}.{field_name}"
        column_entity = Entity(
            system_id=system_id,
            type=EntityType.SYMBOL,
            name=field_name,
            qualified_name=column_qn,
            source=owning_table.source,
            technology=TechnologyInfo(language="sql", framework="sqlalchemy"),
            metadata={
                "kind": "sqlalchemy_column",
                "is_primary_key": bool(obs.payload.get("is_primary_key")),
                "column_type": obs.payload.get("column_type_text"),
            },
            identity_claims=[
                qualified_name_claim(column_qn),
                declaration_anchor_claim(
                    (owning_table.source.file if owning_table.source else None) or "",
                    column_qn,
                    "sqlalchemy_column",
                ),
            ],
            first_seen_revision=revision,
            last_seen_revision=revision,
        )
        new_entities.append(column_entity)
        columns[column_qn] = column_entity
        new_relationships.append(
            Relationship(
                system_id=system_id,
                source_entity_id=owning_table.id,
                target_entity_id=column_entity.id,
                type=RelationshipType.CONTAINS,
                origin=Origin.STATIC_ANALYSIS,
                evidence_ids=list(obs.evidence_ids),
                valid_from_revision=revision,
            )
        )

        fk_target = obs.payload.get("foreign_key_target")
        if fk_target:
            pending_foreign_keys.append((obs, column_qn, str(fk_target)))

    for obs, source_column_qn, target_column_qn in pending_foreign_keys:
        source_column = columns.get(source_column_qn)
        target_column = columns.get(target_column_qn)
        if source_column is None or target_column is None:
            unresolved.append(obs)
            continue
        new_relationships.append(
            Relationship(
                system_id=system_id,
                source_entity_id=source_column.id,
                target_entity_id=target_column.id,
                type=RelationshipType.REFERENCES,
                origin=Origin.STATIC_ANALYSIS,
                evidence_ids=list(obs.evidence_ids),
                valid_from_revision=revision,
            )
        )

    for obs in observations:
        if obs.kind != "sqlalchemy.field_access":
            continue
        accessor = by_qn.get(str(obs.payload["accessor_qualified_name"]))
        column = columns.get(str(obs.payload["column_qualified_name"]))
        if accessor is None or column is None:
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
                target_entity_id=column.id,
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
