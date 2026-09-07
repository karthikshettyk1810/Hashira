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


def test_module_qualified_name_falls_back_to_root_name_for_the_roots_own_init(
    tmp_path: Path,
) -> None:
    """A real-repository crash: when the import root itself is a Python
    package (`<root>/__init__.py`, e.g. a Django project's settings package
    doubling as the indexed root), the path-relative name is empty by
    construction. `Entity(name="")` used to crash the entire indexing run;
    falling back to the root directory's own name is real, not fabricated —
    it is genuinely importable as such one level up."""
    file = _write(tmp_path, "__init__.py", "")
    assert module_qualified_name(file, tmp_path) == tmp_path.name


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


def test_relative_import_inside_a_package_init_resolves_against_itself(
    tmp_path: Path, system_id: str
) -> None:
    """End-to-end version of `test_python_resolve.py`'s package-init finding:
    a relative import written in `common/kafka/__init__.py` must resolve
    against `common.kafka` itself, not `common` (its own parent)."""
    result = _extract(
        tmp_path, "common/kafka/__init__.py", "from .publisher import Foo\n", system_id
    )
    imports = {
        i.payload["bound_name"]: i.payload["target"]
        for i in _by_kind(result.observations, "python.import")
    }
    assert imports == {"Foo": "common.kafka.publisher.Foo"}


