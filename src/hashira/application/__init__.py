"""Application services: where adapters, identity resolution and storage meet.

Nothing below `hashira.ports` may appear here directly — see
`indexing.py`'s docstring for how `IndexingService` stays language-agnostic.
"""

from .graph import get_entity, get_entity_at_revision, get_relationships
from .history import HistoricalGraph, query_at_revision
from .impact import (
    ImpactAnalyzer,
    ImpactHop,
    ImpactPath,
    ImpactResult,
    LineageHop,
    LineageResult,
    follow_lineage,
    forward_impact,
    resolve_identity,
    reverse_impact,
)
from .indexing import IndexingResult, IndexingService, Normalizer
from .search import search_entities

__all__ = [
    "HistoricalGraph",
    "ImpactAnalyzer",
    "ImpactHop",
    "ImpactPath",
    "ImpactResult",
    "IndexingResult",
    "IndexingService",
    "LineageHop",
    "LineageResult",
    "Normalizer",
    "follow_lineage",
    "forward_impact",
    "get_entity",
    "get_entity_at_revision",
    "get_relationships",
    "query_at_revision",
    "resolve_identity",
    "reverse_impact",
    "search_entities",
]
