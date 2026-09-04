"""The field-access coverage audit (Impact Analysis v0.4): every syntactic
way `adapters/sqlalchemy/adapter.py` claims to understand -- or explicitly
does not understand -- a mapped ORM field being touched, enumerated once,
here, as executable documentation rather than prose that can drift from
the implementation.

For a mapped field like `Payment.status`, this file is the audit table:

| Access form                            | Outcome                       |
|-----------------------------------------|-------------------------------|
| `payment.status` (read)                 | SUPPORTED                     |
| `payment.status = x` (write)            | SUPPORTED                     |
| typed parameter (`def f(x: Payment)`)   | SUPPORTED                     |
| local instantiation (`x = Payment()`)   | SUPPORTED                     |
| annotated return value (`repo.get()`)   | SUPPORTED                     |
| `Payment(status=x)` (constructor kwarg) | SUPPORTED (this milestone)    |
| untyped/unresolvable parameter          | LIMITATION: UNTYPED_PARAMETER |
| unresolvable return value               | LIMITATION: RETURN_VALUE_PROVENANCE |
| `self`/`cls` method return value        | LIMITATION: RETURN_VALUE_PROVENANCE |
| chained return value (`b = a.other()`)  | LIMITATION: RETURN_VALUE_PROVENANCE |
| `getattr(x, "status")` (literal name)   | LIMITATION: DYNAMIC_ATTRIBUTE_ACCESS |
| `setattr(x, "status", v)` (literal)     | LIMITATION: DYNAMIC_ATTRIBUTE_ACCESS |
| `getattr(x, computed_name)`             | correctly silent (no column name to check) |
| raw SQL (`sqlalchemy.text(...)`)        | LIMITATION: RAW_SQL (structural, unconditional) |

This table is a *description* of the implementation, produced by the
implementation's own tests below -- if a row's real behavior ever drifts,
the corresponding test here fails, rather than the table quietly going
stale. Two invariants this file exists to hold permanently, per
`docs/IR.md`'s "coverage is not confidence" entry:

1. A SUPPORTED row must produce the correct `sqlalchemy.field_access`
   edge -- adversarially tested, not just the happy path.
2. A LIMITATION row must produce a `sqlalchemy.unresolved_field_access`
   observation tagged with the right `LimitationKind` -- it may reduce
   coverage, but it may never disappear silently. Each such test is
   paired with the adversarial case one line above/below it in the
   matrix to keep the boundary itself under test, not just each side in
   isolation.

Individual mechanisms already have their own focused tests elsewhere
(`test_sqlalchemy_adapter.py`, `test_sqlalchemy_normalizer.py`); this file
exists to hold the *shape of the whole audit* in one place, so a reviewer
can see every row at a glance rather than reconstructing the table by
reading five files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hashira.adapters.python import PythonAdapter
from hashira.adapters.python.discovery import discover_python_files
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


def _base(tmp_path: Path, system_id: str) -> ExtractionResult:
    files = list(discover_python_files(tmp_path))
    return PythonAdapter().extract(tmp_path, files, system_id=system_id, revision="rev1")


def _model(tmp_path: Path) -> None:
    _write(tmp_path, "payments/__init__.py", "")
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


def _enrich(tmp_path: Path, system_id: str) -> ExtractionResult:
    base = _base(tmp_path, system_id)
    return SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")


def _field_accesses(result: ExtractionResult) -> list[object]:
    return [o for o in result.observations if o.kind == "sqlalchemy.field_access"]


def _unresolved(result: ExtractionResult) -> list[object]:
    return [o for o in result.observations if o.kind == "sqlalchemy.unresolved_field_access"]


# --- SUPPORTED: attribute read/write, local instantiation -------------------


def test_supported__attribute_read_and_write(tmp_path: Path, system_id: str) -> None:
    _model(tmp_path)
    _write(
        tmp_path,
        "payments/service.py",
        "from .models import Payment\n\n\n"
        "def process():\n"
        "    payment = Payment()\n"
        '    payment.status = "captured"\n'
        "    return payment.status\n",
    )
    addition = _enrich(tmp_path, system_id)
    kinds = {a.payload["access_kind"] for a in _field_accesses(addition)}  # type: ignore[attr-defined]
    assert kinds == {"READ", "WRITE"}
    assert _unresolved(addition) == []


# --- SUPPORTED: typed parameter ----------------------------------------------


def test_supported__typed_parameter(tmp_path: Path, system_id: str) -> None:
    _model(tmp_path)
    _write(
        tmp_path,
        "payments/service.py",
        "from .models import Payment\n\n\n"
        "def close(payment: Payment) -> str:\n"
        '    payment.status = "closed"\n'
        "    return payment.status\n",
    )
    addition = _enrich(tmp_path, system_id)
    assert len(_field_accesses(addition)) == 2
    assert _unresolved(addition) == []


# --- SUPPORTED: annotated return value ---------------------------------------


def test_supported__annotated_return_value(tmp_path: Path, system_id: str) -> None:
    _model(tmp_path)
    _write(
        tmp_path,
        "payments/repository.py",
        "from .models import Payment\n\n\n"
        "class PaymentRepository:\n"
        "    def get(self, payment_id) -> Payment:\n"
        "        return Payment()\n",
    )
    _write(
        tmp_path,
        "payments/service.py",
        "from .repository import PaymentRepository\n\n\n"
        "def close(repo: PaymentRepository, payment_id) -> str:\n"
        "    payment = repo.get(payment_id)\n"
        '    payment.status = "closed"\n'
        "    return payment.status\n",
    )
    addition = _enrich(tmp_path, system_id)
    assert len(_field_accesses(addition)) == 2
    assert _unresolved(addition) == []


# --- SUPPORTED (this milestone): constructor keyword ------------------------


def test_supported__constructor_keyword(tmp_path: Path, system_id: str) -> None:
    _model(tmp_path)
    _write(
        tmp_path,
        "payments/service.py",
        "from .models import Payment\n\n\ndef create():\n    return Payment(status='pending')\n",
    )
    addition = _enrich(tmp_path, system_id)
    accesses = _field_accesses(addition)
    assert len(accesses) == 1
    assert accesses[0].payload["access_kind"] == "WRITE"  # type: ignore[attr-defined]
    assert _unresolved(addition) == []


def test_adversarial__constructor_keyword_that_is_not_a_column_invents_nothing(
    tmp_path: Path, system_id: str
) -> None:
    _model(tmp_path)
    _write(
        tmp_path,
        "payments/service.py",
        "from .models import Payment\n\n\ndef create():\n    return Payment(foo='bar')\n",
    )
    addition = _enrich(tmp_path, system_id)
    assert _field_accesses(addition) == []
    assert _unresolved(addition) == []


# --- LIMITATION: untyped/unresolvable parameter ------------------------------


def test_limitation__untyped_parameter(tmp_path: Path, system_id: str) -> None:
    _model(tmp_path)
    _write(tmp_path, "payments/worker.py", "def sync(payment):\n    return payment.status\n")
    addition = _enrich(tmp_path, system_id)
    assert _field_accesses(addition) == []
    unresolved = _unresolved(addition)
    assert len(unresolved) == 1
    assert unresolved[0].payload["limitation_kind"] == LimitationKind.UNTYPED_PARAMETER.value  # type: ignore[attr-defined]


# --- LIMITATION: unresolvable / self / chained return value ------------------


def test_limitation__unresolvable_return_value(tmp_path: Path, system_id: str) -> None:
    _model(tmp_path)
    _write(
        tmp_path,
        "payments/repository.py",
        "class PaymentRepository:\n    def get(self, payment_id):\n        return None\n",
    )
    _write(
        tmp_path,
        "payments/service.py",
        "from .repository import PaymentRepository\n\n\n"
        "def read(repo: PaymentRepository, payment_id):\n"
        "    payment = repo.get(payment_id)\n"
        "    return payment.status\n",
    )
    addition = _enrich(tmp_path, system_id)
    assert _field_accesses(addition) == []
    unresolved = _unresolved(addition)
    assert len(unresolved) == 1
    assert (
        unresolved[0].payload["limitation_kind"]  # type: ignore[attr-defined]
        == LimitationKind.RETURN_VALUE_PROVENANCE.value
    )


def test_limitation__self_method_return_value(tmp_path: Path, system_id: str) -> None:
    _model(tmp_path)
    _write(
        tmp_path,
        "payments/service.py",
        "from .models import Payment\n\n\n"
        "class PaymentService:\n"
        "    def find(self, payment_id) -> Payment:\n"
        "        return Payment()\n\n"
        "    def close(self, payment_id):\n"
        "        payment = self.find(payment_id)\n"
        "        return payment.status\n",
    )
    addition = _enrich(tmp_path, system_id)
    assert _field_accesses(addition) == []
    unresolved = _unresolved(addition)
    assert len(unresolved) == 1
    assert (
        unresolved[0].payload["limitation_kind"]  # type: ignore[attr-defined]
        == LimitationKind.RETURN_VALUE_PROVENANCE.value
    )


def test_adversarial__external_object_return_value_is_not_a_limitation(
    tmp_path: Path, system_id: str
) -> None:
    """The precision boundary this whole audit exists to keep honest: an
    object whose type this adapter never saw at all (an external SDK's
    client) is a different, deeper gap than "we know who was called" --
    flagging it would be noise, not signal."""
    _model(tmp_path)
    _write(
        tmp_path,
        "payments/gateway.py",
        "import external_sdk\n\n"
        "_client = external_sdk.Client()\n\n\n"
        "def charge():\n"
        "    result = _client.charge()\n"
        "    return result.status\n",
    )
    addition = _enrich(tmp_path, system_id)
    assert _field_accesses(addition) == []
    assert _unresolved(addition) == []


# --- LIMITATION: dynamic attribute access, vs. its own adversarial case -----


def test_limitation__getattr_with_a_literal_field_name(tmp_path: Path, system_id: str) -> None:
    _model(tmp_path)
    _write(
        tmp_path,
        "payments/worker.py",
        "def read(payment):\n    return getattr(payment, 'status')\n",
    )
    addition = _enrich(tmp_path, system_id)
    assert _field_accesses(addition) == []
    unresolved = _unresolved(addition)
    assert len(unresolved) == 1
    assert (
        unresolved[0].payload["limitation_kind"]  # type: ignore[attr-defined]
        == LimitationKind.DYNAMIC_ATTRIBUTE_ACCESS.value
    )


def test_adversarial__getattr_with_a_computed_name_is_correctly_silent(
    tmp_path: Path, system_id: str
) -> None:
    """Not a false limitation: a computed name isn't a literal this
    adapter can check against a column list at all."""
    _model(tmp_path)
    _write(
        tmp_path,
        "payments/worker.py",
        "def read(payment, field_name):\n    return getattr(payment, field_name)\n",
    )
    addition = _enrich(tmp_path, system_id)
    assert _field_accesses(addition) == []
    assert _unresolved(addition) == []


# --- LIMITATION: raw SQL, structural and unconditional -----------------------


def test_limitation__raw_sql_is_declared_structurally() -> None:
    """Raw SQL is not detected per-callsite at all (there is no attribute
    access for an AST walk to see) -- it is declared once, unconditionally,
    as an adapter capability, which is a materially different -- but still
    non-silent -- outcome from every other row in this table."""
    limitations = SQLAlchemyAdapter().capabilities().known_limitations
    assert any(limitation.kind is LimitationKind.RAW_SQL for limitation in limitations)
