"""Import-binding and expression resolution: pure, syntax-only, no files."""

from __future__ import annotations

import ast

import pytest

from hashira.adapters.python.resolve import (
    ResolutionContext,
    bindings_for_import,
    bindings_for_import_from,
    resolve_expr,
)


def _parse_stmt(src: str) -> ast.stmt:
    return ast.parse(src).body[0]


def _parse_expr(src: str) -> ast.expr:
    return ast.parse(src, mode="eval").body


# --- import bindings -------------------------------------------------------


def test_plain_import_binds_the_top_level_package() -> None:
    node = _parse_stmt("import a.b.c")
    assert isinstance(node, ast.Import)
    bindings = bindings_for_import(node)
    assert len(bindings) == 1
    assert bindings[0].bound_name == "a"
    assert bindings[0].target == "a"
    assert bindings[0].is_module_import


def test_aliased_import_binds_the_full_path_to_the_alias() -> None:
    node = _parse_stmt("import a.b.c as abc")
    assert isinstance(node, ast.Import)
    binding = bindings_for_import(node)[0]
    assert binding.bound_name == "abc"
    assert binding.target == "a.b.c"


def test_multiple_names_in_one_import_statement() -> None:
    node = _parse_stmt("import os, sys as system")
    assert isinstance(node, ast.Import)
    bindings = bindings_for_import(node)
    assert [(b.bound_name, b.target) for b in bindings] == [("os", "os"), ("system", "sys")]


@pytest.mark.parametrize(
    ("module_qn", "src", "expected"),
    [
        (
            "shop.checkout",
            "from .payments import PaymentService",
            [("PaymentService", "shop.payments.PaymentService")],
        ),
        ("shop.checkout", "from . import payments", [("payments", "shop.payments")]),
        ("shop.sub.checkout", "from ..other import X", [("X", "shop.other.X")]),
        ("shop.checkout", "from django.db import models", [("models", "django.db.models")]),
        (
            "shop.checkout",
            "from .payments import PaymentService as PS",
            [("PS", "shop.payments.PaymentService")],
        ),
    ],
)
def test_bindings_for_import_from(
    module_qn: str, src: str, expected: list[tuple[str, str]]
) -> None:
    node = _parse_stmt(src)
    assert isinstance(node, ast.ImportFrom)
    bindings = bindings_for_import_from(module_qn, node)
    assert [(b.bound_name, b.target) for b in bindings] == expected


def test_relative_import_inside_a_package_init_anchors_on_itself_not_its_parent() -> None:
    """A real-repository finding: `common/kafka/__init__.py` (qualified name
    `common.kafka` -- `module_qualified_name` already strips `__init__`) is
    itself the package a relative import inside it is relative to. Treating
    it like an ordinary module (dropping one more dotted component, as if
    `common.kafka` were `common.kafka.something`) silently resolves
    `from .publisher import X` to `common.publisher.X` instead of
    `common.kafka.publisher.X` -- wrong, not merely unresolved, since the
    wrong target can coincidentally look plausible."""
    node = _parse_stmt("from .publisher import KafkaEventPublisher")
    assert isinstance(node, ast.ImportFrom)
    bindings = bindings_for_import_from("common.kafka", node, is_package_init=True)
    assert [(b.bound_name, b.target) for b in bindings] == [
        ("KafkaEventPublisher", "common.kafka.publisher.KafkaEventPublisher")
    ]


def test_relative_import_inside_a_package_init_one_level_up() -> None:
    """`from .. import x` inside `common/kafka/__init__.py` walks one
    package above `common.kafka` itself, i.e. `common` -- not two levels
    above it, the way the ordinary-module formula would compute."""
    node = _parse_stmt("from .. import shared")
    assert isinstance(node, ast.ImportFrom)
    bindings = bindings_for_import_from("common.kafka", node, is_package_init=True)
    assert [(b.bound_name, b.target) for b in bindings] == [("shared", "common.shared")]


def test_relative_import_inside_an_ordinary_module_is_unaffected_by_the_flag() -> None:
    """`is_package_init=False` (the default) must reproduce the exact,
    already-correct behavior for a regular module -- this fix must not
    change resolution for the overwhelmingly common non-`__init__.py` case."""
    node = _parse_stmt("from .payments import PaymentService")
    assert isinstance(node, ast.ImportFrom)
    bindings = bindings_for_import_from("shop.checkout", node, is_package_init=False)
    assert [(b.bound_name, b.target) for b in bindings] == [
        ("PaymentService", "shop.payments.PaymentService")
    ]


def test_star_import_is_skipped_not_guessed() -> None:
    node = _parse_stmt("from os.path import *")
    assert isinstance(node, ast.ImportFrom)
    assert bindings_for_import_from("shop.checkout", node) == []


# --- resolve_expr ------------------------------------------------------------


