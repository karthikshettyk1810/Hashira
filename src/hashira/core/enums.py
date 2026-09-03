"""The closed vocabularies of System IR.

Spec §38 names "schema becomes too complex" as a live risk, with "keep a small
universal vocabulary; use extensions" as the countermeasure. Everything in this
module is therefore deliberately closed: a technology-specific concept earns a
place in ``metadata`` or an adapter extension, never a new member here.

Adding a member is an additive IR change (§31). Removing or renaming one is
breaking and requires a schema version bump.
"""

from __future__ import annotations

from enum import StrEnum


class EntityType(StrEnum):
    """Spec §8. The universal semantic types an adapter may map onto."""

    SYSTEM = "SYSTEM"
    COMPONENT = "COMPONENT"
    MODULE = "MODULE"
    SYMBOL = "SYMBOL"
    INTERFACE = "INTERFACE"
    DATA_STORE = "DATA_STORE"
    DATA_ENTITY = "DATA_ENTITY"
    MESSAGE_CHANNEL = "MESSAGE_CHANNEL"
    PROCESS = "PROCESS"
    EXTERNAL_SYSTEM = "EXTERNAL_SYSTEM"
    DEPENDENCY = "DEPENDENCY"
    TEST = "TEST"
    CONFIGURATION = "CONFIGURATION"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    ENVIRONMENT = "ENVIRONMENT"
    DEPLOYMENT = "DEPLOYMENT"
    EVENT = "EVENT"
    INCIDENT = "INCIDENT"
    CHANGE = "CHANGE"
    DECISION = "DECISION"
    CONSTRAINT = "CONSTRAINT"


class IdentityClaimKind(StrEnum):
    """The signals identity resolution may weigh (spec §10).

    Ranked in ``hashira.identity.resolver`` by how much a single hit is worth
    trusting on its own. The vocabulary is closed here for the same reason
    every other enum in this module is: a new signal source is a deliberate
    addition to the ladder's policy, not a string an adapter happens to invent.
    """

    SYMBOL_ID = "SYMBOL_ID"
    """A stable language-level identifier from LSP/SCIP/compiler metadata."""

    USER_DECLARED = "USER_DECLARED"
    """An explicit human mapping. Outranks every inferred signal."""

    DECLARATION_ANCHOR = "DECLARATION_ANCHOR"
    """The exact declaration site: file path + qualified name + kind, all at
    once. Not independent of ``QUALIFIED_NAME`` — it is derived from it — but
    materially narrower: two symbols coincidentally sharing a qualified name
    is plausible across a graph; two coincidentally sharing a qualified name
    *at the identical file path, of the identical kind* is not. Strong enough
    to merge in place (an unchanged file re-indexed should not manufacture a
    new entity generation on every run); still correctly powerless the moment
    the file, the name, or the kind changes at all — that is exactly the
    boundary a rename needs Git evidence to cross (docs/IR.md)."""

    GIT_RENAME = "GIT_RENAME"
    """Git's own rename/similarity detection between two revisions."""

    MIGRATION_LINEAGE = "MIGRATION_LINEAGE"
    """A database migration's own record of a rename (e.g. ALTER TABLE ... RENAME)."""

    QUALIFIED_NAME = "QUALIFIED_NAME"
    """Same fully-qualified name. Common, but names get reused across files."""

    STRUCTURAL_SIMILARITY = "STRUCTURAL_SIMILARITY"
    """Same type, same container, similar signature. Weak on its own."""


