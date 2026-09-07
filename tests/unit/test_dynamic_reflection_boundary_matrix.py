"""Hashira 0.2 -- Resolution Integrity, R3: the dynamic/reflection
boundary. Deliberately not about resolving dynamic Python -- about being
excellent at *recognizing and disclosing* it. For each pattern below, the
question is never "can Hashira derive the target" (the honest answer is
almost always no, and should stay no); it is:

    Can Hashira detect the pattern exists at all?
    Can it identify the affected entity/site?
    Does `coverage` (structural, or per-access where the mechanism
      supports it) disclose the boundary?
    Does the result avoid reading as "there is no relationship" --
      the one failure mode this whole project exists to refuse?

| Pattern                              | Detected?     | Disclosed?                |
|------------------------------------------|----------------|-----------------------------|
| `getattr(obj,"lit")` (SQLAlchemy)        | yes            | DYNAMIC_ATTRIBUTE_ACCESS   |
| `setattr(obj,"lit",v)` (SQLAlchemy)      | yes            | DYNAMIC_ATTRIBUTE_ACCESS   |
| `getattr(obj, computed)` (SQLAlchemy)    | correctly not  | correctly silent            |
| `getattr(obj,"lit")` (Django)            | yes (R3 fix)   | DYNAMIC_ATTRIBUTE_ACCESS   |
| `getattr(obj, name)()` (dispatch)        | yes            | DYNAMIC_DISPATCH (blanket) |
| registry dispatch (`HANDLERS[k]()`)      | yes, unresolved | DYNAMIC_DISPATCH (blanket) |
| decorator-injected attr (`.delay()`)     | yes, unresolved | DYNAMIC_DISPATCH -- imprecise, open |
| framework reflection (FastAPI/Pydantic)  | n/a, no access | FRAMEWORK_REFLECTION (blanket) |
| framework reflection (Django)            | n/a            | FRAMEWORK_REFLECTION (R3 fix) |

Two real, distinct gaps closed this pass, both pure detect-and-disclose,
no new resolution logic:

1. **Django had zero `getattr`/`setattr` detection at all** for model
   field access -- not even the coarse, structural kind SQLAlchemy already
   had. `adapters/django/adapter.py`'s `_check_dynamic_attribute_call`
   ports SQLAlchemy's own, already-proven mechanism unchanged in spirit:
   a literal field name is disclosed as `DYNAMIC_ATTRIBUTE_ACCESS`, a
   computed one stays correctly silent (nothing to check against a field
   list at all).
2. **`DjangoAdapter.capabilities()` declared no `known_limitations`
   whatsoever** -- a Django project's `coverage.limitations` never
   mentioned framework reflection at all, unlike a FastAPI project's own
   (which correctly names Pydantic's `orm_mode`). Django's own reflection
   surface (DRF `ModelSerializer` field introspection, admin
   `list_display`, `get_FOO_display()`, signal receivers connected at
   runtime) is at least as large. Fixed by declaring
   `FRAMEWORK_REFLECTION` structurally, mirroring `FastAPIAdapter`'s
   existing declaration exactly.

**One real finding, deliberately left open, not fixed this pass**:
decorator-injected attributes (`@celery_task; def f(): ...` then
`f.delay(...)` elsewhere -- a real, live pattern in the read-only Rider
benchmark's own Celery usage) are *syntactically* a literal `obj.method(...)`
call the resolver can read directly (unlike `getattr(obj, computed)(...)`),
so labeling the miss `DYNAMIC_DISPATCH` -- whose own docstring describes
"a call whose target is resolved at runtime" -- is technically true but
imprecise: the target expression itself is fully static; only the
attribute a decorator injects at runtime is unknown. `LimitationKind`'s
own stated growth policy is "one real, adapter-reported case at a time,
never speculatively ahead of the evidence" -- every existing member
(`DYNAMIC_DISPATCH`/`FRAMEWORK_REFLECTION`) was added only after an
independent agent experiment rediscovered it more than once. This pass's
own single observation does not clear that bar yet; logged here so a
future pass with stronger evidence does not have to rediscover it from
nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hashira.adapters.django import DjangoAdapter
from hashira.adapters.python import PythonAdapter
from hashira.adapters.python.discovery import discover_python_files
from hashira.adapters.python.normalizer import normalize
from hashira.adapters.sqlalchemy import SQLAlchemyAdapter
from hashira.core.ids import IDPrefix, new_id
from hashira.ports.adapters import ExtractionResult, LimitationKind


@pytest.fixture
def system_id() -> str:
    return new_id(IDPrefix.SYSTEM)


def _write(root: Path, relpath: str, content: str) -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _python_base(tmp_path: Path, system_id: str) -> ExtractionResult:
    files = list(discover_python_files(tmp_path))
    return PythonAdapter().extract(tmp_path, files, system_id=system_id, revision="rev1")


# --- Detected + disclosed: getattr/setattr on a known model, both adapters --


def test_detected_and_disclosed__sqlalchemy_getattr_literal(tmp_path: Path, system_id: str) -> None:
    _write(
        tmp_path,
        "payments/models.py",
        "from sqlalchemy import Column, String\n"
        "from sqlalchemy.orm import declarative_base\n\n"
        "Base = declarative_base()\n\n\n"
        "class Payment(Base):\n"
        '    __tablename__ = "payments"\n\n'
        "    status = Column(String(20))\n",
    )
    _write(
        tmp_path,
        "payments/service.py",
        "def read(payment):\n    return getattr(payment, 'status')\n",
    )
    base = _python_base(tmp_path, system_id)
    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    unresolved = [
        o for o in addition.observations if o.kind == "sqlalchemy.unresolved_field_access"
    ]
    assert len(unresolved) == 1
    assert unresolved[0].payload["limitation_kind"] == LimitationKind.DYNAMIC_ATTRIBUTE_ACCESS.value


def test_detected_and_disclosed__django_getattr_literal(tmp_path: Path, system_id: str) -> None:
    """The first real gap this pass closes: Django had no equivalent at all."""
    _write(
        tmp_path,
        "payments/models.py",
        "from django.db import models\n\n\nclass Payment(models.Model):\n"
        "    status = models.CharField(max_length=20)\n",
    )
    _write(
        tmp_path,
        "payments/service.py",
        "def read(payment):\n    return getattr(payment, 'status')\n",
    )
    base = _python_base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    unresolved = [o for o in addition.observations if o.kind == "django.unresolved_field_access"]
    assert len(unresolved) == 1
    assert unresolved[0].payload["limitation_kind"] == LimitationKind.DYNAMIC_ATTRIBUTE_ACCESS.value


def test_correctly_silent__computed_getattr_name_is_not_a_false_limitation(
    tmp_path: Path, system_id: str
) -> None:
    """A computed name is not a literal this adapter can check against a
    field list at all -- correctly silent, not a false limitation, for
    either adapter."""
    _write(
        tmp_path,
        "payments/models.py",
        "from django.db import models\n\n\nclass Payment(models.Model):\n"
        "    status = models.CharField(max_length=20)\n",
    )
    _write(
        tmp_path,
        "payments/service.py",
        "def read(payment, field_name):\n    return getattr(payment, field_name)\n",
    )
    base = _python_base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    assert [o for o in addition.observations if "field_access" in o.kind] == []


# --- Detected, disclosed structurally: dynamic dispatch (general resolver) --


def test_detected_and_disclosed__dynamic_dispatch_via_getattr_call(
    tmp_path: Path, system_id: str
) -> None:
    _write(tmp_path, "app.py", "def dispatch(obj, name):\n    getattr(obj, name)()\n")
    system = new_id(IDPrefix.SYSTEM)
    files = list(discover_python_files(tmp_path))
    base = PythonAdapter().extract(tmp_path, files, system_id=system, revision="rev1")
    run = normalize(base.observations, system_id=system, revision="rev1")
    assert [r for r in run.relationships if r.type.value == "CALLS"] == []

    limitations = PythonAdapter().capabilities().known_limitations
    assert any(lim.kind is LimitationKind.DYNAMIC_DISPATCH for lim in limitations)


def test_detected_and_disclosed__registry_dispatch_via_subscript_call(
    tmp_path: Path, system_id: str
) -> None:
    """`HANDLERS[key]()` -- a dispatch table -- is explicitly named in
    `DYNAMIC_DISPATCH`'s own docstring as covered by the same structural
    declaration as `getattr`-based dispatch; verified here, not assumed."""
    _write(
        tmp_path,
        "app.py",
        "def handle_a():\n    pass\n\n\n"
        "HANDLERS = {'a': handle_a}\n\n\n"
        "def dispatch(key):\n    return HANDLERS[key]()\n",
    )
    files = list(discover_python_files(tmp_path))
    base = PythonAdapter().extract(tmp_path, files, system_id=system_id, revision="rev1")
    run = normalize(base.observations, system_id=system_id, revision="rev1")
    assert [r for r in run.relationships if r.type.value == "CALLS"] == []
    unresolved_calls = [o for o in run.unresolved if o.kind == "python.call"]
    assert any(o.payload["callee_expr"] == "HANDLERS[key]" for o in unresolved_calls)


# --- Detected, disclosed structurally: framework reflection -----------------


def test_detected_and_disclosed__fastapi_framework_reflection(tmp_path: Path) -> None:
    from hashira.adapters.fastapi import FastAPIAdapter

    limitations = FastAPIAdapter().capabilities().known_limitations
    assert any(lim.kind is LimitationKind.FRAMEWORK_REFLECTION for lim in limitations)


def test_detected_and_disclosed__django_framework_reflection(tmp_path: Path) -> None:
    """The second real gap this pass closes: `DjangoAdapter` declared zero
    `known_limitations` at all before this -- a Django project's own
    `coverage.limitations` never named framework reflection, even though
    Django's reflection surface (DRF serializers, admin, signals) is at
    least as large as FastAPI's single declared case."""
    limitations = DjangoAdapter().capabilities().known_limitations
    assert any(lim.kind is LimitationKind.FRAMEWORK_REFLECTION for lim in limitations)


