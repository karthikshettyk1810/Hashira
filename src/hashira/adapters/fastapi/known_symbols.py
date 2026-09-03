"""The closed vocabulary of FastAPI/Pydantic names this adapter recognizes.

Same discipline as `adapters.django.known_bases`: a curated allowlist of
fully-qualified names, matched against *resolved* references
(`adapters.python.resolve.resolve_expr`, seeded from the Python adapter's own
import observations) — never a name-suffix guess. A variable named `app` that
is not actually a `fastapi.FastAPI()` instance, or a class named `Response`
that does not actually extend `pydantic.BaseModel`, must not be misclassified.
"""

from __future__ import annotations

__all__ = [
    "APP_CLASSES",
    "BASE_MODEL_CLASSES",
    "DEPENDS_CALLABLES",
    "HTTP_METHODS",
    "ROUTER_CLASSES",
]

APP_CLASSES: frozenset[str] = frozenset({"fastapi.FastAPI"})
"""A module-level variable assigned the result of calling one of these is a
FastAPI application instance."""

ROUTER_CLASSES: frozenset[str] = frozenset({"fastapi.APIRouter"})
"""A module-level variable assigned the result of calling one of these is a
sub-router, later attached to an app (or another router) via
`include_router(...)`."""

DEPENDS_CALLABLES: frozenset[str] = frozenset({"fastapi.Depends"})
"""A parameter default that resolves to a call to one of these is dependency
injection (`DEPENDS_ON` in the graph), not a request-body parameter."""

HTTP_METHODS: frozenset[str] = frozenset(
    {"get", "post", "put", "patch", "delete", "options", "head"}
)
"""The decorator method names FastAPI's `FastAPI`/`APIRouter` expose for
route registration (`@app.get(...)`, `@router.post(...)`, ...)."""

BASE_MODEL_CLASSES: frozenset[str] = frozenset({"pydantic.BaseModel"})
"""A class directly extending one of these is a Pydantic model -- eligible
to be recognized as a route handler's request or response contract."""
