"""Application services: where adapters, identity resolution and storage meet.

Nothing below `hashira.ports` may appear here directly — see
`indexing.py`'s docstring for how `IndexingService` stays language-agnostic.
"""

from .history import HistoricalGraph, query_at_revision
from .impact import (
    ImpactAnalyzer,
    ImpactHop,
    ImpactPath,
    ImpactResult,
    forward_impact,
    resolve_identity,
    reverse_impact,
)
from .indexing import IndexingResult, IndexingService, Normalizer

__all__ = [
    "HistoricalGraph",
    "ImpactAnalyzer",
    "ImpactHop",
    "ImpactPath",
    "ImpactResult",
    "IndexingResult",
    "IndexingService",
    "Normalizer",
    "forward_impact",
    "query_at_revision",
    "resolve_identity",
    "reverse_impact",
]
