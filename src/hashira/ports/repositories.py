"""Storage ports (spec §23).

These Protocols are the entire vocabulary the application layer has for talking
to persistence. SQLite is the local-first default and PostgreSQL the hosted
store; neither may leak a session, a cursor or a dialect above this line.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from ..core.entities import Entity, System
from ..core.enums import EntityType, RelationshipType
from ..core.events import Event
from ..core.evidence import Evidence, Inference, Observation
from ..core.relationships import Relationship
from ..core.snapshots import Snapshot


@runtime_checkable
class SystemRepository(Protocol):
    """Systems and their configuration."""

    def get(self, system_id: str) -> System | None: ...

    def get_by_slug(self, slug: str) -> System | None: ...

    def list(self) -> Sequence[System]: ...

    def save(self, system: System) -> System: ...


@runtime_checkable
class GraphRepository(Protocol):
    """Entities and relationships.

    Every read takes an optional revision. Omitting it means "as currently
    believed"; supplying one means "as of that revision" (§13). A store that
    cannot answer historically must raise rather than silently return current
    state — a wrong answer about the past is worse than no answer.
    """

    def get_entity(self, entity_id: str) -> Entity | None: ...

    def find_entities(
        self,
        system_id: str,
        *,
        type: EntityType | None = None,
        name: str | None = None,
        qualified_name: str | None = None,
        revision: str | None = None,
        limit: int = 100,
    ) -> Sequence[Entity]: ...

    def upsert_entities(self, entities: Iterable[Entity]) -> Sequence[Entity]: ...

    def get_relationships(
        self,
        entity_id: str,
        *,
        direction: str = "out",
        types: Sequence[RelationshipType] | None = None,
        revision: str | None = None,
        at: datetime | None = None,
    ) -> Sequence[Relationship]: ...

    def upsert_relationships(
        self, relationships: Iterable[Relationship]
    ) -> Sequence[Relationship]: ...

    def close_relationships(self, relationship_ids: Sequence[str], *, revision: str) -> int:
        """Mark edges no longer observed. Never deletes: history stays readable."""
        ...


@runtime_checkable
class EventStore(Protocol):
    """Append-only history.

    ``append`` must be idempotent on ``Event.dedupe_key`` (§30): a replayed
    webhook returns the existing event rather than forking history.
    """

    def append(self, event: Event) -> Event: ...

    def append_many(self, events: Iterable[Event]) -> Sequence[Event]: ...

    def find(
        self,
        system_id: str,
        *,
        types: Sequence[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        entity_id: str | None = None,
        limit: int = 100,
    ) -> Sequence[Event]: ...


@runtime_checkable
class ObservationStore(Protocol):
    """Raw normalized facts, kept so indexing stays auditable and replayable."""

    def record(self, observations: Iterable[Observation]) -> Sequence[Observation]: ...

    def find(
        self, system_id: str, *, adapter: str | None = None, revision: str | None = None
    ) -> Sequence[Observation]: ...


@runtime_checkable
class EvidenceStore(Protocol):
    """Immutable evidence. There is deliberately no update method."""

    def record(self, evidence: Iterable[Evidence]) -> Sequence[Evidence]: ...

    def get(self, evidence_id: str) -> Evidence | None: ...

    def get_many(self, evidence_ids: Sequence[str]) -> Sequence[Evidence]: ...


@runtime_checkable
class InferenceStore(Protocol):
    """Derived and probabilistic claims, whose status changes over time."""

    def save(self, inference: Inference) -> Inference: ...

    def find(
        self,
        system_id: str,
        *,
        entity_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> Sequence[Inference]: ...


@runtime_checkable
class SnapshotStore(Protocol):
    """Named cut points, and the last-known-good pointer queries resolve against."""

    def create(self, snapshot: Snapshot) -> Snapshot: ...

    def update(self, snapshot: Snapshot) -> Snapshot: ...

    def get(self, snapshot_id: str) -> Snapshot | None: ...

    def latest_complete(self, system_id: str) -> Snapshot | None:
        """The newest snapshot safe to serve. A failed run is never the answer."""
        ...

    def list(self, system_id: str, *, limit: int = 50) -> Sequence[Snapshot]: ...


@runtime_checkable
class UnitOfWork(Protocol):
    """Transactional boundary for an indexing run (§30).

    A failed run must leave the last known-good snapshot intact, which means
    graph writes and the snapshot flip commit or roll back together.
    """

    systems: SystemRepository
    graph: GraphRepository
    events: EventStore
    observations: ObservationStore
    evidence: EvidenceStore
    inferences: InferenceStore
    snapshots: SnapshotStore

    def __enter__(self) -> UnitOfWork: ...

    def __exit__(self, *exc_info: object) -> bool | None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...
