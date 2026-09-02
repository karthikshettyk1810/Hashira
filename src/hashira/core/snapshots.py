"""Snapshots — named cut points in the system's understanding (spec §13).

A snapshot is a *label on a revision*, not a copy of the graph. Reproducing one
means re-deriving from source at that revision, which is a promise we can keep;
replaying an event log to reconstruct a graph is a promise that quietly rots.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from .base import IRModel, utc_now
from .enums import SnapshotStatus
from .ids import IDPrefix, SnapshotID, SystemID, new_id


class SnapshotStatistics(IRModel):
    """What the run produced. Also the input to Hashira's own telemetry (§32)."""

    entity_count: int = Field(default=0, ge=0)
    relationship_count: int = Field(default=0, ge=0)
    observation_count: int = Field(default=0, ge=0)
    evidence_count: int = Field(default=0, ge=0)
    inference_count: int = Field(default=0, ge=0)
    files_processed: int = Field(default=0, ge=0)
    adapters_failed: list[str] = Field(default_factory=list)
    duration_seconds: float | None = Field(default=None, ge=0)


class Snapshot(IRModel):
    """Hashira's understanding of a system at a revision boundary."""

    id: SnapshotID = Field(default_factory=lambda: new_id(IDPrefix.SNAPSHOT))
    system_id: SystemID
    revision: str = Field(min_length=1, description="The source revision this describes.")
    parent_snapshot_id: SnapshotID | None = None
    indexing_version: str = Field(
        description="Version of the indexing pipeline, so behaviour changes are reviewable (§33)."
    )
    ir_version: str
    status: SnapshotStatus = SnapshotStatus.BUILDING
    statistics: SnapshotStatistics = Field(default_factory=SnapshotStatistics)
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    diagnostics: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_lifecycle(self) -> Snapshot:
        if self.status is SnapshotStatus.COMPLETE and self.completed_at is None:
            object.__setattr__(self, "completed_at", utc_now())
        if self.parent_snapshot_id == self.id:
            raise ValueError("a snapshot cannot be its own parent")
        return self

    @property
    def is_known_good(self) -> bool:
        """Whether this may be served to a consumer.

        A failed run must never become the answer to a query (§30).
        """
        return self.status is SnapshotStatus.COMPLETE
