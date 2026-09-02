"""The ports must be implementable, not merely declarable.

A Protocol nothing satisfies is a wish. These in-memory doubles are the smallest
honest implementations of each port; the SQLite and PostgreSQL stores land in
Phase 1 and will be held to the same tests.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

import pytest

from hashira.core import (
    Entity,
    EntityType,
    Event,
    EventType,
    Evidence,
    Origin,
    Relationship,
    RelationshipType,
    Snapshot,
    SnapshotStatus,
    SourceRef,
    System,
)
from hashira.ports import (
    AdapterCapabilities,
    EventStore,
    EvidenceStore,
    ExtractionResult,
    GraphRepository,
    IntelligenceProvider,
    SnapshotStore,
    SystemRepository,
)


class InMemorySystems:
    def __init__(self) -> None:
        self._by_id: dict[str, System] = {}

    def get(self, system_id: str) -> System | None:
        return self._by_id.get(system_id)

    def get_by_slug(self, slug: str) -> System | None:
        return next((s for s in self._by_id.values() if s.slug == slug), None)

    def list(self) -> Sequence[System]:
        return list(self._by_id.values())

    def save(self, system: System) -> System:
        self._by_id[system.id] = system
        return system


class InMemoryGraph:
    def __init__(self) -> None:
        self.entities: dict[str, Entity] = {}
        self.relationships: dict[str, Relationship] = {}

    def get_entity(self, entity_id: str) -> Entity | None:
        return self.entities.get(entity_id)

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
        found = [
            entity
            for entity in self.entities.values()
            if entity.system_id == system_id
            and (type is None or entity.type is type)
            and (name is None or entity.name == name)
            and (qualified_name is None or entity.qualified_name == qualified_name)
        ]
        return found[:limit]

    def upsert_entities(self, entities: Iterable[Entity]) -> Sequence[Entity]:
        saved = list(entities)
        for entity in saved:
            self.entities[entity.id] = entity
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
        def matches(edge: Relationship) -> bool:
            if direction == "out" and edge.source_entity_id != entity_id:
                return False
            if direction == "in" and edge.target_entity_id != entity_id:
                return False
            if types is not None and edge.type not in types:
                return False
            return at is None or edge.held_at(at)

        return [edge for edge in self.relationships.values() if matches(edge)]

    def upsert_relationships(self, relationships: Iterable[Relationship]) -> Sequence[Relationship]:
        saved = list(relationships)
        for edge in saved:
            self.relationships[edge.id] = edge
        return saved

    def close_relationships(self, relationship_ids: Sequence[str], *, revision: str) -> int:
        closed = 0
        for rel_id in relationship_ids:
            edge = self.relationships.get(rel_id)
            if edge is not None and edge.is_current:
                self.relationships[rel_id] = edge.close(revision=revision)
                closed += 1
        return closed


class InMemoryEvents:
    def __init__(self) -> None:
        self._by_key: dict[str, Event] = {}

    def append(self, event: Event) -> Event:
        assert event.dedupe_key is not None
        return self._by_key.setdefault(event.dedupe_key, event)

    def append_many(self, events: Iterable[Event]) -> Sequence[Event]:
        return [self.append(event) for event in events]

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
        found = [event for event in self._by_key.values() if event.system_id == system_id]
        return sorted(found, key=lambda event: event.timestamp)[:limit]


class InMemoryEvidence:
    def __init__(self) -> None:
        self._by_id: dict[str, Evidence] = {}

    def record(self, evidence: Iterable[Evidence]) -> Sequence[Evidence]:
        stored = list(evidence)
        for item in stored:
            self._by_id[item.id] = item
        return stored

    def get(self, evidence_id: str) -> Evidence | None:
        return self._by_id.get(evidence_id)

    def get_many(self, evidence_ids: Sequence[str]) -> Sequence[Evidence]:
        return [self._by_id[i] for i in evidence_ids if i in self._by_id]


class InMemorySnapshots:
    def __init__(self) -> None:
        self._by_id: dict[str, Snapshot] = {}

    def create(self, snapshot: Snapshot) -> Snapshot:
        self._by_id[snapshot.id] = snapshot
        return snapshot

    def update(self, snapshot: Snapshot) -> Snapshot:
        self._by_id[snapshot.id] = snapshot
        return snapshot

    def get(self, snapshot_id: str) -> Snapshot | None:
        return self._by_id.get(snapshot_id)

    def latest_complete(self, system_id: str) -> Snapshot | None:
        candidates = [
            snapshot
            for snapshot in self._by_id.values()
            if snapshot.system_id == system_id and snapshot.is_known_good
        ]
        return max(candidates, key=lambda s: s.created_at, default=None)

    def list(self, system_id: str, *, limit: int = 50) -> Sequence[Snapshot]:
        return [s for s in self._by_id.values() if s.system_id == system_id][:limit]


@pytest.mark.contract
@pytest.mark.parametrize(
    ("double", "port"),
    [
        (InMemorySystems(), SystemRepository),
        (InMemoryGraph(), GraphRepository),
        (InMemoryEvents(), EventStore),
        (InMemoryEvidence(), EvidenceStore),
        (InMemorySnapshots(), SnapshotStore),
    ],
    ids=["systems", "graph", "events", "evidence", "snapshots"],
)
def test_port_is_satisfiable(double: object, port: type) -> None:
    assert isinstance(double, port)


@pytest.mark.contract
def test_event_append_is_idempotent(system: System) -> None:
    store = InMemoryEvents()
    make = lambda: Event(  # noqa: E731
        system_id=system.id,
        type=EventType.COMMIT_CREATED,
        source=SourceRef(provider="git", reference="abc123"),
    )
    first = store.append(make())
    second = store.append(make())
    assert first.id == second.id
    assert len(store.find(system.id)) == 1


@pytest.mark.contract
def test_closing_an_edge_is_not_a_delete(
    system: System, payment_service: Entity, razorpay_client: Entity, evidence: Evidence
) -> None:
    graph = InMemoryGraph()
    graph.upsert_entities([payment_service, razorpay_client])
    edge = Relationship(
        system_id=system.id,
        source_entity_id=payment_service.id,
        target_entity_id=razorpay_client.id,
        type=RelationshipType.CALLS,
        origin=Origin.STATIC_ANALYSIS,
        evidence_ids=[evidence.id],
        valid_from_revision="abc123",
    )
    graph.upsert_relationships([edge])

    assert graph.close_relationships([edge.id], revision="def456") == 1
    assert graph.close_relationships([edge.id], revision="def456") == 0, "already closed"

    surviving = graph.get_relationships(payment_service.id)
    assert len(surviving) == 1
    assert not surviving[0].is_current


@pytest.mark.contract
def test_a_failed_snapshot_is_never_served(system: System) -> None:
    store = InMemorySnapshots()
    good = store.create(
        Snapshot(
            system_id=system.id,
            revision="abc123",
            indexing_version="0.1.0",
            ir_version="0.1.0",
            status=SnapshotStatus.COMPLETE,
        )
    )
    store.create(
        Snapshot(
            system_id=system.id,
            revision="def456",
            indexing_version="0.1.0",
            ir_version="0.1.0",
            status=SnapshotStatus.FAILED,
        )
    )
    assert store.latest_complete(system.id) is not None
    assert store.latest_complete(system.id).id == good.id


@pytest.mark.contract
def test_adapter_capabilities_declare_an_ir_range() -> None:
    caps = AdapterCapabilities(
        name="python",
        version="0.1.0",
        ir_versions=["0.1.0"],
        languages=["python"],
        entity_types=[EntityType.MODULE, EntityType.SYMBOL],
        relationship_types=[RelationshipType.CONTAINS, RelationshipType.CALLS],
    )
    assert caps.ir_versions == ["0.1.0"]
    assert not caps.requires_network


@pytest.mark.contract
def test_extraction_results_merge() -> None:
    a = ExtractionResult(errors=["a"])
    b = ExtractionResult(errors=["b"])
    a.extend(b)
    assert a.errors == ["a", "b"]


@pytest.mark.contract
def test_intelligence_provider_is_optional_and_structural() -> None:
    class NullProvider:
        def name(self) -> str:
            return "null"

        def is_available(self) -> bool:
            return False

        def infer(self, request: object) -> Sequence[object]:
            return []

    assert isinstance(NullProvider(), IntelligenceProvider)
