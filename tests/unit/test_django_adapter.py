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


def test_addition_does_not_echo_the_base_observations(tmp_path: Path, system_id: str) -> None:
    """`enrich()` returns only what it added -- the caller already has
    `base`'s content and merging it again would duplicate everything."""
    _write(tmp_path, "payments/models.py", "class Plain:\n    pass\n")
    base = _base(tmp_path, system_id)
    addition = DjangoAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    addition_kinds = {o.kind for o in addition.observations}
    assert "python.symbol" not in addition_kinds
    assert "python.module" not in addition_kinds
