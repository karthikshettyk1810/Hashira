"""DjangoAdapter: model/view/field/route/field-access detection, built on
top of what PythonAdapter already extracted -- never re-derived independently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hashira.adapters.django import DjangoAdapter
from hashira.adapters.python import PythonAdapter
from hashira.adapters.python.discovery import discover_python_files
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


def _by_kind(result: ExtractionResult, kind: str) -> list:  # type: ignore[type-arg]
    return [o for o in result.observations if o.kind == kind]


# --- detect() ----------------------------------------------------------------


def test_detect_true_with_manage_py(tmp_path: Path) -> None:
    (tmp_path / "manage.py").write_text("")
    assert DjangoAdapter().detect(tmp_path)


def test_detect_false_without_manage_py(tmp_path: Path) -> None:
    assert not DjangoAdapter().detect(tmp_path)


# --- model detection ---------------------------------------------------------


def test_model_detected_via_resolved_inheritance(tmp_path: Path, system_id: str) -> None:
    _write(
        tmp_path,
        "payments/models.py",
        "from django.db import models\n\n\nclass Payment(models.Model):\n    pass\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")

    models = _by_kind(addition, "django.model")
    assert len(models) == 1
    assert models[0].payload["class_qualified_name"] == "payments.models.Payment"
    assert models[0].evidence_ids


def test_unrelated_class_named_model_is_not_detected(tmp_path: Path, system_id: str) -> None:
    """The whole point of known_bases.py: name-suffix matching is refused.
    A class literally named `PaymentModel` that extends nothing Django-ish
    must not be classified as a Django model."""
    _write(tmp_path, "shop/things.py", "class PaymentModel:\n    pass\n")
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    assert _by_kind(addition, "django.model") == []


def test_class_extending_an_unrelated_base_named_model_is_not_detected(
    tmp_path: Path, system_id: str
) -> None:
    """A class extending *some* base called `Model` that isn't Django's is
    still not detected -- resolution must land on the exact known base."""
    _write(
        tmp_path,
        "shop/things.py",
        "from pydantic import BaseModel as Model\n\n\nclass Thing(Model):\n    pass\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    assert _by_kind(addition, "django.model") == []


def test_model_detected_through_a_multi_level_custom_abstract_base(
    tmp_path: Path, system_id: str
) -> None:
    """The real gap: a project's own multi-level custom abstract base
    (`TimestampedModel(UUIDModel)`, `UUIDModel(models.Model)`) was
    previously invisible entirely -- only a class's *direct* base was ever
    checked against `MODEL_BASES`, silently missing every real domain
    model in a real Django app that follows this idiomatic pattern
    (found via the read-only Rider benchmark: 15+ real models, none
    detected). `_expand_known_bases` mirrors SQLAlchemy's own
    `known_bases` fixpoint."""
    _write(
        tmp_path,
        "common/models.py",
        "from django.db import models\n\n\n"
        "class UUIDModel(models.Model):\n"
        "    class Meta:\n        abstract = True\n\n\n"
        "class TimestampedModel(UUIDModel):\n"
        "    class Meta:\n        abstract = True\n",
    )
    _write(
        tmp_path,
        "tours/models.py",
        "from common.models import TimestampedModel\n\n\nclass Tour(TimestampedModel):\n    pass\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")

    models = {m.payload["class_qualified_name"] for m in _by_kind(addition, "django.model")}
    assert "tours.models.Tour" in models


def test_unrelated_class_extending_a_detected_custom_base_by_coincidence_still_needs_evidence(
    tmp_path: Path, system_id: str
) -> None:
    """The fixpoint must still refuse a class whose base is *not* actually
    in the (correctly expanded) known set -- expanding the base set must
    not become a license to guess."""
    _write(
        tmp_path,
        "common/models.py",
        "from django.db import models\n\n\nclass UUIDModel(models.Model):\n    pass\n",
    )
    _write(tmp_path, "shop/things.py", "class Unrelated:\n    pass\n")
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    models = {m.payload["class_qualified_name"] for m in _by_kind(addition, "django.model")}
    assert "shop.things.Unrelated" not in models


# --- view detection ----------------------------------------------------------


def test_view_detected_via_resolved_inheritance(tmp_path: Path, system_id: str) -> None:
    _write(
        tmp_path,
        "payments/views.py",
        "from django.views import View\n\n\nclass CheckoutView(View):\n    pass\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")

    views = _by_kind(addition, "django.view")
    assert len(views) == 1
    assert views[0].payload["class_qualified_name"] == "payments.views.CheckoutView"
    assert views[0].payload["base_qualified_name"] == "django.views.View"


def test_drf_apiview_is_detected(tmp_path: Path, system_id: str) -> None:
    _write(
        tmp_path,
        "payments/views.py",
        "from rest_framework.views import APIView\n\n\nclass CheckoutView(APIView):\n    pass\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    assert len(_by_kind(addition, "django.view")) == 1


# --- field detection -----------------------------------------------------


def test_model_fields_are_detected(tmp_path: Path, system_id: str) -> None:
    _write(
        tmp_path,
        "payments/models.py",
        "from django.db import models\n\n\n"
        "class Payment(models.Model):\n"
        "    amount = models.DecimalField(max_digits=10, decimal_places=2)\n"
        "    status = models.CharField(max_length=20)\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")

    fields = {f.payload["field_name"]: f.payload for f in _by_kind(addition, "django.model_field")}
    assert set(fields) == {"amount", "status"}
    assert fields["status"]["field_type_qualified_name"] == "django.db.models.CharField"
    assert fields["status"]["model_qualified_name"] == "payments.models.Payment"


def test_non_field_class_attribute_is_not_a_field(tmp_path: Path, system_id: str) -> None:
    """A plain class-level constant (not a call, or a call to something
    outside django.db.models) must not be mistaken for a field."""
    _write(
        tmp_path,
        "payments/models.py",
        "from django.db import models\n\n\n"
        "class Payment(models.Model):\n"
        "    MAX_AMOUNT = 1000\n"
        "    status = models.CharField(max_length=20)\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    fields = {f.payload["field_name"] for f in _by_kind(addition, "django.model_field")}
    assert fields == {"status"}


def test_fields_on_a_non_model_class_are_not_detected(tmp_path: Path, system_id: str) -> None:
    _write(
        tmp_path,
        "payments/services.py",
        "from django.db import models\n\n\n"
        "class NotAModel:\n"
        "    status = models.CharField(max_length=20)\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    assert _by_kind(addition, "django.model_field") == []


# --- URL route detection ---------------------------------------------------


def test_url_route_resolves_class_based_view(tmp_path: Path, system_id: str) -> None:
    _write(
        tmp_path,
        "payments/views.py",
        "from django.views import View\n\n\nclass CheckoutView(View):\n    pass\n",
    )
    _write(
        tmp_path,
        "payments/urls.py",
        "from django.urls import path\n\nfrom .views import CheckoutView\n\n"
        "urlpatterns = [\n"
        '    path("checkout/", CheckoutView.as_view(), name="checkout"),\n'
        "]\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")

    routes = _by_kind(addition, "django.url_route")
    assert len(routes) == 1
    assert routes[0].payload["route"] == "checkout/"
    assert routes[0].payload["name"] == "checkout"
    assert routes[0].payload["resolved_view_qualified_name"] == "payments.views.CheckoutView"


def test_url_route_with_unresolvable_view_is_still_reported(tmp_path: Path, system_id: str) -> None:
    """`include(...)` and other non-view targets don't resolve to a view --
    the route observation is still emitted (uncertainty is data), just
    without a resolved target for the normalizer to link."""
    _write(
        tmp_path,
        "config/urls.py",
        "from django.urls import include, path\n\nurlpatterns = [\n"
        '    path("api/", include("payments.urls")),\n'
        "]\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    routes = _by_kind(addition, "django.url_route")
    assert len(routes) == 1
    assert routes[0].payload["resolved_view_qualified_name"] is None


def test_non_urlpatterns_list_is_ignored(tmp_path: Path, system_id: str) -> None:
    _write(tmp_path, "payments/constants.py", 'other_list = ["checkout/", "refund/"]\n')
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    assert _by_kind(addition, "django.url_route") == []


# --- field access detection -------------------------------------------------


def test_field_read_and_write_are_detected(tmp_path: Path, system_id: str) -> None:
    _write(
        tmp_path,
        "payments/models.py",
        "from django.db import models\n\n\nclass Payment(models.Model):\n"
        "    status = models.CharField(max_length=20)\n",
    )
    _write(
        tmp_path,
        "payments/services.py",
        "from .models import Payment\n\n\nclass PaymentService:\n"
        "    def process(self, amount):\n"
        "        payment = Payment()\n"
        '        payment.status = "pending"\n'
        "        return payment.status\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")

    accesses = _by_kind(addition, "django.field_access")
    kinds = {a.payload["access_kind"] for a in accesses}
    assert kinds == {"READ", "WRITE"}
    assert all(a.payload["field_name"] == "status" for a in accesses)
    assert all(
        a.payload["accessor_qualified_name"] == "payments.services.PaymentService.process"
        for a in accesses
    )


def test_field_access_on_an_unrelated_object_is_not_detected(
    tmp_path: Path, system_id: str
) -> None:
    """`.status` accessed on something that isn't a known model instance --
    e.g. a plain, undetected class -- must not be mistaken for a field access."""
    _write(
        tmp_path,
        "payments/services.py",
        "class Result:\n    def __init__(self):\n        self.status = 'ok'\n\n\n"
        "class Checker:\n    def run(self):\n"
        "        r = Result()\n        return r.status\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    assert _by_kind(addition, "django.field_access") == []


def test_field_read_and_write_through_a_typed_parameter_are_detected(
    tmp_path: Path, system_id: str
) -> None:
    """Real-repository verification (Rider): a typed parameter
    (`def process(payment: Payment)`) is arguably *the* most common way a
    Django service function receives a model instance at all -- a view or
    caller already looked it up -- yet was completely invisible to
    field-access tracking before `_typed_parameter_model_instances`. This
    is a direct, exact port of the analogous, already-proven SQLAlchemy
    mechanism (`_typed_parameter_instances`)."""
    _write(
        tmp_path,
        "payments/models.py",
        "from django.db import models\n\n\nclass Payment(models.Model):\n"
        "    status = models.CharField(max_length=20)\n",
    )
    _write(
        tmp_path,
        "payments/services.py",
        "from .models import Payment\n\n\n"
        "def close(payment: Payment) -> str:\n"
        '    payment.status = "closed"\n'
        "    return payment.status\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")

    accesses = _by_kind(addition, "django.field_access")
    kinds = {a.payload["access_kind"] for a in accesses}
    assert kinds == {"READ", "WRITE"}
    assert all(a.payload["field_name"] == "status" for a in accesses)
    assert all(a.payload["accessor_qualified_name"] == "payments.services.close" for a in accesses)


def test_getattr_with_a_literal_field_name_is_disclosed_not_resolved(
    tmp_path: Path, system_id: str
) -> None:
    """Resolution Integrity R3: a real, previously-undisclosed gap --
    Django's field-access extraction had no `getattr`/`setattr` detection
    at all, unlike SQLAlchemy's own, already-proven mechanism. Ported here,
    unchanged in spirit: a literal field name is disclosed as
    `DYNAMIC_ATTRIBUTE_ACCESS`, never resolved into a real access."""
    _write(
        tmp_path,
        "payments/models.py",
        "from django.db import models\n\n\nclass Payment(models.Model):\n"
        "    status = models.CharField(max_length=20)\n",
    )
    _write(
        tmp_path,
        "payments/services.py",
        "def process(payment):\n    return getattr(payment, 'status')\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")

    assert _by_kind(addition, "django.field_access") == []
    unresolved = _by_kind(addition, "django.unresolved_field_access")
    assert len(unresolved) == 1
    assert unresolved[0].payload["attribute_name"] == "status"
    assert unresolved[0].payload["limitation_kind"] == "DYNAMIC_ATTRIBUTE_ACCESS"


def test_getattr_with_a_computed_name_is_correctly_silent(tmp_path: Path, system_id: str) -> None:
    """Not a false limitation: a computed name isn't a literal this
    adapter can check against a field list at all -- mirrors SQLAlchemy's
    own adversarial case for the identical mechanism."""
    _write(
        tmp_path,
        "payments/models.py",
        "from django.db import models\n\n\nclass Payment(models.Model):\n"
        "    status = models.CharField(max_length=20)\n",
    )
    _write(
        tmp_path,
        "payments/services.py",
        "def process(payment, field_name):\n    return getattr(payment, field_name)\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")

    assert _by_kind(addition, "django.field_access") == []
    assert _by_kind(addition, "django.unresolved_field_access") == []


def test_capabilities_declare_framework_reflection() -> None:
    """Resolution Integrity R3: `DjangoAdapter` declared zero
    `known_limitations` at all before this -- Django users got no
    reflection disclosure whatsoever, unlike `FastAPIAdapter`'s own
    declaration for Pydantic's orm_mode. Mirrors it: DRF
    serializers/admin/signals read mapped fields with no source-level
    access for any adapter to see."""
    limitations = DjangoAdapter().capabilities().known_limitations
    assert any(lim.kind is LimitationKind.FRAMEWORK_REFLECTION for lim in limitations)


def test_addition_does_not_echo_the_base_observations(tmp_path: Path, system_id: str) -> None:
    """`enrich()` returns only what it added -- the caller already has
    `base`'s content and merging it again would duplicate everything."""
    _write(tmp_path, "payments/models.py", "class Plain:\n    pass\n")
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    addition_kinds = {o.kind for o in addition.observations}
    assert "python.symbol" not in addition_kinds
    assert "python.module" not in addition_kinds


