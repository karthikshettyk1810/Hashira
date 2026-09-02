"""Shared port-conformance tests for any `UnitOfWork` implementation.

Not collected directly — import with ``from tests.contract.uow_conformance
import *`` into a test module that defines its own ``uow_factory`` fixture: a
zero-argument callable that returns a fresh, *unentered* `UnitOfWork` bound to
the same underlying storage across calls (so writes committed in one
transaction are visible to the next). pytest resolves each imported test
function's fixtures from the importing file, so the same test bodies run
against every backend registered this way — currently
`hashira.storage.memory.MemoryDatabase` (`tests/contract/test_memory_conformance.py`)
and `hashira.storage.sqlite.SqliteDatabase`
(`tests/integration/test_sqlite_storage.py`).

That is the point of this module: "the storage ports are a real abstraction"
is provable by running one set of tests against two backends, not something
the architecture docs merely assert.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from hashira.core import (
    Actor,
    ActorType,
    Confidence,
    Entity,
    EntityStatus,
    EntityType,
    Event,
    EventType,
    Evidence,
    Inference,
    InferenceStatus,
    KnowledgeClass,
    Origin,
    Relationship,
    RelationshipType,
    Snapshot,
    SnapshotStatus,
    SourceRef,
    System,
)
from hashira.ports import UnitOfWork

UowFactory = Callable[[], UnitOfWork]


# --- systems -------------------------------------------------------------


def test_system_save_and_get_round_trip(uow_factory: UowFactory) -> None:
    system = System(name="Food Platform", slug="food-platform")
    with uow_factory() as uow:
        uow.systems.save(system)
        uow.commit()

    with uow_factory() as uow:
        assert uow.systems.get(system.id) == system
        assert uow.systems.get_by_slug("food-platform") == system
        assert uow.systems.get("sys_does_not_exist") is None


def test_system_list_returns_every_saved_system(uow_factory: UowFactory) -> None:
    a = System(name="A", slug="a")
    b = System(name="B", slug="b")
    with uow_factory() as uow:
        uow.systems.save(a)
        uow.systems.save(b)
        uow.commit()

    with uow_factory() as uow:
        assert {s.id for s in uow.systems.list()} >= {a.id, b.id}


# --- entities --------------------------------------------------------------


def test_entity_upsert_and_find_by_type_name_qualified_name(uow_factory: UowFactory) -> None:
    system = System(name="s", slug="s-entities")
    entity = Entity(
        system_id=system.id,
        type=EntityType.SYMBOL,
        name="process",
        qualified_name="payments.services.PaymentService.process",
    )
    with uow_factory() as uow:
        uow.systems.save(system)
        uow.graph.upsert_entities([entity])
        uow.commit()

    with uow_factory() as uow:
        assert uow.graph.get_entity(entity.id) == entity
        assert [e.id for e in uow.graph.find_entities(system.id, type=EntityType.SYMBOL)] == [
            entity.id
        ]
        assert [e.id for e in uow.graph.find_entities(system.id, name="process")] == [entity.id]
        assert [
            e.id
            for e in uow.graph.find_entities(
                system.id, qualified_name="payments.services.PaymentService.process"
            )
        ] == [entity.id]
        assert uow.graph.find_entities(system.id, type=EntityType.DATA_ENTITY) == []


def test_entity_upsert_is_an_update_not_a_duplicate(uow_factory: UowFactory) -> None:
    system = System(name="s", slug="s-upsert")
    entity = Entity(system_id=system.id, type=EntityType.SYMBOL, name="process")
    with uow_factory() as uow:
        uow.systems.save(system)
        uow.graph.upsert_entities([entity])
        uow.commit()

    renamed = entity.model_copy(update={"name": "handle"})
    with uow_factory() as uow:
        uow.graph.upsert_entities([renamed])
        uow.commit()

    with uow_factory() as uow:
        found = uow.graph.find_entities(system.id, type=EntityType.SYMBOL)
        assert len(found) == 1
        assert found[0].name == "handle"


def test_find_entities_rejects_revision_scoped_queries(uow_factory: UowFactory) -> None:
    """§13 says historical queries must fail loudly rather than silently
    return current state; revision ordering does not exist yet (docs/IR.md)."""
    system = System(name="s", slug="s-revision-entities")
    with uow_factory() as uow:
        uow.systems.save(system)
        uow.commit()
        with pytest.raises(NotImplementedError):
            uow.graph.find_entities(system.id, revision="abc123")


# --- relationships -----------------------------------------------------------


def _system_and_pair(uow_factory: UowFactory, slug: str) -> tuple[System, Entity, Entity]:
    system = System(name="s", slug=slug)
    a = Entity(system_id=system.id, type=EntityType.SYMBOL, name="PaymentService")
    b = Entity(system_id=system.id, type=EntityType.EXTERNAL_SYSTEM, name="RazorpayClient")
    with uow_factory() as uow:
        uow.systems.save(system)
        uow.graph.upsert_entities([a, b])
        uow.commit()
    return system, a, b


def _evidence_for(system: System) -> Evidence:
    return Evidence(
        system_id=system.id,
        origin=Origin.STATIC_ANALYSIS,
        source=SourceRef(provider="python-ast", reference="payments/services.py"),
        summary="PaymentService calls RazorpayClient",
    )


def test_relationship_upsert_and_direction_filters(uow_factory: UowFactory) -> None:
    system, a, b = _system_and_pair(uow_factory, "s-rel-direction")
    ev = _evidence_for(system)
    edge = Relationship(
        system_id=system.id,
        source_entity_id=a.id,
        target_entity_id=b.id,
        type=RelationshipType.CALLS,
        origin=Origin.STATIC_ANALYSIS,
        evidence_ids=[ev.id],
    )
    with uow_factory() as uow:
        uow.evidence.record([ev])
        uow.graph.upsert_relationships([edge])
        uow.commit()

    with uow_factory() as uow:
        assert [r.id for r in uow.graph.get_relationships(a.id, direction="out")] == [edge.id]
        assert uow.graph.get_relationships(a.id, direction="in") == []
        assert [r.id for r in uow.graph.get_relationships(b.id, direction="in")] == [edge.id]
        assert [r.id for r in uow.graph.get_relationships(a.id, direction="both")] == [edge.id]
        assert [
            r.id for r in uow.graph.get_relationships(a.id, types=[RelationshipType.CALLS])
        ] == [edge.id]
        assert uow.graph.get_relationships(a.id, types=[RelationshipType.WRITES]) == []


def test_close_relationships_is_not_a_delete(uow_factory: UowFactory) -> None:
    system, a, b = _system_and_pair(uow_factory, "s-rel-close")
    ev = _evidence_for(system)
    edge = Relationship(
        system_id=system.id,
        source_entity_id=a.id,
        target_entity_id=b.id,
        type=RelationshipType.CALLS,
        origin=Origin.STATIC_ANALYSIS,
        evidence_ids=[ev.id],
        valid_from_revision="abc123",
    )
    with uow_factory() as uow:
        uow.evidence.record([ev])
        uow.graph.upsert_relationships([edge])
        uow.commit()

    with uow_factory() as uow:
        assert uow.graph.close_relationships([edge.id], revision="def456") == 1
        assert uow.graph.close_relationships([edge.id], revision="def456") == 0, "already closed"
        uow.commit()

    with uow_factory() as uow:
        surviving = uow.graph.get_relationships(a.id)
        assert len(surviving) == 1, "closing an edge must not delete its row"
        assert not surviving[0].is_current
        assert surviving[0].valid_until_revision == "def456"


def test_get_relationships_at_filters_by_wall_clock_validity(uow_factory: UowFactory) -> None:
    system, a, b = _system_and_pair(uow_factory, "s-rel-at")
    ev = _evidence_for(system)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    edge = Relationship(
        system_id=system.id,
        source_entity_id=a.id,
        target_entity_id=b.id,
        type=RelationshipType.CALLS,
        origin=Origin.STATIC_ANALYSIS,
        evidence_ids=[ev.id],
        valid_from=start,
    ).close(at=start + timedelta(days=30))
    with uow_factory() as uow:
        uow.evidence.record([ev])
        uow.graph.upsert_relationships([edge])
        uow.commit()

    with uow_factory() as uow:
        assert uow.graph.get_relationships(a.id, at=start + timedelta(days=1)) != []
        assert uow.graph.get_relationships(a.id, at=start + timedelta(days=60)) == []


def test_get_relationships_rejects_revision_scoped_queries(uow_factory: UowFactory) -> None:
    _system, a, _b = _system_and_pair(uow_factory, "s-rel-revision")
    with uow_factory() as uow, pytest.raises(NotImplementedError):
        uow.graph.get_relationships(a.id, revision="abc123")


# --- events ------------------------------------------------------------------


def _commit_event(system: System, sha: str, *, subject: str | None = None) -> Event:
    return Event(
        system_id=system.id,
        type=EventType.COMMIT_CREATED,
        actor=Actor(type=ActorType.DEVELOPER, id="dev_1"),
        source=SourceRef(provider="git", reference=sha),
        subject_entity_ids=[subject] if subject else [],
    )


def test_event_append_is_idempotent_across_transactions(uow_factory: UowFactory) -> None:
    system = System(name="s", slug="s-event-idempotent")
    with uow_factory() as uow:
        uow.systems.save(system)
        uow.commit()

    with uow_factory() as uow:
        first = uow.events.append(_commit_event(system, "abc123"))
        uow.commit()

    with uow_factory() as uow:
        second = uow.events.append(_commit_event(system, "abc123"))
        uow.commit()

    assert first.id == second.id
    with uow_factory() as uow:
        assert len(uow.events.find(system.id)) == 1


def test_event_find_filters_by_type_time_and_entity(uow_factory: UowFactory) -> None:
    system, a, _b = _system_and_pair(uow_factory, "s-event-filters")
    early = _commit_event(system, "rev1", subject=a.id)
    late = _commit_event(system, "rev2")
    with uow_factory() as uow:
        uow.events.append_many([early, late])
        uow.commit()

    with uow_factory() as uow:
        found = uow.events.find(system.id, entity_id=a.id)
        assert [e.id for e in found] == [early.id]
        assert len(uow.events.find(system.id, types=["COMMIT_CREATED"])) == 2
        assert uow.events.find(system.id, since=datetime(2999, 1, 1, tzinfo=UTC)) == []


# --- observations --------------------------------------------------------


def test_observation_record_and_find_and_idempotency(uow_factory: UowFactory) -> None:
    from hashira.core import Observation

    system = System(name="s", slug="s-observations")
    obs = Observation(
        system_id=system.id,
        adapter="python@0.1.0",
        origin=Origin.STATIC_ANALYSIS,
        kind="python.function",
    )
    with uow_factory() as uow:
        uow.systems.save(system)
        uow.observations.record([obs])
        uow.observations.record([obs])  # re-recording the same id must not duplicate or error
        uow.commit()

    with uow_factory() as uow:
        found = uow.observations.find(system.id, adapter="python@0.1.0")
        assert [o.id for o in found] == [obs.id]


# --- evidence ------------------------------------------------------------


def test_evidence_record_get_and_get_many(uow_factory: UowFactory) -> None:
    system = System(name="s", slug="s-evidence")
    ev = _evidence_for(system)
    with uow_factory() as uow:
        uow.systems.save(system)
        uow.evidence.record([ev])
        uow.commit()

    with uow_factory() as uow:
        assert uow.evidence.get(ev.id) == ev
        assert uow.evidence.get("ev_does_not_exist") is None
        assert [e.id for e in uow.evidence.get_many([ev.id, "ev_missing"])] == [ev.id]
        assert uow.evidence.get_many([]) == []


# --- inferences ------------------------------------------------------------


def test_inference_save_and_find_by_status_and_entity(uow_factory: UowFactory) -> None:
    system, a, _b = _system_and_pair(uow_factory, "s-inference")
    inference = Inference(
        system_id=system.id,
        knowledge_class=KnowledgeClass.HYPOTHESIS,
        statement="PaymentService may be involved",
        subject_entity_ids=[a.id],
        origin=Origin.DERIVED,
        confidence=Confidence.SPECULATIVE,
    )
    with uow_factory() as uow:
        uow.inferences.save(inference)
        uow.commit()

    with uow_factory() as uow:
        assert [i.id for i in uow.inferences.find(system.id, entity_id=a.id)] == [inference.id]
        assert [
            i.id for i in uow.inferences.find(system.id, status=InferenceStatus.PROPOSED.value)
        ] == [inference.id]
        assert uow.inferences.find(system.id, status=InferenceStatus.CONFIRMED.value) == []


def test_inference_resave_replaces_its_subjects(uow_factory: UowFactory) -> None:
    """A re-saved inference's old subject links must not linger as ghosts."""
    system, a, b = _system_and_pair(uow_factory, "s-inference-resave")
    inference = Inference(
        system_id=system.id,
        knowledge_class=KnowledgeClass.HYPOTHESIS,
        statement="maybe A",
        subject_entity_ids=[a.id],
        origin=Origin.DERIVED,
        confidence=Confidence.SPECULATIVE,
    )
    with uow_factory() as uow:
        uow.inferences.save(inference)
        uow.commit()

    updated = inference.model_copy(update={"subject_entity_ids": [b.id]})
    with uow_factory() as uow:
        uow.inferences.save(updated)
        uow.commit()

    with uow_factory() as uow:
        assert uow.inferences.find(system.id, entity_id=a.id) == []
        assert [i.id for i in uow.inferences.find(system.id, entity_id=b.id)] == [inference.id]