class RelationshipType(StrEnum):
    """Spec §11, plus three additions documented in docs/IR.md.

    ``SUPERSEDES`` carries identity lineage: when resolution cannot establish
    with sufficient confidence that a renamed or relocated construct is the same
    entity, we mint a new entity and record the lineage rather than silently
    overwriting history (§10).

    ``MAPS_TO`` and ``REFERENCES`` came from the SQLAlchemy `DataAdapter`
    milestone (`adapters/sqlalchemy/`), where two genuinely new semantic
    relationships showed up that no existing type honestly covered:
    ``MAPS_TO`` is a Python class's declarative binding to the physical
    entity it persists to (``EXTENDS``/``IMPLEMENTS`` are about code
    structure, not this; ``RELATED_TO`` is too vague to be useful in an
    impact query) -- unlike a FastAPI handler, an ORM class and its table
    are not the same conceptual thing, so tagging the class in place (the
    rule every other framework enricher here follows) would have been
    dishonest. ``REFERENCES`` is a foreign-key relationship between two
    columns -- a relational-database fact independent of any one adapter,
    distinct from ``DEPENDS_ON`` (which already spans build-time import
    dependencies and runtime dependency injection; folding a third, very
    different kind of dependency into it would cost precision on every
    existing query that walks it).
    """

    CONTAINS = "CONTAINS"
    DEFINES = "DEFINES"
    IMPORTS = "IMPORTS"
    CALLS = "CALLS"
    EXTENDS = "EXTENDS"
    IMPLEMENTS = "IMPLEMENTS"
    DEPENDS_ON = "DEPENDS_ON"
    EXPOSES = "EXPOSES"
    CONSUMES = "CONSUMES"
    PRODUCES = "PRODUCES"
    READS = "READS"
    WRITES = "WRITES"
    TRIGGERS = "TRIGGERS"
    PUBLISHES = "PUBLISHES"
    SUBSCRIBES = "SUBSCRIBES"
    RUNS_IN = "RUNS_IN"
    DEPLOYS_TO = "DEPLOYS_TO"
    CONFIGURED_BY = "CONFIGURED_BY"
    TESTED_BY = "TESTED_BY"
    MONITORED_BY = "MONITORED_BY"
    CHANGED_BY = "CHANGED_BY"
    AFFECTS = "AFFECTS"
    CAUSED = "CAUSED"
    FIXED_BY = "FIXED_BY"
    RELATED_TO = "RELATED_TO"
    SUPERSEDES = "SUPERSEDES"
    MAPS_TO = "MAPS_TO"
    REFERENCES = "REFERENCES"


class KnowledgeClass(StrEnum):
    """Spec §12. What kind of claim a record is, and therefore how far to trust it.

    The whole epistemic model rests on never collapsing these into one another.
    """

    OBSERVATION = "OBSERVATION"
    """Directly obtained from a source. A parser said so, Git said so."""

    DERIVATION = "DERIVATION"
    """Mechanically derived from observations. Reproducible, no model involved."""

    INFERENCE = "INFERENCE"
    """A probabilistic or semantic conclusion. May be wrong."""

    HYPOTHESIS = "HYPOTHESIS"
    """An unverified explanation or prediction awaiting evidence."""


class Origin(StrEnum):
    """Where a claim came from. Determines its default confidence."""

    GIT = "GIT"
    PARSER = "PARSER"
    STATIC_ANALYSIS = "STATIC_ANALYSIS"
    LSP = "LSP"
    SCIP = "SCIP"
    MANIFEST = "MANIFEST"
    MIGRATION = "MIGRATION"
    CONFIGURATION = "CONFIGURATION"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    TEST_REPORT = "TEST_REPORT"
    CI = "CI"
    RUNTIME = "RUNTIME"
    USER_DECLARED = "USER_DECLARED"
    DERIVED = "DERIVED"
    LLM = "LLM"


class Confidence(StrEnum):
    """How much weight a claim carries.

    Deliberately ordinal rather than a float. A ``0.9`` from a call-graph walker
    and a ``0.9`` from a language model are not the same quantity and cannot be
    compared or multiplied, but a float schema invites exactly that. Three levels
    with defined meanings stay honest and stay comparable.

    See docs/IR.md for the deviation from §11's ``confidence: 1.0``.
    """

    CERTAIN = "CERTAIN"
    """A deterministic source asserted it. Wrong only if the source is broken."""

    LIKELY = "LIKELY"
    """Derived or inferred with corroboration. Safe to act on, worth surfacing."""

    SPECULATIVE = "SPECULATIVE"
    """Unverified. Must never be presented to an agent as fact."""

    @property
    def rank(self) -> int:
        """Ordinal position, for sorting and thresholds only."""
        return _CONFIDENCE_RANK[self]

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Confidence):
            return NotImplemented
        return self.rank < other.rank

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Confidence):
            return NotImplemented
        return self.rank <= other.rank

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Confidence):
            return NotImplemented
        return self.rank > other.rank

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Confidence):
            return NotImplemented
        return self.rank >= other.rank


_CONFIDENCE_RANK: dict[Confidence, int] = {
    Confidence.SPECULATIVE: 0,
    Confidence.LIKELY: 1,
    Confidence.CERTAIN: 2,
}

