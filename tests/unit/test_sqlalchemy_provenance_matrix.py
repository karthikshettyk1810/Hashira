"""Hashira 0.2 -- Resolution Integrity, R2: instance & attribute
provenance. Where `test_sqlalchemy_coverage_matrix.py` audits every
*syntactic form* a field access can take within one function, this file
audits the orthogonal axis: does a field access still resolve when the
*instance's own type* was established through a different provenance form,
and/or a different file, than the access site itself?

| Pattern                                              | Outcome                     |
|--------------------------------------------------------|------------------------------|
| constructor instance, cross-module                     | SUPPORTED                   |
| constructor instance, function-local import             | SUPPORTED (real bug, fixed) |
| constructor instance, aliased import                    | SUPPORTED                   |
| typed parameter, cross-module                           | SUPPORTED                   |
| typed parameter, aliased import                          | SUPPORTED                   |
| return-value instance, cross-module                     | SUPPORTED                   |
| constructor keyword write, cross-module                 | SUPPORTED                   |
| self-attribute chain, constructor-composed               | SUPPORTED (real bug, fixed) |
| self-attribute chain, cross-module                       | SUPPORTED (real bug, fixed) |
| self-attribute chain, aliased import                     | SUPPORTED (real bug, fixed) |
| unrelated self-attribute (adversarial)                   | correctly silent, no guess  |
| self-attribute assigned outside `__init__`               | correctly silent (bounded)  |
| multi-hop nested attribute (`self.a.b.field`)            | correctly silent (bounded)  |
| module-level singleton instance field access             | open gap, not fixed         |
| dynamic `getattr` through a self-attribute base          | open gap, not fixed         |

Three real, entirely *silent* gaps came out of building this matrix --
worse than a disclosed `LimitationKind`, the exact "unresolved looks like
no relationship" failure mode this project exists to refuse: a
function-local import of a model/type was invisible to
`_local_class_instances`/`_typed_parameter_instances` (this adapter builds
its own, independent resolution context, never wired to the general Python
resolver's own R1 fix for the same gap), and a constructor-composed
dependency (`self._payment = Payment()` in `__init__`, read as
`self._payment.status` elsewhere) had no equivalent of the general
resolver's `SELF_ATTRIBUTE` mechanism at all. Both closed this pass,
narrowly: `function_local_imports` was promoted from
`adapters/python/extractor.py` to shared `adapters/python/resolve.py`
plumbing (the same "a second, independent adapter needs the exact same
thing" trigger `_python_index.py` was already extracted for), and
`_self_attribute_class_instances` mirrors `_local_class_instances`'
own "any known class" broadening, scoped to `__init__` alone -- the same
discipline `resolve.py`'s own `SELF_ATTRIBUTE` docstring states for the
general resolver.

**Deliberately not attempted, per this milestone's own instruction not to
solve everything a matrix reveals**: a module-level singleton instance
(`payment = Payment()` at module scope, imported and touched from another
file) is unfixed for field-access purposes -- a real gap, but a much rarer
real-world shape than a service/client singleton (ORM row instances are not
usually process-wide singletons), unlike the general `CALLS` resolver's own
singleton-instance fix, which *was* high-value (a Kafka publisher client,
not an ORM row). `getattr(self._payment, "status")` is also unfixed:
`_check_dynamic_attribute_call`'s own guard requires its first argument to
be a bare `ast.Name`, so an `ast.Attribute` base (`self._payment`) is never
even considered for `DYNAMIC_ATTRIBUTE_ACCESS` classification, regardless
of whether the literal field name matches. Both are pinned here as
`open gap, not fixed` rows rather than silently left out of this file, so a
future widening is a deliberate change to this table, not silent drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hashira.adapters.python import PythonAdapter
from hashira.adapters.python.discovery import discover_python_files
from hashira.adapters.sqlalchemy import SQLAlchemyAdapter
from hashira.core.ids import IDPrefix, new_id
from hashira.ports.adapters import ExtractionResult


@pytest.fixture
def system_id() -> str:
    return new_id(IDPrefix.SYSTEM)


def _write(root: Path, relpath: str, content: str) -> None:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _enrich(tmp_path: Path, system_id: str) -> ExtractionResult:
    files = list(discover_python_files(tmp_path))
    base = PythonAdapter().extract(tmp_path, files, system_id=system_id, revision="rev1")
    return SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")


_MODEL = (
    "from sqlalchemy import Column, String\n"
    "from sqlalchemy.orm import declarative_base\n\n"
    "Base = declarative_base()\n\n\n"
    "class Payment(Base):\n"
    '    __tablename__ = "payments"\n\n'
    "    status = Column(String(20))\n"
)


def _field_accesses(result: ExtractionResult) -> list[object]:
    return [o for o in result.observations if o.kind == "sqlalchemy.field_access"]


def _unresolved(result: ExtractionResult) -> list[object]:
    return [o for o in result.observations if o.kind == "sqlalchemy.unresolved_field_access"]


# --- SUPPORTED: constructor instance, every cross-file variant --------------


def test_supported__constructor_instance_cross_module(tmp_path: Path, system_id: str) -> None:
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "payments/service.py",
        "from payments.models import Payment\n\n\n"
        "def process():\n"
        "    payment = Payment()\n"
        '    payment.status = "captured"\n'
        "    return payment.status\n",
    )
    addition = _enrich(tmp_path, system_id)
    kinds = {a.payload["access_kind"] for a in _field_accesses(addition)}  # type: ignore[attr-defined]
    assert kinds == {"READ", "WRITE"}
    assert _unresolved(addition) == []


def test_supported__constructor_instance_function_local_import(
    tmp_path: Path, system_id: str
) -> None:
    """The real bug: a function-local import of the model class was
    invisible to instance-type tracking entirely."""
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "payments/service.py",
        "def process():\n"
        "    from payments.models import Payment\n"
        "    payment = Payment()\n"
        '    payment.status = "captured"\n'
        "    return payment.status\n",
    )
    addition = _enrich(tmp_path, system_id)
    kinds = {a.payload["access_kind"] for a in _field_accesses(addition)}  # type: ignore[attr-defined]
    assert kinds == {"READ", "WRITE"}
    assert _unresolved(addition) == []


def test_supported__constructor_instance_aliased_import(tmp_path: Path, system_id: str) -> None:
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "payments/service.py",
        "from payments.models import Payment as PaymentModel\n\n\n"
        "def process():\n"
        "    payment = PaymentModel()\n"
        "    return payment.status\n",
    )
    addition = _enrich(tmp_path, system_id)
    assert len(_field_accesses(addition)) == 1
    assert _unresolved(addition) == []


# --- SUPPORTED: typed parameter and return-value instance -------------------


def test_supported__typed_parameter_cross_module(tmp_path: Path, system_id: str) -> None:
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "payments/service.py",
        "from payments.models import Payment\n\n\n"
        "def close(payment: Payment) -> str:\n"
        "    return payment.status\n",
    )
    addition = _enrich(tmp_path, system_id)
    assert len(_field_accesses(addition)) == 1
    assert _unresolved(addition) == []


def test_supported__typed_parameter_aliased_import(tmp_path: Path, system_id: str) -> None:
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "payments/service.py",
        "from payments.models import Payment as PaymentModel\n\n\n"
        "def close(payment: PaymentModel) -> str:\n"
        "    return payment.status\n",
    )
    addition = _enrich(tmp_path, system_id)
    assert len(_field_accesses(addition)) == 1
    assert _unresolved(addition) == []


def test_supported__return_value_instance_cross_module(tmp_path: Path, system_id: str) -> None:
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "payments/repository.py",
        "from payments.models import Payment\n\n\n"
        "class PaymentRepository:\n"
        "    def get(self, payment_id) -> Payment:\n"
        "        return Payment()\n",
    )
    _write(
        tmp_path,
        "payments/service.py",
        "from payments.repository import PaymentRepository\n\n\n"
        "def close(repo: PaymentRepository, payment_id) -> str:\n"
        "    payment = repo.get(payment_id)\n"
        "    return payment.status\n",
    )
    addition = _enrich(tmp_path, system_id)
    assert len(_field_accesses(addition)) == 1
    assert _unresolved(addition) == []


def test_supported__constructor_keyword_write_cross_module(tmp_path: Path, system_id: str) -> None:
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "payments/service.py",
        "from payments.models import Payment\n\n\n"
        "def create():\n    return Payment(status='pending')\n",
    )
    addition = _enrich(tmp_path, system_id)
    accesses = _field_accesses(addition)
    assert len(accesses) == 1
    assert accesses[0].payload["access_kind"] == "WRITE"  # type: ignore[attr-defined]
    assert _unresolved(addition) == []


# --- SUPPORTED: self-attribute chain (the second real bug this pass closed) -


def test_supported__self_attribute_chain_constructor_composed(
    tmp_path: Path, system_id: str
) -> None:
    """The real bug: `self._payment = Payment()` in `__init__`, read as
    `self._payment.status` from a *different* method, was completely
    invisible -- no access, no limitation."""
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "payments/service.py",
        "from payments.models import Payment\n\n\n"
        "class Service:\n"
        "    def __init__(self):\n"
        "        self._payment = Payment()\n\n"
        "    def process(self):\n"
        '        self._payment.status = "captured"\n'
        "        return self._payment.status\n",
    )
    addition = _enrich(tmp_path, system_id)
    kinds = {a.payload["access_kind"] for a in _field_accesses(addition)}  # type: ignore[attr-defined]
    assert kinds == {"READ", "WRITE"}
    assert _unresolved(addition) == []


def test_supported__self_attribute_chain_cross_module(tmp_path: Path, system_id: str) -> None:
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "payments/service.py",
        "from payments.models import Payment\n\n\n"
        "class Service:\n"
        "    def __init__(self):\n"
        "        self._payment = Payment()\n\n"
        "    def status(self):\n"
        "        return self._payment.status\n",
    )
    addition = _enrich(tmp_path, system_id)
    assert len(_field_accesses(addition)) == 1
    assert _unresolved(addition) == []


def test_supported__self_attribute_chain_aliased_import(tmp_path: Path, system_id: str) -> None:
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "payments/service.py",
        "from payments.models import Payment as PaymentModel\n\n\n"
        "class Service:\n"
        "    def __init__(self):\n"
        "        self._payment = PaymentModel()\n\n"
        "    def status(self):\n"
        "        return self._payment.status\n",
    )
    addition = _enrich(tmp_path, system_id)
    assert len(_field_accesses(addition)) == 1
    assert _unresolved(addition) == []


# --- Correctly silent: adversarial and deliberately bounded cases -----------


def test_adversarial__unrelated_self_attribute_is_never_guessed(
    tmp_path: Path, system_id: str
) -> None:
    """A class with one known self-attribute must not cause an unrelated
    `self.<other_attr>.field` -- never assigned in `__init__` -- to be
    guessed at just because the class has *a* known attribute."""
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "payments/service.py",
        "from payments.models import Payment\n\n\n"
        "class Service:\n"
        "    def __init__(self):\n"
        "        self._payment = Payment()\n\n"
        "    def process(self):\n"
        '        self._unrelated.status = "x"\n',
    )
    addition = _enrich(tmp_path, system_id)
    assert _field_accesses(addition) == []
    assert _unresolved(addition) == []


def test_bounded__self_attribute_assigned_outside_init_stays_unresolved(
    tmp_path: Path, system_id: str
) -> None:
    """Scoped to `__init__` alone, deliberately -- matching the general
    resolver's own `SELF_ATTRIBUTE` restriction. A wider scan for
    self-attribute assignments anywhere is a further, unattempted
    widening, not a bug."""
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "payments/service.py",
        "from payments.models import Payment\n\n\n"
        "class Service:\n"
        "    def setup(self):\n"
        "        self._payment = Payment()\n\n"
        "    def process(self):\n"
        '        self._payment.status = "captured"\n',
    )
    addition = _enrich(tmp_path, system_id)
    assert _field_accesses(addition) == []
    assert _unresolved(addition) == []


def test_bounded__multi_hop_nested_attribute_stays_unresolved(
    tmp_path: Path, system_id: str
) -> None:
    """`self._holder.payment.status` -- two hops beyond `self` -- is a
    further widening beyond `SELF_ATTRIBUTE`'s own one-attribute-level
    scope, deliberately not attempted here."""
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "payments/service.py",
        "from payments.models import Payment\n\n\n"
        "class Holder:\n"
        "    def __init__(self):\n"
        "        self.payment = Payment()\n\n\n"
        "class Service:\n"
        "    def __init__(self):\n"
        "        self._holder = Holder()\n\n"
        "    def process(self):\n"
        '        self._holder.payment.status = "captured"\n',
    )
    addition = _enrich(tmp_path, system_id)
    assert _field_accesses(addition) == []
    assert _unresolved(addition) == []


# --- Open gaps: classified, deliberately not fixed this pass ----------------


def test_open_gap__module_level_singleton_instance_field_access(
    tmp_path: Path, system_id: str
) -> None:
    """Pinned, not fixed: `payment = Payment()` at module scope, imported
    and touched from another file. The general `CALLS` resolver closed the
    equivalent gap for a service/client singleton (a much more common
    real-world shape than a process-wide ORM row instance); this adapter's
    own, independent field-access mechanism was not widened to match in
    this pass. If this ever starts resolving, update this test rather than
    deleting it -- that is the point of pinning it."""
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "payments/singleton.py",
        "from payments.models import Payment\n\npayment = Payment()\n",
    )
    _write(
        tmp_path,
        "payments/service.py",
        "from payments.singleton import payment\n\n\ndef process():\n    return payment.status\n",
    )
    addition = _enrich(tmp_path, system_id)
    assert _field_accesses(addition) == []
    assert _unresolved(addition) == []


def test_open_gap__dynamic_getattr_through_a_self_attribute_base(
    tmp_path: Path, system_id: str
) -> None:
    """Pinned, not fixed: `getattr(self._payment, "status")` -- a literal
    field name, which `DYNAMIC_ATTRIBUTE_ACCESS` exists to disclose for a
    bare-name base (`getattr(x, "status")`) -- is silently dropped instead,
    because `_check_dynamic_attribute_call`'s own guard requires its first
    argument to be a plain `ast.Name`, not an `ast.Attribute`. Pinned here
    so a future widening of that guard is a deliberate change, not a
    silent one."""
    _write(tmp_path, "payments/models.py", _MODEL)
    _write(
        tmp_path,
        "payments/service.py",
        "from payments.models import Payment\n\n\n"
        "class Service:\n"
        "    def __init__(self):\n"
        "        self._payment = Payment()\n\n"
        "    def process(self):\n"
        "        return getattr(self._payment, 'status')\n",
    )
    addition = _enrich(tmp_path, system_id)
    assert _field_accesses(addition) == []
    assert _unresolved(addition) == []