# --- queryset-local-alias disclosure (Pattern A) ----------------------------


def test_queryset_filter_first_alias_is_disclosed_not_resolved(
    tmp_path: Path, system_id: str
) -> None:
    """Real-repository finding: `tour = Tour.objects.filter(stop=stop).first()`
    then `tour.status` -- the dominant queryset pattern in Django code.
    `_local_model_instances` only recognizes `= ModelClass(...)` (direct
    constructor); the queryset chain is not a constructor call, so `tour`
    was not in `local_instance_types` and `tour.status` was silently
    dropped — no edge, no limitation.

    Now disclosed as `RETURN_VALUE_PROVENANCE` through `_queryset_local_instances`,
    which narrowly detects the `.objects` manager pattern."""
    _write(
        tmp_path,
        "tours/models.py",
        "from django.db import models\n\n\nclass Tour(models.Model):\n"
        "    status = models.CharField(max_length=20)\n",
    )
    _write(
        tmp_path,
        "tours/services.py",
        "from .models import Tour\n\n\n"
        "def get_status(stop):\n"
        "    tour = Tour.objects.filter(stop=stop).first()\n"
        "    return tour.status\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")

    assert _by_kind(addition, "django.field_access") == []
    unresolved = _by_kind(addition, "django.unresolved_field_access")
    assert len(unresolved) == 1
    assert unresolved[0].payload["attribute_name"] == "status"
    assert unresolved[0].payload["limitation_kind"] == "RETURN_VALUE_PROVENANCE"