# --- snapshots -------------------------------------------------------------


def _snapshot(system: System, revision: str, status: SnapshotStatus, *, at: datetime) -> Snapshot:
    return Snapshot(
        system_id=system.id,
        revision=revision,
        indexing_version="0.1.0",
        ir_version="0.1.1",
        status=status,
        created_at=at,
    )


def test_snapshot_create_get_update(uow_factory: UowFactory) -> None:
    system = System(name="s", slug="s-snapshot-crud")
    snap = _snapshot(system, "abc123", SnapshotStatus.BUILDING, at=datetime(2026, 1, 1, tzinfo=UTC))
    with uow_factory() as uow:
        uow.systems.save(system)
        uow.snapshots.create(snap)
        uow.commit()

    with uow_factory() as uow:
        assert uow.snapshots.get(snap.id) is not None
        assert uow.snapshots.get(snap.id).status is SnapshotStatus.BUILDING  # type: ignore[union-attr]

    completed = snap.model_copy(update={"status": SnapshotStatus.COMPLETE})
    with uow_factory() as uow:
        uow.snapshots.update(completed)
        uow.commit()

    with uow_factory() as uow:
        got = uow.snapshots.get(snap.id)
        assert got is not None and got.status is SnapshotStatus.COMPLETE


def test_latest_complete_ignores_failed_snapshots(uow_factory: UowFactory) -> None:
    system = System(name="s", slug="s-snapshot-latest")
    good = _snapshot(system, "abc123", SnapshotStatus.COMPLETE, at=datetime(2026, 1, 1, tzinfo=UTC))
    failed = _snapshot(system, "def456", SnapshotStatus.FAILED, at=datetime(2026, 1, 2, tzinfo=UTC))
    with uow_factory() as uow:
        uow.systems.save(system)
        uow.snapshots.create(good)
        uow.snapshots.create(failed)
        uow.commit()

    with uow_factory() as uow:
        latest = uow.snapshots.latest_complete(system.id)
        assert latest is not None
        assert latest.id == good.id


