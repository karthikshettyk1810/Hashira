"""SQLAlchemyAdapter: declarative-base recognition (both styles), table/
column/foreign-key detection, built on top of what PythonAdapter already
extracted -- never re-derived independently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hashira.adapters.python import PythonAdapter
from hashira.adapters.python.discovery import discover_python_files
from hashira.adapters.sqlalchemy import SQLAlchemyAdapter
from hashira.core import Observation, Origin
from hashira.core.ids import IDPrefix, new_id
from hashira.ports.adapters import ExtractionResult


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


def _by_kind(result: ExtractionResult, kind: str) -> list:  # type: ignore[type-arg]
    return [o for o in result.observations if o.kind == kind]


# --- detect() --------------------------------------------------------------


def test_detect_true_with_sqlalchemy_in_requirements(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("sqlalchemy\n")
    assert SQLAlchemyAdapter().detect(tmp_path)


def test_detect_false_without_any_manifest_mentioning_sqlalchemy(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("django\n")
    assert not SQLAlchemyAdapter().detect(tmp_path)


# --- declarative base recognition, both real-world styles -------------------


def test_factory_style_declarative_base_is_recognized(tmp_path: Path, system_id: str) -> None:
    _write(
        tmp_path,
        "models.py",
        "from sqlalchemy import Column, Integer\n"
        "from sqlalchemy.orm import declarative_base\n\n"
        "Base = declarative_base()\n\n\n"
        "class Account(Base):\n"
        '    __tablename__ = "accounts"\n\n'
        "    id = Column(Integer, primary_key=True)\n",
    )
    base = _base(tmp_path, system_id)
    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    models = _by_kind(addition, "sqlalchemy.model")
    assert len(models) == 1
    assert models[0].payload["table_name"] == "accounts"


def test_class_style_declarative_base_is_recognized(tmp_path: Path, system_id: str) -> None:
    _write(
        tmp_path,
        "models.py",
        "from sqlalchemy import Column, Integer\n"
        "from sqlalchemy.orm import DeclarativeBase, mapped_column\n\n\n"
        "class Base(DeclarativeBase):\n    pass\n\n\n"
        "class Account(Base):\n"
        '    __tablename__ = "accounts"\n\n'
        "    id = mapped_column(Integer, primary_key=True)\n",
    )
    base = _base(tmp_path, system_id)
    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    models = _by_kind(addition, "sqlalchemy.model")
    assert len(models) == 1
    # Account's *direct* base is Base, not DeclarativeBase itself -- Base
    # is what's recorded, since that's what the source actually says.
    assert models[0].payload["base_qualified_name"] == "models.Base"


def test_a_multi_level_base_hierarchy_still_resolves(tmp_path: Path, system_id: str) -> None:
    """`class TimestampedBase(Base)` then `class Account(TimestampedBase)` --
    the fixpoint expansion in `_discover_declarative_bases` must reach two
    levels deep, not just one."""
    _write(
        tmp_path,
        "models.py",
        "from sqlalchemy import Column, Integer\n"
        "from sqlalchemy.orm import declarative_base\n\n"
        "Base = declarative_base()\n\n\n"
        "class TimestampedBase(Base):\n    __abstract__ = True\n\n\n"
        "class Account(TimestampedBase):\n"
        '    __tablename__ = "accounts"\n\n'
        "    id = Column(Integer, primary_key=True)\n",
    )
    base = _base(tmp_path, system_id)
    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    models = _by_kind(addition, "sqlalchemy.model")
    assert {m.payload["class_qualified_name"] for m in models} == {"models.Account"}


def test_an_unrelated_base_named_base_is_not_recognized(tmp_path: Path, system_id: str) -> None:
    """The whole point of known_symbols.py: a class extending something
    that merely happens to be named `Base` -- but is not actually rooted in
    SQLAlchemy's declarative machinery -- must not be misclassified."""
    _write(
        tmp_path,
        "models.py",
        'class Base:\n    pass\n\n\nclass Account(Base):\n    __tablename__ = "accounts"\n',
    )
    base = _base(tmp_path, system_id)
    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    assert _by_kind(addition, "sqlalchemy.model") == []