def test_queryset_get_alias_is_disclosed(tmp_path: Path, system_id: str) -> None:
    """Same pattern with `.get()` instead of `.filter(...).first()`."""
    _write(
        tmp_path,
        "tours/models.py",
        "from django.db import models\n\n\nclass Tour(models.Model):\n"
        "    status = models.CharField(max_length=20)\n",
    )
    _write(
        tmp_path,
        "tours/services.py",
        "from .models import Tour\n\n\n"
        "def get_status(tour_id):\n"
        "    tour = Tour.objects.get(id=tour_id)\n"
        "    return tour.status\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")

    assert _by_kind(addition, "django.field_access") == []
    unresolved = _by_kind(addition, "django.unresolved_field_access")
    assert len(unresolved) == 1
    assert unresolved[0].payload["attribute_name"] == "status"
    assert unresolved[0].payload["limitation_kind"] == "RETURN_VALUE_PROVENANCE"


def test_queryset_alias_does_not_override_constructor_resolution(
    tmp_path: Path, system_id: str
) -> None:
    """If a name is already resolved through direct constructor call,
    the queryset detection must NOT downgrade it to a disclosure.
    `tour = Tour()` should still produce a real `django.field_access`,
    not an unresolved disclosure."""
    _write(
        tmp_path,
        "tours/models.py",
        "from django.db import models\n\n\nclass Tour(models.Model):\n"
        "    status = models.CharField(max_length=20)\n",
    )
    _write(
        tmp_path,
        "tours/services.py",
        "from .models import Tour\n\n\n"
        "def make_tour():\n"
        "    tour = Tour()\n"
        "    return tour.status\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")

    accesses = _by_kind(addition, "django.field_access")
    assert len(accesses) == 1
    assert accesses[0].payload["field_name"] == "status"
    assert _by_kind(addition, "django.unresolved_field_access") == []


