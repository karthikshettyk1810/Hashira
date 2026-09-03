"""The Python AST extractor: source in, Observations out, nothing invented.

Every assertion here is checked against the *raw* `payload` dict an adapter
produces — this is Stage 1 of the pipeline (see `normalizer.py` for Stage 2),
so there are no `Entity`/`Relationship` objects to inspect yet, by design.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hashira.adapters.python.extractor import extract_file, module_qualified_name
from hashira.core.ids import IDPrefix, new_id


@pytest.fixture
def system_id() -> str:
    return new_id(IDPrefix.SYSTEM)


def _write(tmp_path: Path, relpath: str, source: str) -> Path:
    path = tmp_path / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return path


def _extract(tmp_path: Path, relpath: str, source: str, system_id: str):  # type: ignore[no-untyped-def]
    file = _write(tmp_path, relpath, source)
    return extract_file(file, import_root=tmp_path, system_id=system_id, revision="rev1")


def _by_kind(observations, kind: str) -> list:  # type: ignore[no-untyped-def]
    return [obs for obs in observations if obs.kind == kind]


# --- module_qualified_name ----------------------------------------------


def test_module_qualified_name_strips_init(tmp_path: Path) -> None:
    file = _write(tmp_path, "shop/__init__.py", "")
    assert module_qualified_name(file, tmp_path) == "shop"


def test_module_qualified_name_joins_path_parts(tmp_path: Path) -> None:
    file = _write(tmp_path, "shop/payments.py", "")
    assert module_qualified_name(file, tmp_path) == "shop.payments"


def test_module_qualified_name_needs_no_init_for_namespace_packages(tmp_path: Path) -> None:
    file = _write(tmp_path, "shop/sub/payments.py", "")
    assert module_qualified_name(file, tmp_path) == "shop.sub.payments"


# --- module + symbol extraction -------------------------------------------


def test_extracts_a_module_observation(tmp_path: Path, system_id: str) -> None:
    result = _extract(tmp_path, "shop/payments.py", "x = 1\n", system_id)
    assert result.errors == []
    modules = _by_kind(result.observations, "python.module")
    assert len(modules) == 1
    assert modules[0].payload == {"qualified_name": "shop.payments", "file": "shop/payments.py"}


def test_extracts_class_and_method_symbols(tmp_path: Path, system_id: str) -> None:
    source = """
class PaymentService:
    def process(self, amount):
        return amount
"""
    result = _extract(tmp_path, "shop/payments.py", source, system_id)
    symbols = {
        s.payload["qualified_name"]: s.payload
        for s in _by_kind(result.observations, "python.symbol")
    }
    assert symbols["shop.payments.PaymentService"]["kind"] == "class"
    assert symbols["shop.payments.PaymentService"]["parent_kind"] == "module"
    assert symbols["shop.payments.PaymentService.process"]["kind"] == "method"
    assert symbols["shop.payments.PaymentService.process"]["parent_kind"] == "class"
    assert symbols["shop.payments.PaymentService.process"]["parent_qualified_name"] == (
        "shop.payments.PaymentService"
    )


def test_top_level_function_is_kind_function_not_method(tmp_path: Path, system_id: str) -> None:
    result = _extract(
        tmp_path, "shop/notifications.py", "def send_receipt(order):\n    pass\n", system_id
    )
    symbols = _by_kind(result.observations, "python.symbol")
    assert symbols[0].payload["kind"] == "function"


def test_async_function_kind(tmp_path: Path, system_id: str) -> None:
    result = _extract(
        tmp_path, "shop/notifications.py", "async def send_receipt(order):\n    pass\n", system_id
    )
    symbols = _by_kind(result.observations, "python.symbol")
    assert symbols[0].payload["kind"] == "async_function"


def test_source_locations_are_recorded(tmp_path: Path, system_id: str) -> None:
    source = "class Foo:\n    def bar(self):\n        pass\n"
    result = _extract(tmp_path, "shop/foo.py", source, system_id)
    symbols = {
        s.payload["qualified_name"]: s.payload
        for s in _by_kind(result.observations, "python.symbol")
    }
    assert symbols["shop.foo.Foo"]["line_start"] == 1
    assert symbols["shop.foo.Foo.bar"]["line_start"] == 2


def test_decorators_are_captured_as_text_not_interpreted(tmp_path: Path, system_id: str) -> None:
    source = """
class Foo:
    @staticmethod
    @some_decorator(1, 2)
    def bar():
        pass
"""
    result = _extract(tmp_path, "shop/foo.py", source, system_id)
    symbols = {
        s.payload["qualified_name"]: s.payload
        for s in _by_kind(result.observations, "python.symbol")
    }
    assert symbols["shop.foo.Foo.bar"]["decorators"] == ["staticmethod", "some_decorator(1, 2)"]


def test_nested_function_is_not_duplicated_and_not_a_method(tmp_path: Path, system_id: str) -> None:
    source = """
def outer():
    def inner():
        pass
    return inner
"""
    result = _extract(tmp_path, "shop/foo.py", source, system_id)
    symbols = _by_kind(result.observations, "python.symbol")
    names = [s.payload["qualified_name"] for s in symbols]
    assert names == ["shop.foo.outer", "shop.foo.outer.inner"]
    inner = next(s for s in symbols if s.payload["name"] == "inner")
    assert inner.payload["kind"] == "function"


# --- imports -----------------------------------------------------------------


def test_extracts_relative_and_absolute_imports(tmp_path: Path, system_id: str) -> None:
    source = """