def test_snapshot_list_orders_newest_first(uow_factory: UowFactory) -> None:
    system = System(name="s", slug="s-snapshot-list")
    older = _snapshot(system, "r1", SnapshotStatus.COMPLETE, at=datetime(2026, 1, 1, tzinfo=UTC))
    newer = _snapshot(system, "r2", SnapshotStatus.COMPLETE, at=datetime(2026, 1, 2, tzinfo=UTC))
    with uow_factory() as uow:
        uow.systems.save(system)
        uow.snapshots.create(older)
        uow.snapshots.create(newer)
        uow.commit()

    with uow_factory() as uow:
        listed = uow.snapshots.list(system.id)
        assert [s.id for s in listed] == [newer.id, older.id]


# --- the §30 guarantee: a failed run cannot corrupt the last known-good state ---


def test_uncommitted_writes_are_invisible_to_a_new_transaction(uow_factory: UowFactory) -> None:
    system = System(name="s", slug="s-uncommitted")
    with uow_factory() as uow:
        uow.systems.save(system)
        # deliberately no commit()

    with uow_factory() as uow:
        assert uow.systems.get(system.id) is None


def test_explicit_rollback_discards_writes(uow_factory: UowFactory) -> None:
    system = System(name="s", slug="s-rollback")
    with uow_factory() as uow:
        uow.systems.save(system)
        uow.rollback()

    with uow_factory() as uow:
        assert uow.systems.get(system.id) is None


