"""The FastAPI framework enricher: builds on Python's own structural
observations rather than re-parsing Python independently.

See `adapter.py`'s module docstring for the reuse boundary, and
`known_symbols.py` for why detection is evidence-based (resolved calls and
inheritance), never a name-suffix guess.
"""

from .adapter import FastAPIAdapter
from .normalizer import normalize

__all__ = ["FastAPIAdapter", "normalize"]