def test_a_declarative_base_with_no_tablename_is_not_a_model(
    tmp_path: Path, system_id: str
) -> None:
    """`Base` itself -- or any abstract mixin with no `__tablename__` --
    must not be misclassified as a table."""
    _write(
        tmp_path,
        "models.py",
        "from sqlalchemy.orm import declarative_base\n\nBase = declarative_base()\n",
    )
    base = _base(tmp_path, system_id)
    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    assert _by_kind(addition, "sqlalchemy.model") == []


# --- column / primary key / foreign key detection ---------------------------


def test_columns_and_primary_key_are_detected(tmp_path: Path, system_id: str) -> None:
    _write(
        tmp_path,
        "models.py",
        "from sqlalchemy import Column, Integer, String\n"
        "from sqlalchemy.orm import declarative_base\n\n"
        "Base = declarative_base()\n\n\n"
        "class Account(Base):\n"
        '    __tablename__ = "accounts"\n\n'
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String(100))\n",
    )
    base = _base(tmp_path, system_id)
    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    columns = {c.payload["field_name"]: c.payload for c in _by_kind(addition, "sqlalchemy.column")}
    assert set(columns) == {"id", "name"}
    assert columns["id"]["is_primary_key"] is True
    assert columns["name"]["is_primary_key"] is False


def test_foreign_key_target_is_captured(tmp_path: Path, system_id: str) -> None:
    _write(
        tmp_path,
        "models.py",
        "from sqlalchemy import Column, ForeignKey, Integer\n"
        "from sqlalchemy.orm import declarative_base\n\n"
        "Base = declarative_base()\n\n\n"
        "class Account(Base):\n"
        '    __tablename__ = "accounts"\n\n'
        "    id = Column(Integer, primary_key=True)\n\n\n"
        "class Payment(Base):\n"
        '    __tablename__ = "payments"\n\n'
        "    id = Column(Integer, primary_key=True)\n"
        '    account_id = Column(Integer, ForeignKey("accounts.id"))\n',
    )
    base = _base(tmp_path, system_id)
    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    columns = {c.payload["field_name"]: c.payload for c in _by_kind(addition, "sqlalchemy.column")}
    assert columns["account_id"]["foreign_key_target"] == "accounts.id"
    assert columns["id"]["foreign_key_target"] is None


def test_a_locally_defined_foreign_key_lookalike_is_not_mistaken_for_sqlalchemys(
    tmp_path: Path, system_id: str
) -> None:
    """Evidence-based, not name-based: a call merely named `ForeignKey`
    that isn't SQLAlchemy's must not be treated as one."""
    _write(
        tmp_path,
        "models.py",
        "from sqlalchemy import Column, Integer\n"
        "from sqlalchemy.orm import declarative_base\n\n"
        "Base = declarative_base()\n\n\n"
        "def ForeignKey(x):\n"
        "    return x\n\n\n"
        "class Payment(Base):\n"
        '    __tablename__ = "payments"\n\n'
        "    id = Column(Integer, primary_key=True)\n"
        '    account_id = Column(Integer, ForeignKey("accounts.id"))\n',
    )
    base = _base(tmp_path, system_id)
    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    columns = {c.payload["field_name"]: c.payload for c in _by_kind(addition, "sqlalchemy.column")}
    assert columns["account_id"]["foreign_key_target"] is None


def test_relationship_construct_is_not_parsed(tmp_path: Path, system_id: str) -> None:
    """Deep relationship inference (`relationship(...)`) is explicitly out
    of scope for v0.1 -- it must not produce a column or crash."""
    _write(
        tmp_path,
        "models.py",
        "from sqlalchemy import Column, Integer\n"
        "from sqlalchemy.orm import declarative_base, relationship\n\n"
        "Base = declarative_base()\n\n\n"
        "class Account(Base):\n"
        '    __tablename__ = "accounts"\n\n'
        "    id = Column(Integer, primary_key=True)\n"
        '    payments = relationship("Payment", back_populates="account")\n',
    )
    base = _base(tmp_path, system_id)
    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    columns = {c.payload["field_name"] for c in _by_kind(addition, "sqlalchemy.column")}
    assert columns == {"id"}
    assert addition.errors == []


