"""The closed vocabulary of SQLAlchemy names this adapter recognizes.

Same discipline as `adapters.django.known_bases` and
`adapters.fastapi.known_symbols`: a curated allowlist of fully-qualified
names, matched against *resolved* references -- never a name-suffix guess.
A class named `Base` that is not actually rooted in SQLAlchemy's declarative
machinery must not be misclassified as one.
"""

from __future__ import annotations

__all__ = [
    "COLUMN_CALLABLES",
    "DECLARATIVE_BASE_FACTORIES",
    "DECLARATIVE_BASE_SEED",
    "FOREIGN_KEY_CALLABLE",
]

DECLARATIVE_BASE_SEED: str = "sqlalchemy.orm.DeclarativeBase"
"""SQLAlchemy 2.0's class-based declarative root. A class directly
extending this (`class Base(DeclarativeBase): pass`) -- or, transitively,
extending something that itself extends it -- is a declarative base. This
one is resolvable through ordinary import bindings, so `adapter.py` finds
it via the same `python.inheritance` observations Django's model detection
already relies on; no extra parsing needed for this style."""

DECLARATIVE_BASE_FACTORIES: frozenset[str] = frozenset(
    {"sqlalchemy.orm.declarative_base", "sqlalchemy.ext.declarative.declarative_base"}
)
"""SQLAlchemy <2.0's factory-function style: `Base = declarative_base()`.
Unlike the class-based style above, `Base` here is a plain module-level
variable Python's own extractor has no reason to track (it is not a
class/function symbol) -- `adapter.py` finds these itself, the same way
`adapters.fastapi.adapter` finds `app = FastAPI()`."""

COLUMN_CALLABLES: frozenset[str] = frozenset({"sqlalchemy.Column", "sqlalchemy.orm.mapped_column"})
"""A class-body attribute assigned a call to one of these, in a recognized
model's body, is a column -- covers both the legacy imperative style
(`id = Column(Integer, primary_key=True)`) and SQLAlchemy 2.0's annotated
style (`id: Mapped[int] = mapped_column(primary_key=True)`)."""

FOREIGN_KEY_CALLABLE: str = "sqlalchemy.ForeignKey"
"""A column whose definition contains a call to this, with a single string
argument (`ForeignKey("accounts.id")`), references another table's column.
Only the string-literal form is recognized in v0.1 -- `ForeignKey(Account.id)`
(a direct attribute reference) needs cross-class resolution this milestone
does not attempt; see `adapter.py`'s module docstring."""
