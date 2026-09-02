"""Relationships — the edges of the System Graph (spec §11, §13, §20).

Temporal design note. The spec offers three ways to know what was true when:
per-relationship validity (§11), snapshots (§13), and event replay (§14). Three
sources of truth drift apart. Here the *relationship's revision-keyed validity is
the truth*; a snapshot is a named cut point over it, and events are history, not
a rebuild mechanism. See docs/IR.md.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from .base import IRModel, utc_now
from .enums import Confidence, KnowledgeClass, Origin, RelationshipType, default_confidence
from .ids import EntityID, EvidenceID, IDPrefix, RelationshipID, SystemID, new_id


class Relationship(IRModel):
    """A typed, provenance-carrying, time-bounded edge between two entities.

    Relationships are first-class records rather than adjacency lists because
    almost every interesting question is about the edge itself: who says
    PaymentService calls Razorpay, how sure are they, and was that still true at
    the revision that broke?
    """

    id: RelationshipID = Field(default_factory=lambda: new_id(IDPrefix.RELATIONSHIP))
    system_id: SystemID
    source_entity_id: EntityID
    target_entity_id: EntityID
    type: RelationshipType
    knowledge_class: KnowledgeClass = KnowledgeClass.OBSERVATION
    origin: Origin
    confidence: Confidence | None = None
    evidence_ids: list[EvidenceID] = Field(default_factory=list)

    # Revision-keyed validity is the authoritative temporal axis.
    valid_from_revision: str | None = Field(
        default=None, description="Revision at which this edge was first observed."
    )
    valid_until_revision: str | None = Field(
        default=None, description="Revision at which it stopped being observed; None means current."
    )
    # Wall-clock validity is derived from the revisions above and kept for
    # time-range queries. It must never disagree with them.
    valid_from: datetime = Field(default_factory=utc_now)
    valid_until: datetime | None = None

    metadata: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_edge(self) -> Relationship:
        lineage = (RelationshipType.RELATED_TO, RelationshipType.SUPERSEDES)
        if self.source_entity_id == self.target_entity_id and self.type not in lineage:
            raise ValueError(f"{self.type.value} cannot be a self-edge")
        if self.valid_until is not None and self.valid_until < self.valid_from:
            raise ValueError("valid_until precedes valid_from")
        if self.confidence is None:
            object.__setattr__(self, "confidence", default_confidence(self.origin))
        if self.knowledge_class is KnowledgeClass.OBSERVATION and not self.evidence_ids:
            raise ValueError(
                "an observed relationship must cite evidence; "
                "every important claim points at its source (§4)"
            )
        return self

    @property
    def is_current(self) -> bool:
        """Whether this edge is still believed to hold as of the latest indexing."""
        return self.valid_until_revision is None and self.valid_until is None

    def held_at(self, when: datetime) -> bool:
        """Whether this edge held at a wall-clock instant (§13)."""
        if when < self.valid_from:
            return False
        return self.valid_until is None or when < self.valid_until

    def close(self, *, revision: str | None = None, at: datetime | None = None) -> Relationship:
        """Return a closed copy. Retracting an edge is an edit to its validity,
        never a delete: history has to stay readable (§13)."""
        return self.model_copy(
            update={
                "valid_until_revision": revision,
                "valid_until": at or utc_now(),
            }
        )


#: Inverse edges, so a traversal can answer "what depends on me?" without
#: storing both directions. Spec §40 writes some of these in prose form
#: (``EXPOSED_BY``, ``INVOLVED_IN``); the stored vocabulary is §11's, and the
#: inverse is computed at query time.
INVERSE: dict[RelationshipType, str] = {
    RelationshipType.CONTAINS: "CONTAINED_BY",
    RelationshipType.DEFINES: "DEFINED_IN",
    RelationshipType.IMPORTS: "IMPORTED_BY",
    RelationshipType.CALLS: "CALLED_BY",
    RelationshipType.EXTENDS: "EXTENDED_BY",
    RelationshipType.IMPLEMENTS: "IMPLEMENTED_BY",
    RelationshipType.DEPENDS_ON: "DEPENDED_ON_BY",
    RelationshipType.EXPOSES: "EXPOSED_BY",
    RelationshipType.CONSUMES: "CONSUMED_BY",
    RelationshipType.PRODUCES: "PRODUCED_BY",
    RelationshipType.READS: "READ_BY",
    RelationshipType.WRITES: "WRITTEN_BY",
    RelationshipType.TRIGGERS: "TRIGGERED_BY",
    RelationshipType.PUBLISHES: "PUBLISHED_BY",
    RelationshipType.SUBSCRIBES: "SUBSCRIBED_BY",
    RelationshipType.RUNS_IN: "HOSTS",
    RelationshipType.DEPLOYS_TO: "DEPLOYMENT_TARGET_OF",
    RelationshipType.CONFIGURED_BY: "CONFIGURES",
    RelationshipType.TESTED_BY: "TESTS",
    RelationshipType.MONITORED_BY: "MONITORS",
    RelationshipType.CHANGED_BY: "CHANGED",
    RelationshipType.AFFECTS: "AFFECTED_BY",
    RelationshipType.CAUSED: "CAUSED_BY",
    RelationshipType.FIXED_BY: "FIXES",
    RelationshipType.RELATED_TO: "RELATED_TO",
    RelationshipType.SUPERSEDES: "SUPERSEDED_BY",
}

#: Edges an impact traversal follows by default (§25). Structural containment and
#: history are deliberately excluded: everything CONTAINS everything eventually.
IMPACT_EDGES: frozenset[RelationshipType] = frozenset(
    {
        RelationshipType.CALLS,
        RelationshipType.IMPORTS,
        RelationshipType.EXTENDS,
        RelationshipType.IMPLEMENTS,
        RelationshipType.DEPENDS_ON,
        RelationshipType.EXPOSES,
        RelationshipType.CONSUMES,
        RelationshipType.PRODUCES,
        RelationshipType.READS,
        RelationshipType.WRITES,
        RelationshipType.TRIGGERS,
        RelationshipType.PUBLISHES,
        RelationshipType.SUBSCRIBES,
        RelationshipType.CONFIGURED_BY,
    }
)