# --- field access detection -------------------------------------------------


def test_field_read_and_write_are_detected(tmp_path: Path, system_id: str) -> None:
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
    _write(
        tmp_path,
        "payments/service.py",
        "from .models import Payment\n\n\n"
        "class PaymentService:\n"
        "    def process(self, amount):\n"
        "        payment = Payment()\n"
        '        payment.status = "pending"\n'
        "        return payment.status\n",
    )
    base = _base(tmp_path, system_id)
    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    accesses = _by_kind(addition, "sqlalchemy.field_access")
    kinds = {a.payload["access_kind"] for a in accesses}
    assert kinds == {"READ", "WRITE"}
    assert all(a.payload["column_qualified_name"] == "payments.status" for a in accesses)


def test_field_access_on_a_non_model_instance_is_not_detected(
    tmp_path: Path, system_id: str
) -> None:
    _write(
        tmp_path,
        "service.py",
        "class Result:\n    def __init__(self):\n        self.status = 'ok'\n\n\n"
        "class Checker:\n    def run(self):\n"
        "        r = Result()\n        return r.status\n",
    )
    base = _base(tmp_path, system_id)
    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    assert _by_kind(addition, "sqlalchemy.field_access") == []


def test_addition_does_not_echo_the_base_observations(tmp_path: Path, system_id: str) -> None:
    _write(tmp_path, "models.py", "class Plain:\n    pass\n")
    base = _base(tmp_path, system_id)
    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    addition_kinds = {o.kind for o in addition.observations}
    assert "python.symbol" not in addition_kinds
    assert "python.module" not in addition_kinds


def test_no_sqlalchemy_shaped_code_produces_no_observations(tmp_path: Path, system_id: str) -> None:
    _write(tmp_path, "plain.py", "def add(a, b):\n    return a + b\n")
    base = _base(tmp_path, system_id)
    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    assert addition.observations == []
    assert addition.errors == []


# --- typed-parameter provenance (Impact Analysis v0.2's "coverage" widening) -


def test_field_write_via_typed_parameter_is_detected(tmp_path: Path, system_id: str) -> None:
    """`def process(self, payment: Payment)` -- a *second* provenance form,
    alongside `payment = Payment()`; see the module docstring's "typed
    object provenance" entry."""
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
    _write(
        tmp_path,
        "payments/service.py",
        "from .models import Payment\n\n\n"
        "class PaymentService:\n"
        "    def mark_captured(self, payment: Payment) -> str:\n"
        '        payment.status = "captured"\n'
        "        return payment.status\n",
    )
    base = _base(tmp_path, system_id)
    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    accesses = _by_kind(addition, "sqlalchemy.field_access")
    kinds = {a.payload["access_kind"] for a in accesses}
    assert kinds == {"READ", "WRITE"}
    assert all(a.payload["column_qualified_name"] == "payments.status" for a in accesses)
    assert _by_kind(addition, "sqlalchemy.unresolved_field_access") == []


def test_unresolved_access_reported_for_untyped_parameter_matching_a_known_column(
    tmp_path: Path, system_id: str
) -> None:
    """An untyped parameter accessing `.status` -- a name that genuinely is
    a column somewhere -- is recognized as plausibly relevant but not
    resolved, and reported so, rather than silently dropped (see
    `application/impact.py::ImpactCoverage`)."""
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
    _write(
        tmp_path,
        "payments/worker.py",
        "def sync(payment):\n    return payment.status\n",
    )
    base = _base(tmp_path, system_id)
    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    assert _by_kind(addition, "sqlalchemy.field_access") == []
    unresolved = _by_kind(addition, "sqlalchemy.unresolved_field_access")
    assert len(unresolved) == 1
    assert unresolved[0].payload["attribute_name"] == "status"
    assert unresolved[0].payload["accessor_qualified_name"] == "payments.worker.sync"