def test_resolves_a_module_local_class_constructor() -> None:
    ctx = ResolutionContext(module_qualified_name="shop.payments", module_locals={"PaymentService"})
    result = resolve_expr(_parse_expr("PaymentService"), ctx)
    assert result.resolution == "MODULE_LOCAL"
    assert result.qualified_name == "shop.payments.PaymentService"


def test_resolves_an_imported_name() -> None:
    ctx = ResolutionContext(
        module_qualified_name="shop.checkout",
        imports={"PaymentService": "shop.payments.PaymentService"},
    )
    result = resolve_expr(_parse_expr("PaymentService"), ctx)
    assert result.resolution == "IMPORT"
    assert result.qualified_name == "shop.payments.PaymentService"


def test_resolves_an_imported_module_with_attribute_suffix() -> None:
    ctx = ResolutionContext(
        module_qualified_name="shop.models", imports={"models": "django.db.models"}
    )
    result = resolve_expr(_parse_expr("models.Model"), ctx)
    assert result.resolution == "IMPORT"
    assert result.qualified_name == "django.db.models.Model"


def test_resolves_self_attribute_one_level_only() -> None:
    ctx = ResolutionContext(
        module_qualified_name="shop.payments",
        enclosing_class_qualified_name="shop.payments.PaymentService",
    )
    result = resolve_expr(_parse_expr("self.validate"), ctx)
    assert result.resolution == "SELF"
    assert result.qualified_name == "shop.payments.PaymentService.validate"


def test_self_attribute_chain_deeper_than_one_level_is_unresolved() -> None:
    ctx = ResolutionContext(
        module_qualified_name="shop.payments",
        enclosing_class_qualified_name="shop.payments.PaymentService",
    )
    result = resolve_expr(_parse_expr("self.client.charge"), ctx)
    assert result.resolution == "UNRESOLVED"
    assert result.qualified_name is None


def test_resolves_a_known_self_attribute_method_call() -> None:
    """`self.<attr>.<method>()` resolves when `<attr>`'s type is already
    known (populated from `__init__` by `extractor.py`'s
    `_self_attribute_types`, mirrored here directly against `resolve_expr`)
    -- the gap the real-repository pilot found (`self._notification_service
    .send_notifications(...)`, docs/ROADMAP.md's Phase 2 entry)."""
    ctx = ResolutionContext(
        module_qualified_name="shop.checkout",
        enclosing_class_qualified_name="shop.checkout.CheckoutView",
        self_attribute_types={"_payments": "shop.payments.PaymentService"},
    )
    result = resolve_expr(_parse_expr("self._payments.process"), ctx)
    assert result.resolution == "SELF_ATTRIBUTE"
    assert result.qualified_name == "shop.payments.PaymentService.process"


def test_unknown_self_attribute_chain_stays_unresolved() -> None:
    """A same-shape chain through an attribute `__init__` never assigned a
    known type for -- `self.foo.bar()` must never be guessed just because
    *some other* attribute on this class resolved."""
    ctx = ResolutionContext(
        module_qualified_name="shop.checkout",
        enclosing_class_qualified_name="shop.checkout.CheckoutView",
        self_attribute_types={"_payments": "shop.payments.PaymentService"},
    )
    result = resolve_expr(_parse_expr("self._unrelated.charge"), ctx)
    assert result.resolution == "UNRESOLVED"
    assert result.qualified_name is None


def test_resolves_a_local_instance_method_call() -> None:
    ctx = ResolutionContext(
        module_qualified_name="shop.checkout",
        local_instance_types={"payment": "shop.payments.PaymentService"},
    )
    result = resolve_expr(_parse_expr("payment.process"), ctx)
    assert result.resolution == "LOCAL_INSTANCE"
    assert result.qualified_name == "shop.payments.PaymentService.process"


def test_local_instance_takes_priority_over_a_same_named_import() -> None:
    """The narrower, more-local signal should win over a broader one."""
    ctx = ResolutionContext(
        module_qualified_name="shop.checkout",
        imports={"payment": "somewhere.else.payment"},
        local_instance_types={"payment": "shop.payments.PaymentService"},
    )
    result = resolve_expr(_parse_expr("payment.process"), ctx)
    assert result.qualified_name == "shop.payments.PaymentService.process"


def test_unknown_name_is_unresolved_not_guessed() -> None:
    ctx = ResolutionContext(module_qualified_name="shop.checkout")
    result = resolve_expr(_parse_expr("something_unknown.foo()"), ctx)
    assert result.resolution == "UNRESOLVED"
    assert result.qualified_name is None
    assert result.text == "something_unknown.foo()"


def test_non_name_call_target_is_unresolved() -> None:
    """e.g. `(a or b)()` -- nothing here to walk down to a leftmost Name."""
    ctx = ResolutionContext(module_qualified_name="shop.checkout")
    result = resolve_expr(_parse_expr("(a or b)"), ctx)
    assert result.resolution == "UNRESOLVED"
