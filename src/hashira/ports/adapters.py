"""Adapter ports (spec §18, §19).

An adapter's whole job is to turn one technology into observations in the
universal vocabulary. It may not mutate core semantics, invent entity types, or
decide what something means at the system level — it reports what it saw.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ..core.entities import Entity
from ..core.enums import EntityType, RelationshipType
from ..core.events import Event
from ..core.evidence import Evidence, Observation
from ..core.relationships import Relationship


class AdapterCapabilities(BaseModel):
    """What an adapter claims it can do (§38: "capability matrix").

    Advertised up front so the CLI can tell a user *why* their Kafka topics did
    not show up, rather than leaving a silent hole in the graph.
    """

    name: str
    version: str
    ir_versions: list[str] = Field(description="IR versions this adapter supports (§31).")
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    entity_types: list[EntityType] = Field(default_factory=list)
    relationship_types: list[RelationshipType] = Field(default_factory=list)
    requires_network: bool = False
    """True if the adapter contacts a remote service. Local-first mode refuses these (§29)."""


class ExtractionResult(BaseModel):
    """One adapter's yield for one unit of work.

    Adapters return entities and relationships alongside the evidence that
    justifies them, so a partial failure is a smaller graph rather than an
    unattributed one (§30).
    """

    observations: list[Observation] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    """Immutable history facts (§14) this adapter can report directly — a
    Git adapter's commits, for instance. Most adapters leave this empty."""
    errors: list[str] = Field(default_factory=list)

    def extend(self, other: ExtractionResult) -> None:
        self.observations.extend(other.observations)
        self.entities.extend(other.entities)
        self.relationships.extend(other.relationships)
        self.evidence.extend(other.evidence)
        self.events.extend(other.events)
        self.errors.extend(other.errors)


@runtime_checkable
class Adapter(Protocol):
    """Common shape for every adapter kind."""

    def capabilities(self) -> AdapterCapabilities: ...

    def detect(self, root: Path) -> bool:
        """Cheap check for whether this adapter applies. Must not parse the world."""
        ...


@runtime_checkable
class LanguageAdapter(Adapter, Protocol):
    """Source-level extraction for one language (§18)."""

    def owned_files(self, root: Path) -> Iterable[Path]: ...

    def extract(
        self, root: Path, files: Sequence[Path], *, system_id: str, revision: str | None
    ) -> ExtractionResult:
        """Parse the given files. Incremental indexing passes only what changed (§19)."""
        ...


@runtime_checkable
class FrameworkAdapter(Adapter, Protocol):
    """Maps framework constructs onto universal types, e.g. a Django view to INTERFACE."""

    def enrich(
        self, root: Path, base: ExtractionResult, *, system_id: str, revision: str | None
    ) -> ExtractionResult:
        """Add framework meaning on top of language-level extraction."""
        ...


@runtime_checkable
class InfrastructureAdapter(Adapter, Protocol):
    """Docker, Kubernetes, Terraform and friends (§18)."""

    def extract(self, root: Path, *, system_id: str, revision: str | None) -> ExtractionResult: ...


@runtime_checkable
class HistoryAdapter(Adapter, Protocol):
    """Git and other version-control systems (§18).

    Reports commits, file changes and rename detections between two
    revisions — nothing more. It never decides what a detected rename means
    for entity identity; that decision belongs to `hashira.identity`, fed by
    the evidence this adapter reports (see `adapters/git/adapter.py`'s
    module docstring for why that separation is load-bearing here).
    """

    def extract(
        self,
        root: Path,
        *,
        system_id: str,
        since_revision: str | None,
        until_revision: str | None,
    ) -> ExtractionResult:
        """Everything observable between ``since_revision`` (exclusive, or
        the full history if ``None``) and ``until_revision`` (or the current
        checkout if ``None``)."""
        ...


@runtime_checkable
class IntegrationAdapter(Adapter, Protocol):
    """External systems of record: GitHub, Sentry, CI (§18).

    These reach the network, so they are the adapters local-first mode disables.
    """

    def pull(self, *, system_id: str, since: str | None = None) -> ExtractionResult: ...