def test_unresolved_access_not_reported_for_an_attribute_name_no_model_has(
    tmp_path: Path, system_id: str
) -> None:
    """The precision guard: an untyped parameter's attribute access is only
    flagged when the attribute name is a real column *somewhere* -- an
    unrelated same-named attribute on some other, genuinely unrelated
    object (e.g. a third-party SDK's response) must not be miscounted."""
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
    _write(
        tmp_path,
        "payments/gateway.py",
        "def charge(result):\n    return result.reference_number\n",
    )
    base = _base(tmp_path, system_id)
    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    assert _by_kind(addition, "sqlalchemy.unresolved_field_access") == []


def test_capabilities_state_known_limitations() -> None:
    limitations = SQLAlchemyAdapter().capabilities().known_limitations
    assert any("raw sql" in limitation.lower() for limitation in limitations)


# --- declaration-rename detection (identity/declaration_evidence.py's raw material) --


def _git_modified(system_id: str, path: str, old_content: str) -> Observation:
    """A synthetic `git.file_change` observation, exactly the shape
    `GitAdapter` would report for a `MODIFIED` (not renamed) file -- built
    directly here since these tests are about `_detect_declaration_renames`'s
    own comparison logic, not about driving a real Git repository (that is
    `tests/integration/test_identity_evolution.py`'s job)."""
    return Observation(
        system_id=system_id,
        adapter="git@0.1.0",
        origin=Origin.GIT,
        kind="git.file_change",
        payload={
            "status": "MODIFIED",
            "path": path,
            "old_path": None,
            "similarity": None,
            "from_revision": "rev0",
            "to_revision": "rev1",
            "old_content": old_content,
        },
    )


def test_an_unambiguous_column_rename_is_detected(tmp_path: Path, system_id: str) -> None:
    old_content = (
        "from sqlalchemy import Column, Integer, String\n"
        "from sqlalchemy.orm import declarative_base\n\n"
        "Base = declarative_base()\n\n\n"
        "class Payment(Base):\n"
        '    __tablename__ = "payments"\n\n'
        "    id = Column(Integer, primary_key=True)\n"
        "    status = Column(String(20))\n"
    )
    _write(
        tmp_path,
        "models.py",
        old_content.replace("status = Column(String(20))", "state = Column(String(20))"),
    )
    base = _base(tmp_path, system_id)
    base.observations.append(_git_modified(system_id, "models.py", old_content))

    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    renames = _by_kind(addition, "sqlalchemy.declaration_rename")
    assert len(renames) == 1
    assert renames[0].payload["table_name"] == "payments"
    assert renames[0].payload["old_field_name"] == "status"
    assert renames[0].payload["new_field_name"] == "state"
    assert renames[0].payload["shape_matched"] is True


def test_a_type_family_change_alongside_the_rename_is_not_detected(
    tmp_path: Path, system_id: str
) -> None:
    old_content = (
        "from sqlalchemy import Column, Integer, String\n"
        "from sqlalchemy.orm import declarative_base\n\n"
        "Base = declarative_base()\n\n\n"
        "class Payment(Base):\n"
        '    __tablename__ = "payments"\n\n'
        "    id = Column(Integer, primary_key=True)\n"
        "    status = Column(String(20))\n"
    )
    _write(
        tmp_path,
        "models.py",
        old_content.replace("status = Column(String(20))", "state = Column(Integer)"),
    )
    base = _base(tmp_path, system_id)
    base.observations.append(_git_modified(system_id, "models.py", old_content))

    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    assert _by_kind(addition, "sqlalchemy.declaration_rename") == []