def test_exception_inside_the_block_rolls_back(uow_factory: UowFactory) -> None:
    system = System(name="s", slug="s-exception")
    with pytest.raises(RuntimeError), uow_factory() as uow:
        uow.systems.save(system)
        raise RuntimeError("adapter crashed mid-write")

    with uow_factory() as uow:
        assert uow.systems.get(system.id) is None


def test_a_failed_indexing_run_never_corrupts_the_last_known_good_snapshot(
    uow_factory: UowFactory,
) -> None:
    """The scenario spec §30 actually describes: one successful indexing run,
    then a second run that writes new entities and a new snapshot but blows up
    before committing. The first snapshot must still be what a query resolves to,
    and the half-written second run's entities must not be visible at all."""
    system = System(name="s", slug="s-failed-run")
    good_entity = Entity(system_id=system.id, type=EntityType.SYMBOL, name="process")
    good_snapshot = _snapshot(
        system, "rev1", SnapshotStatus.COMPLETE, at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    with uow_factory() as uow:
        uow.systems.save(system)
        uow.graph.upsert_entities([good_entity])
        uow.snapshots.create(good_snapshot)
        uow.commit()

    with pytest.raises(RuntimeError), uow_factory() as uow:
        broken_entity = Entity(system_id=system.id, type=EntityType.SYMBOL, name="broken")
        uow.graph.upsert_entities([broken_entity])
        uow.snapshots.create(
            _snapshot(system, "rev2", SnapshotStatus.BUILDING, at=datetime(2026, 1, 2, tzinfo=UTC))
        )
        raise RuntimeError("adapter crashed before the run could complete")

    with uow_factory() as uow:
        latest = uow.snapshots.latest_complete(system.id)
        assert latest is not None
        assert latest.id == good_snapshot.id, "the last known-good snapshot must survive"

        names = {e.name for e in uow.graph.find_entities(system.id, type=EntityType.SYMBOL)}
        assert names == {"process"}, "the failed run's entities must never become visible"


def test_entity_superseded_status_survives_a_commit(uow_factory: UowFactory) -> None:
    """A regression guard tying storage to the identity model's own invariant
    (core/entities.py): a SUPERSEDED entity must keep the claims that justified
    it, and that must round-trip through storage exactly, not just in memory."""
    from hashira.core import IdentityClaim, IdentityClaimKind

    system = System(name="s", slug="s-superseded")
    claim = IdentityClaim(
        kind=IdentityClaimKind.QUALIFIED_NAME,
        value="payments.services.PaymentService.process",
        origin=Origin.PARSER,
        confidence=Confidence.LIKELY,
    )
    entity = Entity(
        system_id=system.id,
        type=EntityType.SYMBOL,
        name="process",
        status=EntityStatus.SUPERSEDED,
        identity_claims=[claim],
    )
    with uow_factory() as uow:
        uow.systems.save(system)
        uow.graph.upsert_entities([entity])
        uow.commit()

    with uow_factory() as uow:
        got = uow.graph.get_entity(entity.id)
        assert got is not None
        assert got.status is EntityStatus.SUPERSEDED
        assert got.identity_claims == [claim]
