"""Adapter ports (spec §18, §19).

An adapter's whole job is to turn one technology into observations in the
universal vocabulary. It may not mutate core semantics, invent entity types, or
decide what something means at the system level — it reports what it saw.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ..core.entities import Entity
from ..core.enums import EntityType, RelationshipType
from ..core.events import Event
from ..core.evidence import Evidence, Observation
from ..core.relationships import Relationship


class LimitationKind(StrEnum):
    """A capability boundary Hashira's analysis cannot see past -- one
    *category* of missed edge, never a count of individual instances
    (`application/impact.py`'s "coverage is not confidence" explains why:
    a limitation must stay useful whether it applies to one access or ten
    thousand). Closed and small on purpose, exactly like `core/enums.py`'s
    IR vocabulary -- but kept here, in `ports/`, rather than there:
    this describes a boundary of Hashira's *own* analysis, not a fact
    about the system being analyzed, so it never touches the versioned
    IR contract (`core/schema.py::IR_MODELS`).

    Grows one real, adapter-reported case at a time -- never speculatively
    ahead of an adapter that actually hits it. A dynamic-dispatch or
    framework-reflection kind, for instance, belongs here only once some
    adapter's analysis genuinely needs to report one, not in advance of
    that (`docs/IR.md`'s entry on this milestone has the reasoning)."""

    RAW_SQL = "RAW_SQL"
    """A read or write expressed as a raw query string (e.g.
    `sqlalchemy.text(...)`) rather than through the ORM -- no adapter
    parses SQL text for column references. Structural: true regardless
    of which entity is being asked about, so this is the one kind
    reported unconditionally by an adapter's own `AdapterCapabilities`."""
    UNTYPED_PARAMETER = "UNTYPED_PARAMETER"
    """An attribute access on a function parameter whose declared type
    (or absence of one) could not be matched to a known class."""
    RETURN_VALUE_PROVENANCE = "RETURN_VALUE_PROVENANCE"
    """An attribute access on a local variable assigned from a known
    object's method call, whose own return type is unannotated or does
    not resolve to a known model -- including a call through `self`/`cls`
    (deliberately not resolved -- see `adapters/sqlalchemy/adapter.py`'s
    module docstring) and a chained return value (a call on a name that
    is itself return-value-sourced, one hop further than this adapter
    follows). Distinct from `UNTYPED_PARAMETER`: this is "we know a call
    produced this value and couldn't tell what it returns," not "we don't
    know what this parameter is at all"."""
    DYNAMIC_ATTRIBUTE_ACCESS = "DYNAMIC_ATTRIBUTE_ACCESS"
    """`getattr(obj, "field")`/`setattr(obj, "field", value)` naming a
    real column by a literal string -- deliberately never resolved into
    an edge, even though the literal makes it look easy: once dynamic
    dispatch is "supported" for a literal name, the same code invites
    `getattr(obj, variable)`/`getattr(obj, mapping[key])`, each requiring
    a different, unbounded kind of guessing. Reported so the access does
    not vanish silently, not because resolving it is hard."""


class LimitationScope(StrEnum):
    """Which analytical *surface* a `Limitation` bounds -- coarser than
    `kind` (a caller can ask "are there gaps in raw SQL" without
    enumerating every kind that could produce one), and deliberately
    named after the surface being analyzed rather than an AST operation:
    `RAW_SQL` doesn't do "field access" in the sense the other kinds do
    (no attribute node exists for an analyzer to even consider), so
    labeling it the same scope as an unresolved parameter would blur two
    genuinely different capability boundaries into one."""

    ORM_ATTRIBUTE_ACCESS = "ORM_ATTRIBUTE_ACCESS"
    """Whether a specific attribute read/write, expressed somewhere in
    Python source, can be traced to a known mapped field."""
    RAW_SQL_REFERENCES = "RAW_SQL_REFERENCES"
    """Whether a data entity/field is referenced from a raw query string
    rather than through the ORM at all -- a different surface, not
    analyzed by walking Python attribute access."""


class Limitation(BaseModel):
    """One named, categorical gap in Hashira's analysis -- never a count
    of individual missed edges. Group and filter on `kind`/`scope`;
    `detail` is for a reader who has not memorized the enum vocabulary,
    never the sole carrier of meaning."""

    kind: LimitationKind
    scope: LimitationScope
    detail: str


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
    known_limitations: list[Limitation] = Field(default_factory=list)
    """This adapter's *structural* analytical gaps -- true regardless of
    which entity a caller asks about (today: only `RAW_SQL`). A
    *conditional* gap (only some entities are actually affected, e.g.
    `UNTYPED_PARAMETER`/`RETURN_VALUE_PROVENANCE`) is not declared here;
    it is reported per-entity instead, via that entity's own indexing-time
    metadata (`application/indexing.py`) -- declaring it unconditionally
    here would be exactly the "blanket pessimism" `docs/IR.md`'s "coverage
    is not confidence" entry warns against. `application.impact` surfaces
    both kinds together, undifferentiated by source, as
    `ImpactResult.coverage.limitations`."""


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
class DataAdapter(Adapter, Protocol):
    """Persistence semantics -- table/column identity, foreign keys, basic
    read/write evidence -- e.g. an ORM's declarative models (§18).

    Structurally identical to `FrameworkAdapter` (an `enrich()` over
    whatever language/framework extraction already produced), but a
    distinct kind on purpose: a `DataAdapter` must not care which, if any,
    `FrameworkAdapter` produced the code it is enriching. SQLAlchemy models
    mean the same thing whether the consuming application is FastAPI,
    Django, a CLI, or nothing at all -- `IndexingService` runs data adapters
    after framework adapters (mirroring the layering diagram this port was
    designed from), but nothing here may read a framework-specific
    observation kind to do its own job.
    """

    def enrich(
        self, root: Path, base: ExtractionResult, *, system_id: str, revision: str | None
    ) -> ExtractionResult:
        """Add persistence meaning on top of whatever extraction has
        produced so far."""
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