def test_import_observations_are_tagged_with_their_own_scope(
    tmp_path: Path, system_id: str
) -> None:
    """A `python.import` observation carries `is_module_scope` so a consumer
    that needs *only* a module's own import namespace (`PythonIndex`, shared
    plumbing every framework enricher uses) can filter function-local
    bindings out -- without this, two unrelated functions locally importing
    the same name to different targets would collide in that shared index
    (a real regression `_all_imports`' own widening to function bodies
    introduced; see `_python_index.py`'s own filter)."""
    source = """
from module_z import Thing as ModuleLevelThing


def func_a():
    from module_x import Thing
    return Thing()


def func_b():
    from module_y import Thing
    return Thing()
"""
    result = _extract(tmp_path, "app.py", source, system_id)
    scope_by_target = {
        i.payload["target"]: i.payload["is_module_scope"]
        for i in _by_kind(result.observations, "python.import")
    }
    assert scope_by_target == {
        "module_z.Thing": True,
        "module_x.Thing": False,
        "module_y.Thing": False,
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


def test_constructor_assigned_self_attribute_resolves_from_another_method(
    tmp_path: Path, system_id: str
) -> None:
    """The real-repository pilot's finding (docs/ROADMAP.md's Phase 2 entry):
    `self._notification_service = NotificationService(...)` in `__init__`,
    called as `self._notification_service.send_notifications(...)` from a
    different method entirely, must resolve -- this is the single most
    common way Python composes a class's own dependencies, and was silently
    UNRESOLVED before `SELF_ATTRIBUTE` (resolve.py) existed."""
    source = """
from .notifications import NotificationService


class IvrService:
    def __init__(self, session):
        self._notification_service = NotificationService(session=session)

    def handle_webhook(self, payload):
        return self._notification_service.send_notifications(payload)
"""
    result = _extract(tmp_path, "shop/ivr.py", source, system_id)
    calls = {
        c.payload["callee_expr"]: c.payload for c in _by_kind(result.observations, "python.call")
    }
    assert calls["self._notification_service.send_notifications"]["resolution"] == (
        "SELF_ATTRIBUTE"
    )
    assert (
        calls["self._notification_service.send_notifications"]["resolved_qualified_name"]
        == "shop.notifications.NotificationService.send_notifications"
    )


def test_constructor_assigned_self_attribute_with_annotation_resolves(
    tmp_path: Path, system_id: str
) -> None:
    source = """
from .payments import PaymentService


class CheckoutView:
    def __init__(self):
        self._payments: PaymentService = PaymentService()

    def checkout(self, order):
        self._payments.process(order)
"""
    result = _extract(tmp_path, "shop/checkout.py", source, system_id)
    calls = {
        c.payload["callee_expr"]: c.payload for c in _by_kind(result.observations, "python.call")
    }
    assert calls["self._payments.process"]["resolution"] == "SELF_ATTRIBUTE"
    assert (
        calls["self._payments.process"]["resolved_qualified_name"]
        == "shop.payments.PaymentService.process"
    )


def test_unrelated_self_attribute_chain_stays_unresolved_even_with_a_known_sibling(
    tmp_path: Path, system_id: str
) -> None:
    """A class with one resolvable `self.<attr>` must not cause an unrelated
    `self.<other_attr>.method()` -- never assigned in `__init__` -- to be
    guessed at just because the class has *a* known attribute."""
    source = """
from .payments import PaymentService


class CheckoutView:
    def __init__(self):
        self._payments = PaymentService()

    def checkout(self, order):
        self._unrelated.charge(order)
"""
    result = _extract(tmp_path, "shop/checkout.py", source, system_id)
    calls = {
        c.payload["callee_expr"]: c.payload for c in _by_kind(result.observations, "python.call")
    }
    assert calls["self._unrelated.charge"]["resolution"] == "UNRESOLVED"
    assert calls["self._unrelated.charge"]["resolved_qualified_name"] is None


# --- fallback-default idiom (`provided or Constructor()`) and typed ------
# --- parameters (docs/ROADMAP.md's "Real Repository Pilot v0.1, Phase 2" -
# --- continuation) --------------------------------------------------------


def test_self_attribute_fallback_via_bare_class_constructor_resolves(
    tmp_path: Path, system_id: str
) -> None:
    """`self._mcube = mcube_client or MCubeClient()` -- a real production
    repository's dominant DI idiom (`docs/ROADMAP.md`'s Phase 2
    continuation), and the case this fix genuinely closes: the fallback
    operand is a bare call to something that resolves to a *class*, so its
    own qualified name really is the resulting attribute's type."""
    source = """
from .clients import MCubeClient


class NotificationService:
    def __init__(self, mcube_client=None):
        self._mcube = mcube_client or MCubeClient()

    def send(self):
        self._mcube.send_sms("x")
"""
    result = _extract(tmp_path, "shop/notify.py", source, system_id)
    calls = {
        c.payload["callee_expr"]: c.payload for c in _by_kind(result.observations, "python.call")
    }
    assert calls["self._mcube.send_sms"]["resolution"] == "SELF_ATTRIBUTE"
    assert (
        calls["self._mcube.send_sms"]["resolved_qualified_name"]
        == "shop.clients.MCubeClient.send_sms"
    )


def test_self_attribute_fallback_via_factory_function_is_a_disclosed_boundary(
    tmp_path: Path, system_id: str
) -> None:
    """`self._settings = settings or get_settings()` -- the *other* real
    shape the same repository used, and one this fix does NOT make
    correct: `get_settings` is a plain factory *function* returning a
    `Settings` instance, not a class named `get_settings`. Stage 1 has no
    way to tell "imported class" from "imported function that returns one"
    apart from an import statement alone -- that distinction requires
    reading the imported module's own return-type annotation, which is
    cross-file and out of this fix's bounded scope (and was never asked
    for). This is not a new problem `_constructor_call` introduced: a bare,
    non-fallback `self._settings = get_settings()` had exactly the same
    limitation before this fix existed. Recording this explicitly as a
    test, not silently: the resulting `resolved_qualified_name` is
    syntactically produced but does not name a real entity, so
    `normalizer.py`'s Stage 2 safely fails to promote it (no relationship
    is fabricated) -- a miss, not a wrong answer, exactly per this
    project's own "never invent a target" rule."""
    source = """
from .config import get_settings


class IvrService:
    def __init__(self, settings=None):
        self._settings = settings or get_settings()

    def handle(self):
        self._settings.get_template_for_event("x")
"""
    result = _extract(tmp_path, "shop/ivr.py", source, system_id)
    calls = {
        c.payload["callee_expr"]: c.payload for c in _by_kind(result.observations, "python.call")
    }
    # Stage 1 mechanically produces a qualified name here ("get_settings" is
    # the only name it has to go on) -- but "shop.config.get_settings" is a
    # function, not a class, so "shop.config.get_settings.get_template_for_event"
    # names nothing real. Stage 2 (normalizer.py) is what actually protects
    # correctness by never promoting a call whose target doesn't exist.
    assert calls["self._settings.get_template_for_event"]["resolution"] == "SELF_ATTRIBUTE"
    assert (
        calls["self._settings.get_template_for_event"]["resolved_qualified_name"]
        == "shop.config.get_settings.get_template_for_event"
    )


def test_local_fallback_default_idiom_resolves(tmp_path: Path, system_id: str) -> None:
    """`x = provided or Constructor()` as a same-function local, mirroring
    the self-attribute case but for `_local_instance_types`."""
    source = """
from .payments import PaymentService


def checkout(provided=None):
    payments = provided or PaymentService()
    payments.process()
"""
    result = _extract(tmp_path, "shop/checkout.py", source, system_id)
    calls = {
        c.payload["callee_expr"]: c.payload for c in _by_kind(result.observations, "python.call")
    }
    assert calls["payments.process"]["resolution"] == "LOCAL_INSTANCE"
    assert (
        calls["payments.process"]["resolved_qualified_name"]
        == "shop.payments.PaymentService.process"
    )


def test_fallback_default_idiom_requires_a_bare_call_as_the_last_operand(
    tmp_path: Path, system_id: str
) -> None:
    """`provided or fallback_name` (not a call) must not be guessed -- only
    a literal call as the final `or` operand is mechanically strong enough
    evidence."""
    source = """
def checkout(provided=None, fallback_name=None):
    payments = provided or fallback_name
    payments.process()
"""
    result = _extract(tmp_path, "shop/checkout.py", source, system_id)
    calls = {
        c.payload["callee_expr"]: c.payload for c in _by_kind(result.observations, "python.call")
    }
    assert calls["payments.process"]["resolution"] == "UNRESOLVED"
    assert calls["payments.process"]["resolved_qualified_name"] is None


def test_typed_parameter_resolves_a_method_call_on_itself(tmp_path: Path, system_id: str) -> None:
    """The other real-production gap: a plain typed parameter calling a
    method on itself (`def receive(inbound: IvrWebhookInbound):
    inbound.resolve(...)`) -- arguably the most common shape in any
    framework's request-handling code -- was never resolved before."""
    source = """
from .webhook import IvrWebhookInbound


def receive(inbound: IvrWebhookInbound):
    inbound.resolve()
"""
    result = _extract(tmp_path, "shop/webhook.py", source, system_id)
    calls = {
        c.payload["callee_expr"]: c.payload for c in _by_kind(result.observations, "python.call")
    }
    assert calls["inbound.resolve"]["resolution"] == "LOCAL_INSTANCE"
    assert (
        calls["inbound.resolve"]["resolved_qualified_name"]
        == "shop.webhook.IvrWebhookInbound.resolve"
    )


def test_annotated_typed_parameter_resolves_a_method_call(tmp_path: Path, system_id: str) -> None:
    """`Annotated[T, ...]` (FastAPI's `Depends(...)` idiom) reduces to `T`
    before resolution -- the exact shape of `service: Annotated[IvrService,
    Depends(get_ivr_service)]` in the real repository that surfaced this."""
    source = """
from typing import Annotated

from .services import IvrService


def receive(service: Annotated[IvrService, Depends(get_ivr_service)]):
    service.handle_webhook()
"""
    result = _extract(tmp_path, "shop/webhook.py", source, system_id)
    calls = {
        c.payload["callee_expr"]: c.payload for c in _by_kind(result.observations, "python.call")
    }
    assert calls["service.handle_webhook"]["resolution"] == "LOCAL_INSTANCE"
    assert (
        calls["service.handle_webhook"]["resolved_qualified_name"]
        == "shop.services.IvrService.handle_webhook"
    )


def test_unresolvable_parameter_annotation_stays_unresolved(tmp_path: Path, system_id: str) -> None:
    """A parameter typed with something this adapter can't confirm against
    this file's own imports/module-locals (here, a builtin) must not be
    guessed -- calling a method on it stays UNRESOLVED, same as an
    unannotated parameter always has."""
    source = """
def handle(payload: dict):
    payload.get("x")
"""
    result = _extract(tmp_path, "shop/handle.py", source, system_id)
    calls = {
        c.payload["callee_expr"]: c.payload for c in _by_kind(result.observations, "python.call")
    }
    assert calls["payload.get"]["resolution"] == "UNRESOLVED"
    assert calls["payload.get"]["resolved_qualified_name"] is None


def test_local_reassignment_overrides_a_parameter_s_own_type(
    tmp_path: Path, system_id: str
) -> None:
    """If a parameter is reassigned to a different known type within the
    function body, the body's own assignment wins -- matching how a local
    reassignment already overrides anything earlier in `_local_instance_types`
    itself."""
    source = """
from .payments import PaymentService
from .refunds import RefundService


def checkout(payments: PaymentService):
    payments = RefundService()
    payments.process()
"""
    result = _extract(tmp_path, "shop/checkout.py", source, system_id)
    calls = {
        c.payload["callee_expr"]: c.payload for c in _by_kind(result.observations, "python.call")
    }
    assert (
        calls["payments.process"]["resolved_qualified_name"] == "shop.refunds.RefundService.process"
    )


def test_realistic_fastapi_route_resolves_end_to_end(tmp_path: Path, system_id: str) -> None:
    """A minimal but realistic fixture reproducing the exact production
    shape both gaps came from in one repository: a route function with an
    `Annotated[T, Depends(...)]` parameter calling a method on itself, and a
    service class composing a collaborator via the `provided or
    Constructor()` fallback-default idiom, called from a different method
    than the one that assigned it."""
    source = """
from typing import Annotated

from .config import get_settings
from .schemas import IvrWebhookInbound


class IvrService:
    def __init__(self, settings=None):
        self.settings = settings or get_settings()

    def handle_webhook(self, payload):
        return self.settings.get_template_for_event(payload)


def receive_ivr_webhook(
    inbound: IvrWebhookInbound,
    service: Annotated[IvrService, Depends(get_ivr_service)],
):
    payload = inbound.resolve()
    return service.handle_webhook(payload)
"""
    result = _extract(tmp_path, "shop/webhook.py", source, system_id)
    calls = {
        c.payload["callee_expr"]: c.payload for c in _by_kind(result.observations, "python.call")
    }

    assert calls["inbound.resolve"]["resolution"] == "LOCAL_INSTANCE"
    assert (
        calls["inbound.resolve"]["resolved_qualified_name"]
        == "shop.schemas.IvrWebhookInbound.resolve"
    )

    assert calls["service.handle_webhook"]["resolution"] == "LOCAL_INSTANCE"
    assert (
        calls["service.handle_webhook"]["resolved_qualified_name"]
        == "shop.webhook.IvrService.handle_webhook"
    )

    assert calls["self.settings.get_template_for_event"]["resolution"] == "SELF_ATTRIBUTE"
    assert (
        calls["self.settings.get_template_for_event"]["resolved_qualified_name"]
        == "shop.config.get_settings.get_template_for_event"
    )


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


# --- function-local imports (a common circular-import / lazy-import idiom) -


def test_function_local_import_resolves_a_call(tmp_path: Path, system_id: str) -> None:
    """`from common.kafka import kafka_publisher` written inside a function
    body, not at module level -- a common idiom for breaking a circular
    import or deferring an expensive one -- previously never entered
    `ResolutionContext.imports` at all: `_module_level_imports` explicitly
    skips descending into a function body, and nothing else picked the
    binding up. `kafka_publisher.push_notification(...)` fell straight to
    UNRESOLVED, silently, with no disclosed limitation naming this gap."""
    source = """
def enqueue_push_notification(payload):
    from common.kafka import kafka_publisher
    kafka_publisher.push_notification(payload)
"""
    result = _extract(tmp_path, "common/outbox/service.py", source, system_id)
    calls = {
        c.payload["callee_expr"]: c.payload for c in _by_kind(result.observations, "python.call")
    }
    assert calls["kafka_publisher.push_notification"]["resolution"] == "IMPORT"
    assert (
        calls["kafka_publisher.push_notification"]["resolved_qualified_name"]
        == "common.kafka.kafka_publisher.push_notification"
    )


def test_function_local_import_emits_an_imports_observation(tmp_path: Path, system_id: str) -> None:
    """The `IMPORTS` relationship is a module-level fact regardless of which
    scope the `import` statement sits in -- a function-local import must
    still produce a `python.import` observation, not just a resolvable call
    binding."""
    source = """
def send(payload):
    from common.kafka import kafka_publisher
    kafka_publisher.push_notification(payload)
"""
    result = _extract(tmp_path, "common/outbox/service.py", source, system_id)
    imports = {
        i.payload["bound_name"]: i.payload["target"]
        for i in _by_kind(result.observations, "python.import")
    }
    assert imports["kafka_publisher"] == "common.kafka.kafka_publisher"


def test_function_local_import_does_not_leak_to_a_sibling_function(
    tmp_path: Path, system_id: str
) -> None:
    """A function-local import binds a name only within that function's own
    scope, matching real Python semantics -- a sibling function that never
    imported the name itself must not resolve a same-named call."""
    source = """
def enqueue_push_notification(payload):
    from common.kafka import kafka_publisher
    kafka_publisher.push_notification(payload)


def some_other_function(payload):
    kafka_publisher.push_notification(payload)
"""
    result = _extract(tmp_path, "common/outbox/service.py", source, system_id)
    calls = [
        c.payload
        for c in _by_kind(result.observations, "python.call")
        if c.payload["callee_expr"] == "kafka_publisher.push_notification"
    ]
    by_caller = {c["caller_qualified_name"]: c for c in calls}
    assert by_caller["common.outbox.service.enqueue_push_notification"]["resolution"] == "IMPORT"
    assert by_caller["common.outbox.service.some_other_function"]["resolution"] == "UNRESOLVED"


def test_function_local_import_shadows_a_module_level_import(
    tmp_path: Path, system_id: str
) -> None:
    """A function-local import of the same name as a module-level import
    shadows it within that function's scope -- matching real Python name
    resolution, not a static merge that could pick either binding."""
    source = """
from common.kafka import legacy_publisher as kafka_publisher


def enqueue_push_notification(payload):
    from common.kafka.v2 import kafka_publisher
    kafka_publisher.push_notification(payload)
"""
    result = _extract(tmp_path, "common/outbox/service.py", source, system_id)
    calls = {
        c.payload["callee_expr"]: c.payload for c in _by_kind(result.observations, "python.call")
    }
    assert (
        calls["kafka_publisher.push_notification"]["resolved_qualified_name"]
        == "common.kafka.v2.kafka_publisher.push_notification"
    )


def test_function_local_import_resolves_a_constructor_call(tmp_path: Path, system_id: str) -> None:
    """A function-local import feeds `_local_instance_types` too, not only
    direct calls -- `x = LocallyImportedClass()` then `x.method()` inside the
    same function must resolve through the local import, not just a call on
    the imported name itself."""
    source = """
def handle(payload):
    from common.kafka import KafkaEventPublisher
    publisher = KafkaEventPublisher()
    publisher.push_notification(payload)
"""
    result = _extract(tmp_path, "common/outbox/service.py", source, system_id)
    calls = {
        c.payload["callee_expr"]: c.payload for c in _by_kind(result.observations, "python.call")
    }
    assert calls["publisher.push_notification"]["resolution"] == "LOCAL_INSTANCE"
    assert (
        calls["publisher.push_notification"]["resolved_qualified_name"]
        == "common.kafka.KafkaEventPublisher.push_notification"
    )


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