# --- Logged, not fixed: decorator-injected attributes -----------------------


def test_logged_not_fixed__decorator_injected_attribute_call(
    tmp_path: Path, system_id: str
) -> None:
    """A real, live pattern in the read-only Rider benchmark's own Celery
    usage (`some_task.delay(...)`): syntactically a literal `obj.method(...)`
    call the resolver reads directly -- `some_task` resolves fully, only
    `.delay` (injected by a decorator at runtime) is unknown. Correctly
    stays unresolved, never fabricated. Currently falls under the same
    blanket `DYNAMIC_DISPATCH` structural disclosure as truly computed
    dispatch targets -- technically not silent, but imprecise, since this
    call's target expression is not actually dynamic. Deliberately not
    given its own `LimitationKind` this pass -- one observation does not
    clear the "independently rediscovered more than once" bar every
    existing kind was held to; logged here for a future pass with
    stronger evidence."""
    _write(
        tmp_path,
        "tasks.py",
        "def celery_task(f):\n    f.delay = lambda *a, **k: None\n    return f\n\n\n"
        "@celery_task\n"
        "def publish_pending_outbox():\n    pass\n",
    )
    _write(
        tmp_path,
        "service.py",
        "from tasks import publish_pending_outbox\n\n\n"
        "def kick():\n    publish_pending_outbox.delay()\n",
    )
    files = list(discover_python_files(tmp_path))
    base = PythonAdapter().extract(tmp_path, files, system_id=system_id, revision="rev1")
    run = normalize(base.observations, system_id=system_id, revision="rev1")
    assert [r for r in run.relationships if r.type.value == "CALLS"] == []
    unresolved_calls = [o for o in run.unresolved if o.kind == "python.call"]
    assert any(o.payload["callee_expr"] == "publish_pending_outbox.delay" for o in unresolved_calls)
