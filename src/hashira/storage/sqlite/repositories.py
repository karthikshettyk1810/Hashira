"""SQLite implementations of every storage port (spec §21, §23, §30).

Each repository holds a live `sqlalchemy.Connection` handed to it by
`SqliteUnitOfWork` — never its own connection, never the engine. That is what
makes the transactional guarantee in §30 possible: every repository used
inside one `with uow:` block shares the same connection and therefore the same
transaction, so graph writes and a snapshot flip commit or roll back together.

Every record is read and written whole, as JSON (see `schema.py` for why).
`_dump`/`_load` are the only two functions that know that.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, TypeVar

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

from ...core.entities import Entity, System
from ...core.enums import EntityType, RelationshipType, SnapshotStatus
from ...core.events import Event
from ...core.evidence import Evidence, Inference, Observation
from ...core.relationships import Relationship
from ...core.snapshots import Snapshot
from . import schema

__all__ = [
    "SqliteEventStore",
    "SqliteEvidenceStore",
    "SqliteGraphRepository",
    "SqliteInferenceStore",
    "SqliteObservationStore",
    "SqliteSnapshotStore",
    "SqliteSystemRepository",
    "SqliteUnitOfWork",
]

_M = TypeVar("_M", bound=BaseModel)

_REVISION_QUERY_UNSUPPORTED = (
    "revision-scoped queries need Git revision ordering, which does not exist "
    "yet (see docs/IR.md#identity-resolution-10--status and ROADMAP.md Phase 2); "
    "omit `revision` and use `at` for wall-clock-in-time queries instead of "
    "returning a silently wrong answer about the past"
)


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _load(model_cls: type[_M], data: Any) -> _M:
    return model_cls.model_validate(data)


class SqliteSystemRepository:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def get(self, system_id: str) -> System | None:
        data = self._conn.execute(
            sa.select(schema.systems.c.data).where(schema.systems.c.id == system_id)
        ).scalar_one_or_none()
        return _load(System, data) if data is not None else None

    def get_by_slug(self, slug: str) -> System | None:
        data = self._conn.execute(
            sa.select(schema.systems.c.data).where(schema.systems.c.slug == slug)
        ).scalar_one_or_none()
        return _load(System, data) if data is not None else None

    def list(self) -> Sequence[System]:
        rows = self._conn.execute(sa.select(schema.systems.c.data)).scalars().all()
        return [_load(System, row) for row in rows]

    def save(self, system: System) -> System:
        stmt = sqlite_insert(schema.systems).values(
            id=system.id, slug=system.slug, data=_dump(system)
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={"slug": stmt.excluded.slug, "data": stmt.excluded.data},
        )
        self._conn.execute(stmt)
        return system


class SqliteGraphRepository:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def get_entity(self, entity_id: str) -> Entity | None:
        data = self._conn.execute(
            sa.select(schema.entities.c.data).where(schema.entities.c.id == entity_id)
        ).scalar_one_or_none()
        return _load(Entity, data) if data is not None else None

    def find_entities(
        self,
        system_id: str,
        *,
        type: EntityType | None = None,
        name: str | None = None,
        qualified_name: str | None = None,
        revision: str | None = None,
        limit: int = 100,
    ) -> Sequence[Entity]:
        if revision is not None:
            raise NotImplementedError(_REVISION_QUERY_UNSUPPORTED)
        col = schema.entities.c
        stmt = sa.select(col.data).where(col.system_id == system_id)
        if type is not None:
            stmt = stmt.where(col.type == type.value)
        if name is not None:
            stmt = stmt.where(col.name == name)
        if qualified_name is not None:
            stmt = stmt.where(col.qualified_name == qualified_name)
        stmt = stmt.limit(limit)
        rows = self._conn.execute(stmt).scalars().all()
        return [_load(Entity, row) for row in rows]

    def upsert_entities(self, entities: Iterable[Entity]) -> Sequence[Entity]:
        saved = list(entities)
        for entity in saved:
            stmt = sqlite_insert(schema.entities).values(
                id=entity.id,
                system_id=entity.system_id,
                type=entity.type.value,
                name=entity.name,
                qualified_name=entity.qualified_name,
                status=entity.status.value,
                data=_dump(entity),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "system_id": stmt.excluded.system_id,
                    "type": stmt.excluded.type,
                    "name": stmt.excluded.name,
                    "qualified_name": stmt.excluded.qualified_name,
                    "status": stmt.excluded.status,
                    "data": stmt.excluded.data,
                },
            )
            self._conn.execute(stmt)
        return saved

    def get_relationships(
        self,
        entity_id: str,
        *,
        direction: str = "out",
        types: Sequence[RelationshipType] | None = None,
        revision: str | None = None,
        at: datetime | None = None,
    ) -> Sequence[Relationship]:
        if revision is not None:
            raise NotImplementedError(_REVISION_QUERY_UNSUPPORTED)
        col = schema.relationships.c
        if direction == "out":
            stmt = sa.select(col.data).where(col.source_entity_id == entity_id)
        elif direction == "in":
            stmt = sa.select(col.data).where(col.target_entity_id == entity_id)
        else:
            stmt = sa.select(col.data).where(
                sa.or_(col.source_entity_id == entity_id, col.target_entity_id == entity_id)
            )
        if types is not None:
            stmt = stmt.where(col.type.in_([t.value for t in types]))
        if at is not None:
            stmt = stmt.where(col.valid_from <= at).where(
                sa.or_(col.valid_until.is_(None), col.valid_until > at)
            )
        rows = self._conn.execute(stmt).scalars().all()
        return [_load(Relationship, row) for row in rows]

    def upsert_relationships(self, relationships: Iterable[Relationship]) -> Sequence[Relationship]:
        saved = list(relationships)
        for rel in saved:
            stmt = sqlite_insert(schema.relationships).values(
                id=rel.id,
                system_id=rel.system_id,
                source_entity_id=rel.source_entity_id,
                target_entity_id=rel.target_entity_id,
                type=rel.type.value,
                is_current=rel.is_current,
                valid_from=rel.valid_from,
                valid_until=rel.valid_until,
                data=_dump(rel),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "system_id": stmt.excluded.system_id,
                    "source_entity_id": stmt.excluded.source_entity_id,
                    "target_entity_id": stmt.excluded.target_entity_id,
                    "type": stmt.excluded.type,
                    "is_current": stmt.excluded.is_current,
                    "valid_from": stmt.excluded.valid_from,
                    "valid_until": stmt.excluded.valid_until,
                    "data": stmt.excluded.data,
                },
            )
            self._conn.execute(stmt)
        return saved

    def close_relationships(self, relationship_ids: Sequence[str], *, revision: str) -> int:
        col = schema.relationships.c
        closed = 0
        for rel_id in relationship_ids:
            data = self._conn.execute(
                sa.select(col.data).where(col.id == rel_id, col.is_current.is_(True))
            ).scalar_one_or_none()
            if data is None:
                continue
            updated = _load(Relationship, data).close(revision=revision)
            self._conn.execute(
                sa.update(schema.relationships)
                .where(col.id == rel_id)
                .values(
                    is_current=updated.is_current,
                    valid_until=updated.valid_until,
                    data=_dump(updated),
                )
            )
            closed += 1
        return closed


class SqliteEventStore:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def append(self, event: Event) -> Event:
        return self.append_many([event])[0]

    def append_many(self, events: Iterable[Event]) -> Sequence[Event]:
        col = schema.events.c
        result: list[Event] = []
        for event in events:
            existing = self._conn.execute(
                sa.select(col.data).where(
                    col.system_id == event.system_id, col.dedupe_key == event.dedupe_key
                )
            ).scalar_one_or_none()
            if existing is not None:
                result.append(_load(Event, existing))
                continue
            self._conn.execute(
                sa.insert(schema.events).values(
                    id=event.id,
                    system_id=event.system_id,
                    type=event.type.value,
                    timestamp=event.timestamp,
                    dedupe_key=event.dedupe_key,
                    data=_dump(event),
                )
            )
            if event.subject_entity_ids:
                self._conn.execute(
                    sa.insert(schema.event_subjects),
                    [
                        {"event_id": event.id, "entity_id": entity_id}
                        for entity_id in event.subject_entity_ids
                    ],
                )
            result.append(event)
        return result

    def find(
        self,
        system_id: str,
        *,
        types: Sequence[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        entity_id: str | None = None,
        limit: int = 100,
    ) -> Sequence[Event]:
        col = schema.events.c
        stmt = sa.select(col.data).where(col.system_id == system_id)
        if types is not None:
            stmt = stmt.where(col.type.in_(list(types)))
        if since is not None:
            stmt = stmt.where(col.timestamp >= since)
        if until is not None:
            stmt = stmt.where(col.timestamp <= until)
        if entity_id is not None:
            stmt = stmt.join(
                schema.event_subjects, schema.event_subjects.c.event_id == col.id
            ).where(schema.event_subjects.c.entity_id == entity_id)
        stmt = stmt.order_by(col.timestamp).limit(limit)
        rows = self._conn.execute(stmt).scalars().all()
        return [_load(Event, row) for row in rows]


class SqliteObservationStore:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def record(self, observations: Iterable[Observation]) -> Sequence[Observation]:
        saved = list(observations)
        if saved:
            stmt = sqlite_insert(schema.observations).on_conflict_do_nothing(index_elements=["id"])
            self._conn.execute(
                stmt,
                [
                    {
                        "id": obs.id,
                        "system_id": obs.system_id,
                        "adapter": obs.adapter,
                        "revision": obs.revision,
                        "data": _dump(obs),
                    }
                    for obs in saved
                ],
            )
        return saved

    def find(
        self, system_id: str, *, adapter: str | None = None, revision: str | None = None
    ) -> Sequence[Observation]:
        col = schema.observations.c
        stmt = sa.select(col.data).where(col.system_id == system_id)
        if adapter is not None:
            stmt = stmt.where(col.adapter == adapter)
        if revision is not None:
            stmt = stmt.where(col.revision == revision)
        rows = self._conn.execute(stmt).scalars().all()
        return [_load(Observation, row) for row in rows]


class SqliteEvidenceStore:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def record(self, evidence: Iterable[Evidence]) -> Sequence[Evidence]:
        saved = list(evidence)
        if saved:
            stmt = sqlite_insert(schema.evidence).on_conflict_do_nothing(index_elements=["id"])
            self._conn.execute(
                stmt,
                [{"id": ev.id, "system_id": ev.system_id, "data": _dump(ev)} for ev in saved],
            )
        return saved

    def get(self, evidence_id: str) -> Evidence | None:
        data = self._conn.execute(
            sa.select(schema.evidence.c.data).where(schema.evidence.c.id == evidence_id)
        ).scalar_one_or_none()
        return _load(Evidence, data) if data is not None else None

    def get_many(self, evidence_ids: Sequence[str]) -> Sequence[Evidence]:
        if not evidence_ids:
            return []
        rows = (
            self._conn.execute(
                sa.select(schema.evidence.c.data).where(
                    schema.evidence.c.id.in_(list(evidence_ids))
                )
            )
            .scalars()
            .all()
        )
        return [_load(Evidence, row) for row in rows]


class SqliteInferenceStore:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def save(self, inference: Inference) -> Inference:
        stmt = sqlite_insert(schema.inferences).values(
            id=inference.id,
            system_id=inference.system_id,
            status=inference.status.value,
            data=_dump(inference),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={"status": stmt.excluded.status, "data": stmt.excluded.data},
        )
        self._conn.execute(stmt)

        self._conn.execute(
            sa.delete(schema.inference_subjects).where(
                schema.inference_subjects.c.inference_id == inference.id
            )
        )
        if inference.subject_entity_ids:
            self._conn.execute(
                sa.insert(schema.inference_subjects),
                [
                    {"inference_id": inference.id, "entity_id": entity_id}
                    for entity_id in inference.subject_entity_ids
                ],
            )
        return inference

    def find(
        self,
        system_id: str,
        *,
        entity_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> Sequence[Inference]:
        col = schema.inferences.c
        stmt = sa.select(col.data).where(col.system_id == system_id)
        if status is not None:
            stmt = stmt.where(col.status == status)
        if entity_id is not None:
            stmt = stmt.join(
                schema.inference_subjects,
                schema.inference_subjects.c.inference_id == col.id,
            ).where(schema.inference_subjects.c.entity_id == entity_id)
        stmt = stmt.limit(limit)
        rows = self._conn.execute(stmt).scalars().all()
        return [_load(Inference, row) for row in rows]


class SqliteSnapshotStore:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def create(self, snapshot: Snapshot) -> Snapshot:
        self._conn.execute(
            sa.insert(schema.snapshots).values(
                id=snapshot.id,
                system_id=snapshot.system_id,
                status=snapshot.status.value,
                created_at=snapshot.created_at,
                data=_dump(snapshot),
            )
        )
        return snapshot

    def update(self, snapshot: Snapshot) -> Snapshot:
        self._conn.execute(
            sa.update(schema.snapshots)
            .where(schema.snapshots.c.id == snapshot.id)
            .values(status=snapshot.status.value, data=_dump(snapshot))
        )
        return snapshot

    def get(self, snapshot_id: str) -> Snapshot | None:
        data = self._conn.execute(
            sa.select(schema.snapshots.c.data).where(schema.snapshots.c.id == snapshot_id)
        ).scalar_one_or_none()
        return _load(Snapshot, data) if data is not None else None

    def latest_complete(self, system_id: str) -> Snapshot | None:
        col = schema.snapshots.c
        data = self._conn.execute(
            sa.select(col.data)
            .where(col.system_id == system_id, col.status == SnapshotStatus.COMPLETE.value)
            .order_by(col.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        return _load(Snapshot, data) if data is not None else None

    def list(self, system_id: str, *, limit: int = 50) -> Sequence[Snapshot]:
        col = schema.snapshots.c
        rows = (
            self._conn.execute(
                sa.select(col.data)
                .where(col.system_id == system_id)
                .order_by(col.created_at.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return [_load(Snapshot, row) for row in rows]


class SqliteUnitOfWork:
    """One indexing transaction (§30).

    Every repository handed out inside a ``with`` block shares one connection
    and one transaction, so graph writes and a snapshot status flip commit or
    roll back as a unit — a failed run cannot corrupt the last known-good
    snapshot, because its writes never became visible in the first place.

    Forgetting to call ``commit()`` is treated as a rollback, not a silent
    success: an indexing bug that exits the ``with`` block without deciding is
    a bug, and this makes it fail safe instead of half-persisting.
    """

    systems: SqliteSystemRepository
    graph: SqliteGraphRepository
    events: SqliteEventStore
    observations: SqliteObservationStore
    evidence: SqliteEvidenceStore
    inferences: SqliteInferenceStore
    snapshots: SqliteSnapshotStore

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._conn: Connection | None = None
        self._trans: sa.engine.RootTransaction | None = None

    def __enter__(self) -> SqliteUnitOfWork:
        self._conn = self._engine.connect()
        self._trans = self._conn.begin()
        self.systems = SqliteSystemRepository(self._conn)
        self.graph = SqliteGraphRepository(self._conn)
        self.events = SqliteEventStore(self._conn)
        self.observations = SqliteObservationStore(self._conn)
        self.evidence = SqliteEvidenceStore(self._conn)
        self.inferences = SqliteInferenceStore(self._conn)
        self.snapshots = SqliteSnapshotStore(self._conn)
        return self

    def __exit__(self, *exc_info: object) -> bool | None:
        assert self._conn is not None and self._trans is not None
        if self._trans.is_active:
            self._trans.rollback()
        self._conn.close()
        self._conn = None
        self._trans = None
        return None

    def commit(self) -> None:
        assert self._trans is not None, "commit() called outside a `with` block"
        self._trans.commit()

    def rollback(self) -> None:
        assert self._trans is not None, "rollback() called outside a `with` block"
        self._trans.rollback()
