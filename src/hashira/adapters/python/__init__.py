"""The Python language adapter: `ast`-based extraction, nothing framework-specific.

Understands plain Python only. Django, FastAPI, Celery and friends are
enrichers layered on top elsewhere — see
docs/ARCHITECTURE.md#hashiras-own-stack-vs-what-hashira-understands.
"""

from .adapter import PythonAdapter
from .normalizer import NormalizedRun, normalize

__all__ = ["NormalizedRun", "PythonAdapter", "normalize"]