# --- chained-attribute-access disclosure (Pattern B) -------------------------


def test_chained_attribute_access_on_known_model_is_disclosed(
    tmp_path: Path, system_id: str
) -> None:
    """Real-repository finding: `stop.tour.status` where `stop` is a typed
    parameter of a known model.  The intermediate `.tour` hop is
    unresolvable (would require FK traversal), but the chain root `stop`
    is a known model instance — establishing model provenance.

    Disclosed as `RETURN_VALUE_PROVENANCE`, not silently dropped."""
    _write(
        tmp_path,
        "tours/models.py",
        "from django.db import models\n\n\n"
        "class Tour(models.Model):\n"
        "    status = models.CharField(max_length=20)\n\n\n"
        "class Stop(models.Model):\n"
        "    name = models.CharField(max_length=100)\n",
    )
    _write(
        tmp_path,
        "tours/services.py",
        "from .models import Stop\n\n\n"
        "def get_tour_status(stop: Stop):\n"
        "    return stop.tour.status\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")

    assert _by_kind(addition, "django.field_access") == []
    unresolved = _by_kind(addition, "django.unresolved_field_access")
    assert len(unresolved) == 1
    assert unresolved[0].payload["attribute_name"] == "status"
    assert unresolved[0].payload["limitation_kind"] == "RETURN_VALUE_PROVENANCE"


