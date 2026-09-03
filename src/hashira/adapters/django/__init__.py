"""The Django framework enricher: builds on Python's own structural
observations rather than re-parsing Python independently.

See `adapter.py`'s module docstring for the reuse boundary, and
`known_bases.py` for why detection is evidence-based (resolved inheritance),
never a name-suffix guess.
"""

from .adapter import DjangoAdapter
from .normalizer import normalize

__all__ = ["DjangoAdapter", "normalize"]
