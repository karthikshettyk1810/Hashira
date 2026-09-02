"""Events — the append-only history of the system (spec §14).

Events are the authoritative sequence of what happened. They are *not* a rebuild
mechanism: derived graph state is reconstructed from source at a revision, which
is cheaper and actually reproducible. See docs/EVENTS.md.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from pydantic import Field, model_validator

from .base import Actor, ImmutableIRModel, SourceRef, utc_now
from .enums import EventType
from .ids import EntityID, EventID, IDPrefix, SystemID, new_id


class Event(ImmutableIRModel):
    """An immutable occurrence.

    Ingestion must be idempotent (§30): the same commit seen twice is one event.
    ``dedupe_key`` is the natural key a store uniquely indexes on, derived from
    the source reference when the producer does not supply one.
    """

    id: EventID = Field(default_factory=lambda: new_id(IDPrefix.EVENT))
    system_id: SystemID
    type: EventType
    timestamp: datetime = Field(default_factory=utc_now)
    actor: Actor = Field(default_factory=Actor)
    source: SourceRef
    subject_entity_ids: list[EntityID] = Field(default_factory=list)
    payload: dict[str, object] = Field(default_factory=dict)
    dedupe_key: str | None = None

    @model_validator(mode="after")
    def _derive_dedupe_key(self) -> Event:
        if self.dedupe_key is None:
            object.__setattr__(self, "dedupe_key", self.natural_key())
        return self

    def natural_key(self) -> str:
        """A stable digest of (system, type, provider, reference).

        Two ingestions of the same underlying occurrence produce the same key, so
        a replayed webhook or a re-run indexer cannot fork history.
        """
        parts = [
            self.system_id,
            self.type.value,
            self.source.provider,
            self.source.reference or self.timestamp.isoformat(),
        ]
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]