def test_chained_attribute_access_unknown_field_is_correctly_silent(
    tmp_path: Path, system_id: str
) -> None:
    """Precision guard: `stop.tour.unrelated_attr` where `unrelated_attr`
    is NOT a known model field name — no disclosure.  Prevents over-eager
    reporting that would flood coverage with false warnings."""
    _write(
        tmp_path,
        "tours/models.py",
        "from django.db import models\n\n\n"
        "class Stop(models.Model):\n"
        "    name = models.CharField(max_length=100)\n",
    )
    _write(
        tmp_path,
        "tours/services.py",
        "from .models import Stop\n\n\n"
        "def do_thing(stop: Stop):\n"
        "    return stop.tour.unrelated_attr\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")

    assert _by_kind(addition, "django.field_access") == []
    assert _by_kind(addition, "django.unresolved_field_access") == []


# --- adversarial precision tests (reviewer amendment) ------------------------


def test_adversarial__unrelated_object_with_coincidental_field_name(
    tmp_path: Path, system_id: str
) -> None:
    """Precision: `user.status` where `user` is an unrelated, untyped local
    variable and `status` happens to be a real field on some Django model
    elsewhere.  Must NOT produce a disclosure — the chain root `user` is
    NOT in `local_instance_types`, so neither the queryset branch nor the
    chained-attribute branch should fire.  A direct `name.field` on an
    unknown name stays correctly silent."""
    _write(
        tmp_path,
        "tours/models.py",
        "from django.db import models\n\n\nclass Tour(models.Model):\n"
        "    status = models.CharField(max_length=20)\n",
    )
    _write(
        tmp_path,
        "tours/services.py",
        "def check_user(user):\n"
        "    return user.status\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")

    assert _by_kind(addition, "django.field_access") == []
    assert _by_kind(addition, "django.unresolved_field_access") == []


