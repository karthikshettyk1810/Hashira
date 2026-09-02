"""Evidence, observations and inferences — the epistemic spine (spec §12).

The invariant this module exists to protect: a fact and a guess must never end up
in the same trust category. Anything that is not directly observed carries its
provenance, its confidence and a pointer to what supports it, all the way out to
the agent that consumes it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from .base import ImmutableIRModel, IRModel, SourceRef, utc_now
from .enums import (
    Confidence,
    InferenceStatus,
    KnowledgeClass,
    Origin,
    default_confidence,
    is_deterministic,
)
from .ids import (
    EntityID,
    EvidenceID,
    IDPrefix,
    InferenceID,
    ObservationID,
    SystemID,
    new_id,
)


class Evidence(ImmutableIRModel):
    """An immutable record of something a source actually said.

    Evidence is the terminal node of every "why do you believe that?" chain. It is
    frozen because an inference built on it must remain auditable even after the
    world has moved on: a later observation may invalidate the conclusion, but it
    may not rewrite the record that led there.
    """

    id: EvidenceID = Field(default_factory=lambda: new_id(IDPrefix.EVIDENCE))
    system_id: SystemID
    origin: Origin
    source: SourceRef
    summary: str = Field(
        min_length=1, description="One line a human can read without opening the source."
    )
    excerpt: str | None = Field(
        default=None, description="The verbatim fragment, redacted per the security policy."
    )
    locator: str | None = Field(
        default=None, description="Path, URL, query or offset that reproduces this evidence."
    )
    observed_at: datetime = Field(default_factory=utc_now)
    content_hash: str | None = Field(
        default=None, description="Digest of the underlying artifact, for idempotent ingestion."
    )


class Observation(ImmutableIRModel):
    """A normalized fact from a source, before it becomes graph state (§19).

    Observations are what adapters emit. Persisting them separately from the
    entities they produce is what makes indexing replayable and lets us answer
    "which adapter claimed this, and at which revision?" long after the fact.
    """

    id: ObservationID = Field(default_factory=lambda: new_id(IDPrefix.OBSERVATION))
    system_id: SystemID
    adapter: str = Field(description="Adapter name and version that produced this.")
    origin: Origin
    kind: str = Field(description="Adapter-local discriminator, e.g. 'python.function'.")
    payload: dict[str, object] = Field(default_factory=dict)
    evidence_ids: list[EvidenceID] = Field(default_factory=list)
    revision: str | None = None
    observed_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _must_be_deterministic(self) -> Observation:
        """An observation by definition comes from a source of record (§26)."""
        if not is_deterministic(self.origin):
            raise ValueError(
                f"origin {self.origin.value} cannot produce an Observation; "
                "model output enters as an Inference"
            )
        return self


class Inference(IRModel):
    """A derived, semantic or probabilistic conclusion (§12).

    Unlike evidence this is mutable, because its *status* is the point: an
    inference is proposed, then supported or contradicted as the world reveals
    itself. What cannot change is the evidence it was built on.
    """

    id: InferenceID = Field(default_factory=lambda: new_id(IDPrefix.INFERENCE))
    system_id: SystemID
    knowledge_class: KnowledgeClass
    statement: str = Field(min_length=1, description="The claim, stated plainly.")
    subject_entity_ids: list[EntityID] = Field(default_factory=list)
    origin: Origin
    confidence: Confidence | None = None
    status: InferenceStatus = InferenceStatus.PROPOSED
    evidence_ids: list[EvidenceID] = Field(default_factory=list)
    provider: str | None = Field(
        default=None, description="Model provider, when an LLM produced this (§26)."
    )
    model: str | None = Field(default=None, description="Model identifier and version.")
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _apply_epistemic_rules(self) -> Inference:
        if self.knowledge_class is KnowledgeClass.OBSERVATION:
            raise ValueError("an OBSERVATION is an Observation record, not an Inference")
        if self.confidence is None:
            object.__setattr__(self, "confidence", default_confidence(self.origin))
        if self.origin is Origin.LLM:
            if not self.provider or not self.model:
                raise ValueError("LLM-origin inference must record provider and model (§12)")
            if self.confidence is Confidence.CERTAIN:
                raise ValueError(
                    "an LLM cannot assert CERTAIN; it is not an authority for system facts (§26)"
                )
        advanced = (InferenceStatus.SUPPORTED, InferenceStatus.CONFIRMED)
        if self.status in advanced and not self.evidence_ids:
            raise ValueError(
                f"status {self.status.value} requires evidence; "
                "provenance is mandatory for derived knowledge (§42)"
            )
        return self

    @property
    def is_actionable(self) -> bool:
        """Whether this may be presented to an agent as something to rely on."""
        return (
            self.status in (InferenceStatus.SUPPORTED, InferenceStatus.CONFIRMED)
            and self.confidence is not None
            and self.confidence >= Confidence.LIKELY
        )
