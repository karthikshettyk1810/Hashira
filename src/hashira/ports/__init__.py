"""Ports: the stable contracts between the core and everything replaceable (§6)."""

from .adapters import (
    Adapter,
    AdapterCapabilities,
    ExtractionResult,
    FrameworkAdapter,
    InfrastructureAdapter,
    IntegrationAdapter,
    LanguageAdapter,
)
from .intelligence import EmbeddingProvider, IntelligenceProvider, IntelligenceRequest
from .repositories import (
    EventStore,
    EvidenceStore,
    GraphRepository,
    InferenceStore,
    ObservationStore,
    SnapshotStore,
    SystemRepository,
    UnitOfWork,
)

__all__ = [
    "Adapter",
    "AdapterCapabilities",
    "EmbeddingProvider",
    "EventStore",
    "EvidenceStore",
    "ExtractionResult",
    "FrameworkAdapter",
    "GraphRepository",
    "InferenceStore",
    "InfrastructureAdapter",
    "IntegrationAdapter",
    "IntelligenceProvider",
    "IntelligenceRequest",
    "LanguageAdapter",
    "ObservationStore",
    "SnapshotStore",
    "SystemRepository",
    "UnitOfWork",
]
