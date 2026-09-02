"""Incidents — where runtime reality reconnects to engineering history (spec §17)."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from .base import IRModel, utc_now
from .enums import Confidence, IncidentSeverity, IncidentStatus, InferenceStatus
from .ids import ChangeID, EntityID, EvidenceID, IDPrefix, IncidentID, SystemID, new_id


class CausalHypothesis(IRModel):
    """A candidate explanation, explicitly not a conclusion.

    Spec §38 lists false causal conclusions as a top risk. The countermeasure is
    structural: a suspected cause cannot be stored as a cause. It is a hypothesis
    with a status, and it only becomes ``CONFIRMED`` when something verified it.
    """

    statement: str = Field(min_length=1)
    suspect_entity_ids: list[EntityID] = Field(default_factory=list)
    suspect_change_ids: list[ChangeID] = Field(default_factory=list)
    confidence: Confidence
    status: InferenceStatus = InferenceStatus.PROPOSED
    evidence_ids: list[EvidenceID] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _require_evidence_to_advance(self) -> CausalHypothesis:
        if self.status is InferenceStatus.CONFIRMED and not self.evidence_ids:
            raise ValueError("a confirmed cause must cite the evidence that confirmed it (§17)")
        return self


class Incident(IRModel):
    """An operational failure and everything Hashira can connect it to."""

    id: IncidentID = Field(default_factory=lambda: new_id(IDPrefix.INCIDENT))
    system_id: SystemID
    reference: str | None = Field(default=None, description="External key, e.g. 'INC-182'.")
    title: str = Field(min_length=1)
    severity: IncidentSeverity
    status: IncidentStatus = IncidentStatus.OPEN
    started_at: datetime
    ended_at: datetime | None = None
    symptoms: list[str] = Field(default_factory=list)
    affected_entity_ids: list[EntityID] = Field(default_factory=list)
    related_deployment_ids: list[str] = Field(default_factory=list)
    related_change_ids: list[ChangeID] = Field(default_factory=list)
    hypotheses: list[CausalHypothesis] = Field(default_factory=list)
    confirmed_cause: str | None = None
    resolution: str | None = None
    evidence_ids: list[EvidenceID] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_timeline(self) -> Incident:
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("incident ended before it started")
        finished = (IncidentStatus.RESOLVED, IncidentStatus.CLOSED)
        if self.status in finished and self.ended_at is None:
            raise ValueError(f"a {self.status.value} incident must have an end time")
        if self.confirmed_cause is not None and not self.evidence_ids:
            raise ValueError("a confirmed cause requires evidence (§17)")
        return self

    @property
    def duration_seconds(self) -> float | None:
        if self.ended_at is None:
            return None
        return (self.ended_at - self.started_at).total_seconds()
