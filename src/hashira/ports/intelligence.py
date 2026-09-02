"""The AI boundary (spec §26).

The core never imports a model SDK. It imports this Protocol, and a provider
implements it somewhere the domain cannot see. Everything a model produces comes
back as an ``Inference`` with evidence and confidence — there is no return path
that lets a model assert a fact.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ..core.entities import Entity
from ..core.evidence import Evidence, Inference
from ..core.relationships import Relationship


class IntelligenceRequest(BaseModel):
    """A question, plus the deterministic context the model is allowed to reason over.

    The model does not get to go looking. It reasons over what the graph already
    established, which is what keeps its output attributable.
    """

    system_id: str
    task: str = Field(description="e.g. 'classify_component', 'hypothesize_cause'.")
    entities: list[Entity] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    instructions: str | None = None


@runtime_checkable
class IntelligenceProvider(Protocol):
    """Optional semantic layer. The MVP must be fully useful without one (§35)."""

    def name(self) -> str: ...

    def is_available(self) -> bool: ...

    def infer(self, request: IntelligenceRequest) -> Sequence[Inference]:
        """Return inferences, each carrying provider, model, evidence and confidence."""
        ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Semantic retrieval only.

    Spec §21: a vector is a retrieval index, never a system fact. Nothing that
    reads from here may write a relationship without independent evidence.
    """

    def name(self) -> str: ...

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...
