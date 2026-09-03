"""An in-process storage backend: no file, no server, transactional anyway.

This is a real implementation of every storage port, not a test mock — useful
for fast tests, one-shot experiments, and anywhere durability across process
restarts doesn't matter. It is held to exactly the same conformance suite as
`hashira.storage.sqlite.SqliteDatabase` (`tests/contract/uow_conformance.py`),
which is what makes "the ports are a real abstraction, not just a Protocol
nothing enforces" a tested fact rather than an assertion in a docstring.

Transaction isolation is copy-on-write: entering a `with` block snapshots the
current dicts (a shallow copy — safe because every write here *replaces* a
dict entry with a new, immutable-by-convention model rather than mutating one
in place), and only `commit()` publishes the working copy back to the
database. Forgetting to commit, or raising inside the block, leaves the
published state untouched — the same guarantee `SqliteUnitOfWork` gives via a
real transaction, without needing one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from ..core.entities import Entity, System
from ..core.enums import EntityType, RelationshipType
from ..core.events import Event
from ..core.evidence import Evidence, Inference, Observation
from ..core.relationships import Relationship
from ..core.revisions import Revision
from ..core.snapshots import Snapshot

__all__ = ["MemoryDatabase", "MemoryUnitOfWork"]

_REVISION_QUERY_UNSUPPORTED = (
    "this port does not do revision-scoped querying natively; use "
    "hashira.application.history.query_at_revision(uow, ...) instead, which "
    "answers it correctly today by filtering the current graph against "
    "recorded revision ancestry -- see that module's docstring. Omitting "
    "`revision` here or using `at` for wall-clock time both still work."
)


@dataclass
class _Store:
    systems: dict[str, System] = field(default_factory=dict)
    entities: dict[str, Entity] = field(default_factory=dict)
    relationships: dict[str, Relationship] = field(default_factory=dict)
    events: dict[tuple[str, str], Event] = field(default_factory=dict)
    observations: dict[str, Observation] = field(default_factory=dict)
    evidence: dict[str, Evidence] = field(default_factory=dict)
    inferences: dict[str, Inference] = field(default_factory=dict)
    snapshots: dict[str, Snapshot] = field(default_factory=dict)
    revisions: dict[tuple[str, str], Revision] = field(default_factory=dict)

    def copy(self) -> _Store:
        return _Store(
            systems=dict(self.systems),
            entities=dict(self.entities),
            relationships=dict(self.relationships),
            events=dict(self.events),
            observations=dict(self.observations),
            evidence=dict(self.evidence),
            inferences=dict(self.inferences),
            snapshots=dict(self.snapshots),
            revisions=dict(self.revisions),
        )


class _SystemRepository:
    def __init__(self, store: _Store) -> None:
        self._store = store

    def get(self, system_id: str) -> System | None:
        return self._store.systems.get(system_id)

    def get_by_slug(self, slug: str) -> System | None:
        return next((s for s in self._store.systems.values() if s.slug == slug), None)

    def list(self) -> Sequence[System]:
        return list(self._store.systems.values())

    def save(self, system: System) -> System:
        self._store.systems[system.id] = system
        return system


class _GraphRepository:
    def __init__(self, store: _Store) -> None:
        self._store = store

    def get_entity(self, entity_id: str) -> Entity | None:
        return self._store.entities.get(entity_id)

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
        found = [
            entity
            for entity in self._store.entities.values()
            if entity.system_id == system_id
            and (type is None or entity.type is type)
            and (name is None or entity.name == name)
            and (qualified_name is None or entity.qualified_name == qualified_name)
        ]
        return found[:limit]

    def upsert_entities(self, entities: Iterable[Entity]) -> Sequence[Entity]:
        saved = list(entities)
        for entity in saved:
            self._store.entities[entity.id] = entity
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

        def matches(edge: Relationship) -> bool:
            if direction == "out" and edge.source_entity_id != entity_id:
                return False
            if direction == "in" and edge.target_entity_id != entity_id:
                return False
            if direction not in ("out", "in") and entity_id not in (
                edge.source_entity_id,
                edge.target_entity_id,
            ):
                return False
            if types is not None and edge.type not in types:
                return False
            return at is None or edge.held_at(at)

        return [edge for edge in self._store.relationships.values() if matches(edge)]

    def upsert_relationships(self, relationships: Iterable[Relationship]) -> Sequence[Relationship]:
        saved = list(relationships)
        for edge in saved:
            self._store.relationships[edge.id] = edge
        return saved

    def close_relationships(self, relationship_ids: Sequence[str], *, revision: str) -> int:
        closed = 0
        for rel_id in relationship_ids:
            edge = self._store.relationships.get(rel_id)
            if edge is not None and edge.is_current:
                self._store.relationships[rel_id] = edge.close(revision=revision)
                closed += 1
        return closed


class _EventStore:
    def __init__(self, store: _Store) -> None:
        self._store = store

    def append(self, event: Event) -> Event:
        return self.append_many([event])[0]

    def append_many(self, events: Iterable[Event]) -> Sequence[Event]:
        result: list[Event] = []
        for event in events:
            assert event.dedupe_key is not None
            key = (event.system_id, event.dedupe_key)
            existing = self._store.events.get(key)
            if existing is not None:
                result.append(existing)
                continue
            self._store.events[key] = event
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
        found = [event for event in self._store.events.values() if event.system_id == system_id]
        if types is not None:
            found = [event for event in found if event.type.value in types]
        if since is not None:
            found = [event for event in found if event.timestamp >= since]
        if until is not None:
            found = [event for event in found if event.timestamp <= until]
        if entity_id is not None:
            found = [event for event in found if entity_id in event.subject_entity_ids]
        found.sort(key=lambda event: event.timestamp)
        return found[:limit]


class _ObservationStore:
    def __init__(self, store: _Store) -> None:
        self._store = store

    def record(self, observations: Iterable[Observation]) -> Sequence[Observation]:
        saved = list(observations)
        for obs in saved:
            self._store.observations.setdefault(obs.id, obs)
        return saved

    def find(
        self, system_id: str, *, adapter: str | None = None, revision: str | None = None
    ) -> Sequence[Observation]:
        found = [obs for obs in self._store.observations.values() if obs.system_id == system_id]
        if adapter is not None:
            found = [obs for obs in found if obs.adapter == adapter]
        if revision is not None:
            found = [obs for obs in found if obs.revision == revision]
        return found


class _EvidenceStore:
    def __init__(self, store: _Store) -> None:
        self._store = store

    def record(self, evidence: Iterable[Evidence]) -> Sequence[Evidence]:
        saved = list(evidence)
        for item in saved:
            self._store.evidence.setdefault(item.id, item)
        return saved

    def get(self, evidence_id: str) -> Evidence | None:
        return self._store.evidence.get(evidence_id)

    def get_many(self, evidence_ids: Sequence[str]) -> Sequence[Evidence]:
        return [self._store.evidence[i] for i in evidence_ids if i in self._store.evidence]


class _InferenceStore:
    def __init__(self, store: _Store) -> None:
        self._store = store

    def save(self, inference: Inference) -> Inference:
        self._store.inferences[inference.id] = inference
        return inference

    def find(
        self,
        system_id: str,
        *,
        entity_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> Sequence[Inference]:
        found = [inf for inf in self._store.inferences.values() if inf.system_id == system_id]
        if status is not None:
            found = [inf for inf in found if inf.status.value == status]
        if entity_id is not None:
            found = [inf for inf in found if entity_id in inf.subject_entity_ids]
        return found[:limit]


class _SnapshotStore:
    def __init__(self, store: _Store) -> None:
        self._store = store

    def create(self, snapshot: Snapshot) -> Snapshot:
        self._store.snapshots[snapshot.id] = snapshot
        return snapshot

    def update(self, snapshot: Snapshot) -> Snapshot:
        self._store.snapshots[snapshot.id] = snapshot
        return snapshot

    def get(self, snapshot_id: str) -> Snapshot | None:
        return self._store.snapshots.get(snapshot_id)

    def latest_complete(self, system_id: str) -> Snapshot | None:
        candidates = [
            snapshot
            for snapshot in self._store.snapshots.values()
            if snapshot.system_id == system_id and snapshot.is_known_good
        ]
        return max(candidates, key=lambda snapshot: snapshot.created_at, default=None)

    def list(self, system_id: str, *, limit: int = 50) -> Sequence[Snapshot]:
        found = [
            snapshot
            for snapshot in self._store.snapshots.values()
            if snapshot.system_id == system_id
        ]
        found.sort(key=lambda snapshot: snapshot.created_at, reverse=True)
        return found[:limit]


class _RevisionStore:
    def __init__(self, store: _Store) -> None:
        self._store = store

    def record(self, revisions: Iterable[Revision]) -> Sequence[Revision]:
        saved = list(revisions)
        for revision in saved:
            self._store.revisions.setdefault((revision.system_id, revision.sha), revision)
        return saved

    def get(self, system_id: str, sha: str) -> Revision | None:
        return self._store.revisions.get((system_id, sha))

    def find(self, system_id: str, *, limit: int = 100_000) -> Sequence[Revision]:
        found = [r for (sid, _sha), r in self._store.revisions.items() if sid == system_id]
        return found[:limit]


class MemoryUnitOfWork:
    """One transaction over a `MemoryDatabase`. See module docstring for the
    copy-on-write isolation this relies on."""

    systems: _SystemRepository
    graph: _GraphRepository
    events: _EventStore
    observations: _ObservationStore
    evidence: _EvidenceStore
    inferences: _InferenceStore
    snapshots: _SnapshotStore
    revisions: _RevisionStore

    def __init__(self, database: MemoryDatabase) -> None:
        self._database = database
        self._working: _Store | None = None

    def __enter__(self) -> MemoryUnitOfWork:
        self._working = self._database._read().copy()
        self.systems = _SystemRepository(self._working)
        self.graph = _GraphRepository(self._working)
        self.events = _EventStore(self._working)
        self.observations = _ObservationStore(self._working)
        self.evidence = _EvidenceStore(self._working)
        self.inferences = _InferenceStore(self._working)
        self.snapshots = _SnapshotStore(self._working)
        self.revisions = _RevisionStore(self._working)
        return self

    def __exit__(self, *exc_info: object) -> bool | None:
        self._working = None
        return None

    def commit(self) -> None:
        assert self._working is not None, "commit() called outside a `with` block"
        self._database._write(self._working)

    def rollback(self) -> None:
        assert self._working is not None, "rollback() called outside a `with` block"
        self._working = self._database._read().copy()
        self.systems = _SystemRepository(self._working)
        self.graph = _GraphRepository(self._working)
        self.events = _EventStore(self._working)
        self.observations = _ObservationStore(self._working)
        self.evidence = _EvidenceStore(self._working)
        self.inferences = _InferenceStore(self._working)
        self.snapshots = _SnapshotStore(self._working)
        self.revisions = _RevisionStore(self._working)


class MemoryDatabase:
    """Owns one in-process store and hands out a fresh `MemoryUnitOfWork` per
    transaction, mirroring `hashira.storage.sqlite.SqliteDatabase`'s shape."""

    def __init__(self) -> None:
        self._store = _Store()

    def _read(self) -> _Store:
        return self._store

    def _write(self, store: _Store) -> None:
        self._store = store

    def unit_of_work(self) -> MemoryUnitOfWork:
        return MemoryUnitOfWork(self)
