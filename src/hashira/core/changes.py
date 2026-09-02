"""Changes — intentional modifications and their consequences (spec §16)."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from .base import Actor, IRModel, utc_now
from .enums import ChangeOutcome, ChangeType, Confidence
from .ids import ChangeID, EntityID, EvidenceID, IDPrefix, IncidentID, SystemID, new_id


class RiskAssessment(IRModel):
    """What a change might cost, separate from how far it reaches.

    Spec §25 draws the distinction that matters: graph reachability is not risk.
    A helper touched by four hundred modules may be trivial; a twenty-line change
    inside payment capture is not.
    """

    level: str = Field(pattern=r"^(LOW|MEDIUM|HIGH|CRITICAL)$")
    rationale: str = Field(min_length=1)
    confidence: Confidence
    blast_radius: int = Field(default=0, ge=0, description="Transitively reachable entities.")
    critical_paths: list[str] = Field(
        default_factory=list, description="Named workflows the change intersects."
    )
    evidence_ids: list[EvidenceID] = Field(default_factory=list)


class VerificationResult(IRModel):
    """Whether the change was checked, and by what."""

    kind: str = Field(description="e.g. 'unit', 'integration', 'e2e', 'policy', 'runtime'.")
    passed: bool
    detail: str | None = None
    evidence_ids: list[EvidenceID] = Field(default_factory=list)
    recorded_at: datetime = Field(default_factory=utc_now)


class Change(IRModel):
    """A modification, what it was meant to do, and what it turned out to do.

    Declared intent and inferred impact are held apart on purpose (§16). The gap
    between "optimize payment processing" and "touches refunds and subscriptions"
    is the most useful thing Hashira can show a reviewer.
    """

    id: ChangeID = Field(default_factory=lambda: new_id(IDPrefix.CHANGE))
    system_id: SystemID
    type: ChangeType
    actor: Actor = Field(default_factory=Actor)
    revision: str | None = None
    pull_request: str | None = None
    title: str | None = None
    declared_intent: str | None = Field(
        default=None, description="What the author said they were doing."
    )
    inferred_intent: str | None = Field(
        default=None, description="What Hashira concluded. Never overwrites the declaration."
    )
    affected_entity_ids: list[EntityID] = Field(default_factory=list)
    risk: RiskAssessment | None = None
    verification: list[VerificationResult] = Field(default_factory=list)
    deployment_ids: list[str] = Field(default_factory=list)
    related_incident_ids: list[IncidentID] = Field(default_factory=list)
    outcome: ChangeOutcome = ChangeOutcome.UNKNOWN
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _check_outcome(self) -> Change:
        if self.outcome is ChangeOutcome.VERIFIED and not self.verification:
            raise ValueError("a VERIFIED change must carry at least one verification result")
        if self.outcome is ChangeOutcome.IMPLICATED_IN_INCIDENT and not self.related_incident_ids:
            raise ValueError("implication in an incident must name the incident")
        return self

    @property
    def is_verified(self) -> bool:
        return bool(self.verification) and all(result.passed for result in self.verification)
