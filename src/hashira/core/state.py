"""Desired, observed and historical state — and the drift between them (spec §15)."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from .base import IRModel, utc_now
from .enums import Confidence, StateKind
from .ids import DeploymentID, EntityID, EvidenceID, IDPrefix, SystemID, new_id


class StateFact(IRModel):
    """One assertion about an entity's state at a point in time."""

    entity_id: EntityID
    key: str = Field(min_length=1, description="e.g. 'replicas', 'version', 'health'.")
    value: object
    kind: StateKind
    confidence: Confidence = Confidence.CERTAIN
    evidence_ids: list[EvidenceID] = Field(default_factory=list)
    recorded_at: datetime = Field(default_factory=utc_now)


class Drift(IRModel):
    """A discrepancy between what should be running and what is.

    v0.1 only reports drift. Acting on it requires the policy and verification
    boundaries of §29, which are deliberately out of scope.
    """

    entity_id: EntityID
    key: str
    desired: object
    observed: object
    detected_at: datetime = Field(default_factory=utc_now)
    evidence_ids: list[EvidenceID] = Field(default_factory=list)


class SystemState(IRModel):
    """The three state views, held apart so drift is computable rather than guessed."""

    system_id: SystemID
    desired: list[StateFact] = Field(default_factory=list)
    observed: list[StateFact] = Field(default_factory=list)
    as_of: datetime = Field(default_factory=utc_now)

    def drift(self) -> list[Drift]:
        """Every (entity, key) where desired and observed disagree."""
        desired_index = {(fact.entity_id, fact.key): fact for fact in self.desired}
        drifts: list[Drift] = []
        for observed in self.observed:
            wanted = desired_index.get((observed.entity_id, observed.key))
            if wanted is None or wanted.value == observed.value:
                continue
            drifts.append(
                Drift(
                    entity_id=observed.entity_id,
                    key=observed.key,
                    desired=wanted.value,
                    observed=observed.value,
                    evidence_ids=[*wanted.evidence_ids, *observed.evidence_ids],
                )
            )
        return drifts


class Deployment(IRModel):
    """A deployed version and the environment it landed in (spec §8, §22)."""

    id: DeploymentID = Field(default_factory=lambda: new_id(IDPrefix.DEPLOYMENT))
    system_id: SystemID
    environment: str = Field(min_length=1)
    version: str = Field(min_length=1)
    revision: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    succeeded: bool | None = None
    component_entity_ids: list[EntityID] = Field(default_factory=list)
    evidence_ids: list[EvidenceID] = Field(default_factory=list)