def test_adversarial__unrelated_chain_with_coincidental_field_name(
    tmp_path: Path, system_id: str
) -> None:
    """Precision: `bar.tour.status` where `bar` is a completely unrelated
    object — NOT a known Django model instance.  Even though `status`
    happens to be a real model field name, the chain root `bar` is not
    in `local_instance_types`, so the disclosure must NOT fire.

    This is the exact adversarial case the reviewer flagged: the
    chained-attribute rule must be gated on the chain root having
    established model provenance, not merely on the leaf field name
    matching a model field."""
    _write(
        tmp_path,
        "tours/models.py",
        "from django.db import models\n\n\nclass Tour(models.Model):\n"
        "    status = models.CharField(max_length=20)\n",
    )
    _write(
        tmp_path,
        "tours/services.py",
        "class HttpResponse:\n    def __init__(self):\n        self.tour = None\n\n\n"
        "def check(bar):\n"
        "    return bar.tour.status\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")

    assert _by_kind(addition, "django.field_access") == []
    assert _by_kind(addition, "django.unresolved_field_access") == []


def test_adversarial__service_method_chain_is_not_queryset(
    tmp_path: Path, system_id: str
) -> None:
    """Precision: `result = some_service.get_tour()` then `result.status`.
    Even though `status` is a model field name, `some_service` is not
    going through `.objects` -- the queryset detection must NOT fire.
    This prevents classifying arbitrary method calls as model-derived
    just because their return value's attribute coincidentally matches."""
    _write(
        tmp_path,
        "tours/models.py",
        "from django.db import models\n\n\nclass Tour(models.Model):\n"
        "    status = models.CharField(max_length=20)\n",
    )
    _write(
        tmp_path,
        "tours/services.py",
        "class TourService:\n    def get_tour(self): pass\n\n\n"
        "def check():\n"
        "    service = TourService()\n"
        "    result = service.get_tour()\n"
        "    return result.status\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")

    assert _by_kind(addition, "django.field_access") == []
    assert _by_kind(addition, "django.unresolved_field_access") == []


def test_adversarial__inline_queryset_without_alias_is_correctly_silent(
    tmp_path: Path, system_id: str
) -> None:
    """Precision: `Tour.objects.filter(...).first().status` (inline, no
    local alias).  No local variable to anchor the detection on — this
    would require value-tracking through the full call chain inline,
    which is beyond the bounded analysis.  Correctly silent, not a
    false disclosure."""
    _write(
        tmp_path,
        "tours/models.py",
        "from django.db import models\n\n\nclass Tour(models.Model):\n"
        "    status = models.CharField(max_length=20)\n",
    )
    _write(
        tmp_path,
        "tours/services.py",
        "from .models import Tour\n\n\n"
        "def get_status():\n"
        "    return Tour.objects.filter(active=True).first().status\n",
    )
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")

    # Inline chain accesses are beyond the bounded analysis — correctly
    # silent (no edge, no disclosure).  A future resolver expansion may
    # choose to handle this, but the current disclosure fix is about
    # local-variable-anchored patterns only.
    assert _by_kind(addition, "django.field_access") == []
    assert _by_kind(addition, "django.unresolved_field_access") == []