from .payments import PaymentService
from django.db import models
import os
"""
    result = _extract(tmp_path, "shop/checkout.py", source, system_id)
    imports = {
        i.payload["bound_name"]: i.payload["target"]
        for i in _by_kind(result.observations, "python.import")
    }
    assert imports == {
        "PaymentService": "shop.payments.PaymentService",
        "models": "django.db.models",
        "os": "os",
    }


# --- calls: every resolution kind -----------------------------------------


def test_call_resolution_kinds(tmp_path: Path, system_id: str) -> None:
    source = """
from .payments import PaymentService


class CheckoutService:
    def checkout(self, order):
        payment = PaymentService()
        result = payment.process(order.total)
        self.log(result)
        something_unknown.foo()
        return result

    def log(self, result):
        pass
"""
    result = _extract(tmp_path, "shop/checkout.py", source, system_id)
    calls = {
        c.payload["callee_expr"]: c.payload for c in _by_kind(result.observations, "python.call")
    }

    assert calls["PaymentService"]["resolution"] == "IMPORT"
    assert calls["PaymentService"]["resolved_qualified_name"] == "shop.payments.PaymentService"

    assert calls["payment.process"]["resolution"] == "LOCAL_INSTANCE"
    assert (
        calls["payment.process"]["resolved_qualified_name"]
        == "shop.payments.PaymentService.process"
    )

    assert calls["self.log"]["resolution"] == "SELF"
    assert calls["self.log"]["resolved_qualified_name"] == "shop.checkout.CheckoutService.log"

    assert calls["something_unknown.foo"]["resolution"] == "UNRESOLVED"
    assert calls["something_unknown.foo"]["resolved_qualified_name"] is None


def test_module_local_class_constructor_resolves_without_import(
    tmp_path: Path, system_id: str
) -> None:
    source = """
class Helper:
    def run(self):
        pass


def use_it():
    h = Helper()
    h.run()
"""
    result = _extract(tmp_path, "shop/util.py", source, system_id)
    calls = {
        c.payload["callee_expr"]: c.payload for c in _by_kind(result.observations, "python.call")
    }
    assert calls["Helper"]["resolution"] == "MODULE_LOCAL"
    assert calls["Helper"]["resolved_qualified_name"] == "shop.util.Helper"
    assert calls["h.run"]["resolution"] == "LOCAL_INSTANCE"
    assert calls["h.run"]["resolved_qualified_name"] == "shop.util.Helper.run"


def test_call_inside_an_if_block_is_still_attributed_to_the_enclosing_function(
    tmp_path: Path, system_id: str
) -> None:
    source = """
from .notifications import send_receipt


def checkout(order, result):
    if result.success:
        send_receipt(order)
"""
    result = _extract(tmp_path, "shop/checkout.py", source, system_id)
    calls = _by_kind(result.observations, "python.call")
    matching = [c for c in calls if c.payload["callee_expr"] == "send_receipt"]
    assert len(matching) == 1
    assert matching[0].payload["caller_qualified_name"] == "shop.checkout.checkout"


def test_calls_are_not_duplicated_in_a_method_with_nested_control_flow(
    tmp_path: Path, system_id: str
) -> None:
    """Regression guard: an earlier version of the walker double-counted every
    call inside a function whose body mixed nested defs with plain statements."""
    source = """
class Foo:
    def bar(self):
        self.a()
        if True:
            self.b()
        for _ in range(3):
            self.c()
"""
    result = _extract(tmp_path, "shop/foo.py", source, system_id)
    calls = [c.payload["callee_expr"] for c in _by_kind(result.observations, "python.call")]
    assert sorted(calls) == ["range", "self.a", "self.b", "self.c"]


# --- inheritance ---------------------------------------------------------


def test_inheritance_resolution(tmp_path: Path, system_id: str) -> None:
    source = """
from django.db import models


class Payment(models.Model):
    pass
"""
    result = _extract(tmp_path, "shop/models.py", source, system_id)
    bases = _by_kind(result.observations, "python.inheritance")
    assert len(bases) == 1
    assert bases[0].payload["class_qualified_name"] == "shop.models.Payment"
    assert bases[0].payload["resolution"] == "IMPORT"
    assert bases[0].payload["resolved_qualified_name"] == "django.db.models.Model"


def test_unresolved_base_class_stays_unresolved(tmp_path: Path, system_id: str) -> None:
    source = "class Foo(SomeMixinNoOneImported):\n    pass\n"
    result = _extract(tmp_path, "shop/foo.py", source, system_id)
    bases = _by_kind(result.observations, "python.inheritance")
    assert bases[0].payload["resolution"] == "UNRESOLVED"
    assert bases[0].payload["resolved_qualified_name"] is None


# --- evidence and provenance -----------------------------------------------


def test_every_observation_has_matching_evidence(tmp_path: Path, system_id: str) -> None:
    source = (
        "class Foo:\n    def bar(self):\n        self.baz()\n    def baz(self):\n        pass\n"
    )
    result = _extract(tmp_path, "shop/foo.py", source, system_id)
    evidence_ids = {ev.id for ev in result.evidence}
    for obs in result.observations:
        assert len(obs.evidence_ids) == 1
        assert obs.evidence_ids[0] in evidence_ids
    assert all(ev.source.provider == "python-ast" for ev in result.evidence)


def test_syntax_error_is_reported_not_raised(tmp_path: Path, system_id: str) -> None:
    result = _extract(tmp_path, "shop/broken.py", "def f(:\n    pass\n", system_id)
    assert result.observations == []
    assert result.evidence == []
    assert len(result.errors) == 1
    assert "broken.py" in result.errors[0]
