"""The closed vocabulary of Django/DRF base classes this adapter recognizes.

Deliberately a curated allowlist of fully-qualified names, not a name-suffix
heuristic ("ends with View", "ends with Model"). A class named `TreeView` or
`WorldModel` that has nothing to do with Django must not be misclassified —
and the only way to know a class really extends `django.db.models.Model` is
to check what it actually, resolvedly, extends (`python.inheritance`
observations from the Python adapter — see `adapter.py`), not what it is
named. Matching by name would be exactly the kind of weak, coincidental
signal this project's identity model refuses to trust elsewhere; there is no
reason framework detection should be held to a lower bar.

Extending this list is how the adapter's reach grows — e.g. supporting a new
DRF mixin — without touching the detection logic itself.
"""

from __future__ import annotations

__all__ = ["MODEL_BASES", "VIEW_BASES"]

MODEL_BASES: frozenset[str] = frozenset({"django.db.models.Model"})
"""A class directly extending one of these is a Django model. Abstract base
model chains (a model extending another model extending `models.Model`) are
not resolved transitively in v0.1 — see this package's docs entry."""

VIEW_BASES: frozenset[str] = frozenset(
    {
        "django.views.View",
        "django.views.generic.View",
        "django.views.generic.base.View",
        "django.views.generic.TemplateView",
        "django.views.generic.ListView",
        "django.views.generic.DetailView",
        "django.views.generic.CreateView",
        "django.views.generic.UpdateView",
        "django.views.generic.DeleteView",
        "rest_framework.views.APIView",
        "rest_framework.generics.GenericAPIView",
        "rest_framework.viewsets.ViewSet",
        "rest_framework.viewsets.GenericViewSet",
        "rest_framework.viewsets.ModelViewSet",
    }
)
"""A class directly extending one of these is a Django or Django REST
Framework class-based view. Function-based views (`@api_view` or bare view
functions referenced from `urls.py`) are not detected in v0.1 — a URL route
whose target does not resolve to a known view class stays unresolved rather
than guessed at (see `adapter.py`)."""

MODEL_FIELD_MODULE_PREFIX = "django.db.models."
"""A model attribute assigned a call whose resolved target starts with this
prefix (`django.db.models.CharField`, `.ForeignKey`, `.DecimalField`, ...) is
treated as a field declaration. Broader than an explicit allowlist on
purpose: Django ships dozens of field types and third-party packages add
more (`django.contrib.postgres.fields.ArrayField` would *not* match this
narrow prefix, deliberately — see the package docs for what that costs)."""
