"""The SQLAlchemy data enricher: builds on Python's own structural
observations rather than re-parsing Python independently.

This is a `DataAdapter`, not a `FrameworkAdapter` -- see `adapter.py`'s
module docstring for why that distinction is load-bearing (it must mean the
same thing whether or not any web framework is even in use), and
`known_symbols.py` for why detection is evidence-based (resolved calls and
inheritance), never a name-suffix guess.
"""

from .adapter import SQLAlchemyAdapter
from .normalizer import normalize

__all__ = ["SQLAlchemyAdapter", "normalize"]
