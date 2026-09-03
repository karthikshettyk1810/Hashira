"""Application services: where adapters, identity resolution and storage meet.

Nothing below `hashira.ports` may appear here directly — see
`indexing.py`'s docstring for how `IndexingService` stays language-agnostic.
"""

from .indexing import IndexingResult, IndexingService, Normalizer

__all__ = ["IndexingResult", "IndexingService", "Normalizer"]
