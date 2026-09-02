"""System IR: the technology-neutral model of a software system.

Nothing in this package may import an LLM SDK, a web framework, a database driver
or an agent runtime. That rule is guardrail 7 of spec §41 and it is enforced by a
contract test, not by good intentions.
"""

from .base import Actor, ImmutableIRModel, IRModel, SourceLocation, SourceRef, TechnologyInfo
from .changes import Change, RiskAssessment, VerificationResult
from .entities import Entity, IdentityClaim, System
from .enums import (
    ActorType,
    ChangeOutcome,
    ChangeType,
    Confidence,
    EntityStatus,
    EntityType,
    EventType,
    IdentityClaimKind,
    IncidentSeverity,
    IncidentStatus,
    InferenceStatus,
    KnowledgeClass,
    Origin,
    RelationshipType,
    SnapshotStatus,
    StateKind,
    default_confidence,
    is_deterministic,
)
from .events import Event
from .evidence import Evidence, Inference, Observation
from .ids import IDPrefix, entity_uri, new_id, parse_id, system_uri
from .incidents import CausalHypothesis, Incident
from .relationships import IMPACT_EDGES, INVERSE, Relationship
from .schema import (
    ADAPTER_CONTRACT_VERSION,
    EVENT_SCHEMA_VERSION,
    IR_MODELS,
    IR_VERSION,
    full_schema,
    json_schema,
    supports_ir_version,
)
from .snapshots import Snapshot, SnapshotStatistics
from .state import Deployment, Drift, StateFact, SystemState

__all__ = [
    "ADAPTER_CONTRACT_VERSION",
    "EVENT_SCHEMA_VERSION",
    "IMPACT_EDGES",
    "INVERSE",
    "IR_MODELS",
    "IR_VERSION",
    "Actor",
    "ActorType",
    "CausalHypothesis",
    "Change",
    "ChangeOutcome",
    "ChangeType",
    "Confidence",
    "Deployment",
    "Drift",
    "Entity",
    "EntityStatus",
    "EntityType",
    "Event",
    "EventType",
    "Evidence",
    "IDPrefix",
    "IRModel",
    "IdentityClaim",
    "IdentityClaimKind",
    "ImmutableIRModel",
    "Incident",
    "IncidentSeverity",
    "IncidentStatus",
    "Inference",
    "InferenceStatus",
    "KnowledgeClass",
    "Observation",
    "Origin",
    "Relationship",
    "RelationshipType",
    "RiskAssessment",
    "Snapshot",
    "SnapshotStatistics",
    "SnapshotStatus",
    "SourceLocation",
    "SourceRef",
    "StateFact",
    "StateKind",
    "System",
    "SystemState",
    "TechnologyInfo",
    "VerificationResult",
    "default_confidence",
    "entity_uri",
    "full_schema",
    "is_deterministic",
    "json_schema",
    "new_id",
    "parse_id",
    "supports_ir_version",
    "system_uri",
]