def test_a_length_only_change_is_detected_but_not_shape_matched(
    tmp_path: Path, system_id: str
) -> None:
    """The base type family agrees (`String` stays `String`) so the rename
    is still detected, but the shapes are not byte-for-byte identical --
    `shape_matched` reflects that, and `application/indexing.py`'s
    `_extract_declaration_renames` maps it to `LIKELY`, not `CERTAIN`,
    confidence."""
    old_content = (
        "from sqlalchemy import Column, Integer, String\n"
        "from sqlalchemy.orm import declarative_base\n\n"
        "Base = declarative_base()\n\n\n"
        "class Payment(Base):\n"
        '    __tablename__ = "payments"\n\n'
        "    id = Column(Integer, primary_key=True)\n"
        "    status = Column(String(20))\n"
    )
    _write(
        tmp_path,
        "models.py",
        old_content.replace("status = Column(String(20))", "state = Column(String(50))"),
    )
    base = _base(tmp_path, system_id)
    base.observations.append(_git_modified(system_id, "models.py", old_content))

    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    renames = _by_kind(addition, "sqlalchemy.declaration_rename")
    assert len(renames) == 1
    assert renames[0].payload["shape_matched"] is False


def test_ambiguous_multi_column_changes_are_not_detected(tmp_path: Path, system_id: str) -> None:
    """Two columns disappeared, two appeared, in the same table -- which
    corresponds to which is genuinely ambiguous, so nothing is proposed for
    either, matching `identity/git_evidence.py`'s own refusal to guess at a
    name collision."""
    old_content = (
        "from sqlalchemy import Column, Integer, String\n"
        "from sqlalchemy.orm import declarative_base\n\n"
        "Base = declarative_base()\n\n\n"
        "class Payment(Base):\n"
        '    __tablename__ = "payments"\n\n'
        "    id = Column(Integer, primary_key=True)\n"
        "    status = Column(String(20))\n"
        "    note = Column(String(100))\n"
    )
    _write(
        tmp_path,
        "models.py",
        old_content.replace("status = Column(String(20))", "state = Column(String(20))").replace(
            "note = Column(String(100))", "comment = Column(String(100))"
        ),
    )
    base = _base(tmp_path, system_id)
    base.observations.append(_git_modified(system_id, "models.py", old_content))

    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    assert _by_kind(addition, "sqlalchemy.declaration_rename") == []


def test_no_old_content_is_handled_gracefully(tmp_path: Path, system_id: str) -> None:
    _write(
        tmp_path,
        "models.py",
        "from sqlalchemy import Column, Integer, String\n"
        "from sqlalchemy.orm import declarative_base\n\n"
        "Base = declarative_base()\n\n\n"
        "class Payment(Base):\n"
        '    __tablename__ = "payments"\n\n'
        "    id = Column(Integer, primary_key=True)\n"
        "    state = Column(String(20))\n",
    )
    base = _base(tmp_path, system_id)
    base.observations.append(
        Observation(
            system_id=system_id,
            adapter="git@0.1.0",
            origin=Origin.GIT,
            kind="git.file_change",
            payload={
                "status": "MODIFIED",
                "path": "models.py",
                "old_path": None,
                "similarity": None,
                "from_revision": "rev0",
                "to_revision": "rev1",
                "old_content": None,
            },
        )
    )
    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    assert _by_kind(addition, "sqlalchemy.declaration_rename") == []
    assert addition.errors == []


def test_the_class_itself_renamed_in_the_same_commit_gets_no_declaration_rename(
    tmp_path: Path, system_id: str
) -> None:
    """This mechanism is scoped to "the class stayed put, a field within it
    changed" -- if the class's own simple name is also different, there is
    no old counterpart to find it under, and nothing is proposed (a class
    rename is `GIT_RENAME`'s job, not this module's, and stacking both at
    once is explicitly out of scope for this pass)."""
    old_content = (
        "from sqlalchemy import Column, Integer, String\n"
        "from sqlalchemy.orm import declarative_base\n\n"
        "Base = declarative_base()\n\n\n"
        "class Payment(Base):\n"
        '    __tablename__ = "payments"\n\n'
        "    id = Column(Integer, primary_key=True)\n"
        "    status = Column(String(20))\n"
    )
    _write(
        tmp_path,
        "models.py",
        old_content.replace("class Payment(Base):", "class Charge(Base):").replace(
            "status = Column(String(20))", "state = Column(String(20))"
        ),
    )
    base = _base(tmp_path, system_id)
    base.observations.append(_git_modified(system_id, "models.py", old_content))

    addition = SQLAlchemyAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    assert _by_kind(addition, "sqlalchemy.declaration_rename") == []
