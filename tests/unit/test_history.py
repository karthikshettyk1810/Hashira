"""Events, snapshots, changes, incidents and drift (spec §13-§17, §30)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from hashira.core import (
    Actor,
    ActorType,
    CausalHypothesis,
    Change,
    ChangeOutcome,
    ChangeType,
    Confidence,
    Entity,
    Event,
    EventType,
    Evidence,
    Incident,
    IncidentSeverity,
    IncidentStatus,
    InferenceStatus,
    Snapshot,
    SnapshotStatus,
    SourceRef,
    StateFact,
    StateKind,
    System,
    SystemState,
    VerificationResult,
)


def _commit(system: System, sha: str) -> Event:
    return Event(
        system_id=system.id,
        type=EventType.COMMIT_CREATED,
        actor=Actor(type=ActorType.DEVELOPER, id="dev_1", name="K"),
        source=SourceRef(provider="git", reference=sha),
    )


def test_events_are_immutable(system: System) -> None:
    event = _commit(system, "abc123")
    with pytest.raises(ValidationError):
        event.type = EventType.RUNTIME_ERROR


def test_the_same_occurrence_dedupes_to_one_key(system: System) -> None:
    """A replayed webhook must not fork history (§30)."""
    first = _commit(system, "abc123")
    second = _commit(system, "abc123")
    assert first.id != second.id
    assert first.dedupe_key == second.dedupe_key


def test_different_commits_do_not_collide(system: System) -> None:
    assert _commit(system, "abc123").dedupe_key != _commit(system, "def456").dedupe_key


def test_snapshot_completion_stamps_a_time(system: System) -> None:
    snapshot = Snapshot(
        system_id=system.id,
        revision="abc123",
        indexing_version="0.1.0",
        ir_version="0.1.0",
        status=SnapshotStatus.COMPLETE,
    )
    assert snapshot.completed_at is not None
    assert snapshot.is_known_good


def test_a_failed_run_is_never_servable(system: System) -> None:
    snapshot = Snapshot(
        system_id=system.id,
        revision="abc123",
        indexing_version="0.1.0",
        ir_version="0.1.0",
        status=SnapshotStatus.FAILED,
        diagnostics=["python adapter crashed on payments/services.py"],
    )
    assert not snapshot.is_known_good


def test_snapshot_cannot_parent_itself(system: System) -> None:
    snapshot = Snapshot(
        system_id=system.id, revision="abc", indexing_version="0.1.0", ir_version="0.1.0"
    )
    with pytest.raises(ValidationError, match="own parent"):
        snapshot.parent_snapshot_id = snapshot.id


def test_declared_and_inferred_intent_coexist(system: System, payment_service: Entity) -> None:
    """The gap between them is the point (§16)."""
    change = Change(
        system_id=system.id,
        type=ChangeType.COMMIT,
        revision="abc123",
        declared_intent="Optimize payment processing.",
        inferred_intent="Touches payment, refund and subscription workflows.",
        affected_entity_ids=[payment_service.id],
    )
    assert change.declared_intent != change.inferred_intent
    assert change.outcome is ChangeOutcome.UNKNOWN
    assert not change.is_verified


def test_verified_outcome_requires_verification(system: System) -> None:
    with pytest.raises(ValidationError, match="verification result"):
        Change(system_id=system.id, type=ChangeType.COMMIT, outcome=ChangeOutcome.VERIFIED)

    change = Change(
        system_id=system.id,
        type=ChangeType.COMMIT,
        outcome=ChangeOutcome.VERIFIED,
        verification=[VerificationResult(kind="unit", passed=True)],
    )
    assert change.is_verified


def test_incident_timeline_is_checked(system: System) -> None:
    start = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    with pytest.raises(ValidationError, match="ended before it started"):
        Incident(
            system_id=system.id,
            title="Checkout failures",
            severity=IncidentSeverity.SEV2,
            started_at=start,
            ended_at=start - timedelta(hours=1),
        )


def test_resolved_incident_needs_an_end_time(system: System) -> None:
    with pytest.raises(ValidationError, match="must have an end time"):
        Incident(
            system_id=system.id,
            title="Checkout failures",
            severity=IncidentSeverity.SEV2,
            status=IncidentStatus.RESOLVED,
            started_at=datetime(2026, 8, 1, tzinfo=UTC),
        )


def test_a_confirmed_cause_needs_evidence(system: System, evidence: Evidence) -> None:
    """Spec §38 lists false causal conclusions as a top risk; the schema refuses them."""
    with pytest.raises(ValidationError, match="confirmed cause requires evidence"):
        Incident(
            system_id=system.id,
            reference="INC-182",
            title="Duplicate payment capture",
            severity=IncidentSeverity.SEV1,
            status=IncidentStatus.CLOSED,
            started_at=datetime(2026, 8, 1, tzinfo=UTC),
            ended_at=datetime(2026, 8, 1, 3, tzinfo=UTC),
            confirmed_cause="transaction boundary moved inside the retry loop",
        )

    incident = Incident(
        system_id=system.id,
        reference="INC-182",
        title="Duplicate payment capture",
        severity=IncidentSeverity.SEV1,
        status=IncidentStatus.CLOSED,
        started_at=datetime(2026, 8, 1, tzinfo=UTC),
        ended_at=datetime(2026, 8, 1, 3, tzinfo=UTC),
        confirmed_cause="transaction boundary moved inside the retry loop",
        evidence_ids=[evidence.id],
    )
    assert incident.duration_seconds == 3 * 3600


def test_hypothesis_cannot_be_confirmed_without_evidence() -> None:
    with pytest.raises(ValidationError, match="evidence that confirmed it"):
        CausalHypothesis(
            statement="CHG-411 caused INC-182",
            confidence=Confidence.LIKELY,
            status=InferenceStatus.CONFIRMED,
        )


def test_drift_is_the_difference_between_desired_and_observed(
    system: System, payment_service: Entity
) -> None:
    state = SystemState(
        system_id=system.id,
        desired=[
            StateFact(
                entity_id=payment_service.id, key="replicas", value=3, kind=StateKind.DESIRED
            ),
            StateFact(
                entity_id=payment_service.id, key="version", value="2.4.1", kind=StateKind.DESIRED
            ),
        ],
        observed=[
            StateFact(
                entity_id=payment_service.id, key="replicas", value=2, kind=StateKind.OBSERVED
            ),
            StateFact(
                entity_id=payment_service.id, key="version", value="2.4.1", kind=StateKind.OBSERVED
            ),
            StateFact(
                entity_id=payment_service.id, key="uptime", value=99.9, kind=StateKind.OBSERVED
            ),
        ],
    )
    drifts = state.drift()
    assert len(drifts) == 1
    assert (drifts[0].key, drifts[0].desired, drifts[0].observed) == ("replicas", 3, 2)