_DETERMINISTIC_ORIGINS: frozenset[Origin] = frozenset(
    {
        Origin.GIT,
        Origin.PARSER,
        Origin.STATIC_ANALYSIS,
        Origin.LSP,
        Origin.SCIP,
        Origin.MANIFEST,
        Origin.MIGRATION,
        Origin.CONFIGURATION,
        Origin.INFRASTRUCTURE,
        Origin.TEST_REPORT,
        Origin.CI,
        Origin.RUNTIME,
        Origin.USER_DECLARED,
    }
)


def is_deterministic(origin: Origin) -> bool:
    """True when the origin is a source of record rather than a guess (§26)."""
    return origin in _DETERMINISTIC_ORIGINS


def default_confidence(origin: Origin) -> Confidence:
    """The confidence an origin carries unless a producer argues otherwise."""
    if origin is Origin.LLM:
        return Confidence.SPECULATIVE
    if origin is Origin.DERIVED:
        return Confidence.LIKELY
    return Confidence.CERTAIN


class InferenceStatus(StrEnum):
    """Spec §12. The lifecycle every non-deterministic claim moves through."""

    PROPOSED = "PROPOSED"
    SUPPORTED = "SUPPORTED"
    CONFIRMED = "CONFIRMED"
    CONTRADICTED = "CONTRADICTED"
    REJECTED = "REJECTED"


class EntityStatus(StrEnum):
    """Whether an entity is still part of the system as last indexed."""

    ACTIVE = "ACTIVE"
    REMOVED = "REMOVED"
    SUPERSEDED = "SUPERSEDED"


class EventType(StrEnum):
    """Spec §14. Immutable occurrences in system history."""

    REPOSITORY_DISCOVERED = "REPOSITORY_DISCOVERED"
    CODE_INDEXED = "CODE_INDEXED"
    COMMIT_CREATED = "COMMIT_CREATED"
    FILE_CHANGED = "FILE_CHANGED"
    TEST_EXECUTED = "TEST_EXECUTED"
    TEST_FAILED = "TEST_FAILED"
    TEST_PASSED = "TEST_PASSED"
    DEPLOYMENT_STARTED = "DEPLOYMENT_STARTED"
    DEPLOYMENT_COMPLETED = "DEPLOYMENT_COMPLETED"
    DEPLOYMENT_FAILED = "DEPLOYMENT_FAILED"
    RUNTIME_ERROR = "RUNTIME_ERROR"
    RUNTIME_METRIC = "RUNTIME_METRIC"
    INCIDENT_CREATED = "INCIDENT_CREATED"
    INCIDENT_RESOLVED = "INCIDENT_RESOLVED"
    DECISION_RECORDED = "DECISION_RECORDED"
    CONFIGURATION_CHANGED = "CONFIGURATION_CHANGED"
    ENTITY_IDENTITY_MERGED = "ENTITY_IDENTITY_MERGED"
    """Identity resolution joined two prior entities (§10). Never a silent edit."""


class ActorType(StrEnum):
    """Who or what caused an event or change."""

    DEVELOPER = "DEVELOPER"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"
    INTEGRATION = "INTEGRATION"
    UNKNOWN = "UNKNOWN"


class ChangeType(StrEnum):
    """Spec §16."""

    COMMIT = "COMMIT"
    PULL_REQUEST = "PULL_REQUEST"
    MIGRATION = "MIGRATION"
    CONFIGURATION = "CONFIGURATION"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    DEPENDENCY_UPGRADE = "DEPENDENCY_UPGRADE"
    REVERT = "REVERT"


class ChangeOutcome(StrEnum):
    """How a change ended up, once history can say."""

    UNKNOWN = "UNKNOWN"
    VERIFIED = "VERIFIED"
    REVERTED = "REVERTED"
    IMPLICATED_IN_INCIDENT = "IMPLICATED_IN_INCIDENT"


class IncidentSeverity(StrEnum):
    """Spec §17."""

    SEV1 = "SEV1"
    SEV2 = "SEV2"
    SEV3 = "SEV3"
    SEV4 = "SEV4"


class IncidentStatus(StrEnum):
    """Spec §17."""

    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    MITIGATED = "MITIGATED"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class StateKind(StrEnum):
    """Spec §15. The gap between the first two is system drift."""

    DESIRED = "DESIRED"
    OBSERVED = "OBSERVED"
    HISTORICAL = "HISTORICAL"


class SnapshotStatus(StrEnum):
    """Spec §30: a failed run must not corrupt the last known-good snapshot."""

    BUILDING = "BUILDING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"
