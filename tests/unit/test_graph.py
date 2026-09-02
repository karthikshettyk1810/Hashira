"""Entities and relationships: identity, provenance and time (spec §10, §11, §13)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from hashira.core import (
    Confidence,
    Entity,
    EntityStatus,
    EntityType,
    Evidence,
    IdentityClaim,
    IdentityClaimKind,
    KnowledgeClass,
    Origin,
    Relationship,
    RelationshipType,
    SourceLocation,
    System,
)
from hashira.core.relationships import IMPACT_EDGES, INVERSE


def test_entity_display_name_prefers_qualified_name(payment_service: Entity) -> None:
    assert payment_service.display_name == "payments.services.PaymentService.process"
    payment_service.qualified_name = None
    assert payment_service.display_name == "process"


def test_source_location_rejects_inverted_range() -> None:
    with pytest.raises(ValidationError):
        SourceLocation(file="a.py", line_start=90, line_end=10)


def test_superseded_entity_must_keep_its_identity_claims(system: System) -> None:
    with pytest.raises(ValidationError, match="identity claims"):
        Entity(
            system_id=system.id,
            type=EntityType.SYMBOL,
            name="process",
            status=EntityStatus.SUPERSEDED,
        )


def test_strongest_claim_breaks_ties_by_confidence(system: System) -> None:
    entity = Entity(
        system_id=system.id,
        type=EntityType.SYMBOL,
        name="process",
        identity_claims=[
            IdentityClaim(
                kind=IdentityClaimKind.QUALIFIED_NAME,
                value="old.path.process",
                origin=Origin.PARSER,
                confidence=Confidence.LIKELY,
            ),
            IdentityClaim(
                kind=IdentityClaimKind.QUALIFIED_NAME,
                value="payments.services.PaymentService.process",
                origin=Origin.USER_DECLARED,
                confidence=Confidence.CERTAIN,
            ),
        ],
    )
    strongest = entity.strongest_claim(IdentityClaimKind.QUALIFIED_NAME)
    assert strongest is not None
    assert strongest.value == "payments.services.PaymentService.process"
    assert entity.strongest_claim(IdentityClaimKind.SYMBOL_ID) is None


def _edge(system: System, a: Entity, b: Entity, ev: Evidence, **kw: object) -> Relationship:
    return Relationship(
        system_id=system.id,
        source_entity_id=a.id,
        target_entity_id=b.id,
        type=RelationshipType.CALLS,
        origin=Origin.STATIC_ANALYSIS,
        evidence_ids=[ev.id],
        **kw,
    )


def test_observed_edge_must_cite_evidence(
    system: System, payment_service: Entity, razorpay_client: Entity
) -> None:
    with pytest.raises(ValidationError, match="must cite evidence"):
        Relationship(
            system_id=system.id,
            source_entity_id=payment_service.id,
            target_entity_id=razorpay_client.id,
            type=RelationshipType.CALLS,
            origin=Origin.STATIC_ANALYSIS,
        )


def test_edge_inherits_confidence_from_origin(
    system: System, payment_service: Entity, razorpay_client: Entity, evidence: Evidence
) -> None:
    edge = _edge(system, payment_service, razorpay_client, evidence)
    assert edge.confidence is Confidence.CERTAIN
    assert edge.is_current


def test_self_edges_are_rejected_except_for_lineage(
    system: System, payment_service: Entity, evidence: Evidence
) -> None:
    with pytest.raises(ValidationError, match="self-edge"):
        _edge(system, payment_service, payment_service, evidence)

    lineage = Relationship(
        system_id=system.id,
        source_entity_id=payment_service.id,
        target_entity_id=payment_service.id,
        type=RelationshipType.RELATED_TO,
        knowledge_class=KnowledgeClass.DERIVATION,
        origin=Origin.DERIVED,
    )
    assert lineage.type is RelationshipType.RELATED_TO


def test_closing_an_edge_preserves_it(
    system: System, payment_service: Entity, razorpay_client: Entity, evidence: Evidence
) -> None:
    """Retraction is a change of validity, never a delete (§13)."""
    edge = _edge(system, payment_service, razorpay_client, evidence, valid_from_revision="abc123")
    closed = edge.close(revision="def456")

    assert edge.is_current
    assert not closed.is_current
    assert closed.id == edge.id
    assert closed.valid_from_revision == "abc123"
    assert closed.valid_until_revision == "def456"
    assert closed.evidence_ids == edge.evidence_ids


def test_held_at_answers_historical_questions(
    system: System, payment_service: Entity, razorpay_client: Entity, evidence: Evidence
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    edge = _edge(system, payment_service, razorpay_client, evidence, valid_from=start)
    closed = edge.close(at=start + timedelta(days=30))

    assert not closed.held_at(start - timedelta(days=1))
    assert closed.held_at(start + timedelta(days=1))
    assert not closed.held_at(start + timedelta(days=60))


def test_valid_until_cannot_precede_valid_from(
    system: System, payment_service: Entity, razorpay_client: Entity, evidence: Evidence
) -> None:
    with pytest.raises(ValidationError, match="precedes valid_from"):
        _edge(
            system,
            payment_service,
            razorpay_client,
            evidence,
            valid_from=datetime(2026, 6, 1, tzinfo=UTC),
            valid_until=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_every_relationship_type_has_an_inverse() -> None:
    assert set(INVERSE) == set(RelationshipType)


def test_impact_traversal_excludes_containment() -> None:
    """Everything CONTAINS everything eventually; a blast radius of 'the repo' is useless."""
    assert RelationshipType.CONTAINS not in IMPACT_EDGES
    assert RelationshipType.CHANGED_BY not in IMPACT_EDGES
    assert RelationshipType.CALLS in IMPACT_EDGES
    assert RelationshipType.WRITES in IMPACT_EDGES
    assert set(RelationshipType) >= IMPACT_EDGES
