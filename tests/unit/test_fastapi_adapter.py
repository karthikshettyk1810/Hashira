"""FastAPIAdapter: app/router recognition, route/dependency/request-response-
model detection, built on top of what PythonAdapter already extracted --
never re-derived independently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hashira.adapters.fastapi import FastAPIAdapter
from hashira.adapters.python import PythonAdapter
from hashira.adapters.python.discovery import discover_python_files
from hashira.core.ids import IDPrefix, new_id
from hashira.ports.adapters import ExtractionResult, LimitationKind, LimitationScope


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


# --- detect() ------------------------------------------------------------


def test_detect_true_with_fastapi_in_requirements(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
    assert FastAPIAdapter().detect(tmp_path)


def test_detect_true_with_fastapi_in_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["fastapi>=0.100"]\n')
    assert FastAPIAdapter().detect(tmp_path)


def test_detect_false_without_any_manifest_mentioning_fastapi(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("django\n")
    assert not FastAPIAdapter().detect(tmp_path)


def test_detect_false_with_no_manifest_at_all(tmp_path: Path) -> None:
    assert not FastAPIAdapter().detect(tmp_path)


# --- app/router recognition is evidence-based, never name-based ----------


def test_a_variable_named_app_that_is_not_fastapi_is_ignored(
    tmp_path: Path, system_id: str
) -> None:
    """The whole point of known_symbols.py: a variable happening to be
    named `app` must not be mistaken for a FastAPI instance."""
    _write(
        tmp_path,
        "main.py",
        "class NotFastAPI:\n    def get(self, path):\n        pass\n\n\n"
        "app = NotFastAPI()\n\n\n"
        '@app.get("/checkout/")\n'
        "def checkout():\n    pass\n",
    )
    base = _base(tmp_path, system_id)
    addition = FastAPIAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    assert _by_kind(addition, "fastapi.route") == []


def test_router_prefix_is_recorded(tmp_path: Path, system_id: str) -> None:
    _write(
        tmp_path,
        "routers.py",
        'from fastapi import APIRouter\n\nrouter = APIRouter(prefix="/payments")\n',
    )
    base = _base(tmp_path, system_id)
    addition = FastAPIAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    routers = _by_kind(addition, "fastapi.router")
    assert len(routers) == 1
    assert routers[0].payload["prefix"] == "/payments"
    assert routers[0].payload["composed_prefix"] == "/payments"  # not yet included anywhere


# --- route decorator detection --------------------------------------------


def test_route_on_the_app_itself_is_detected(tmp_path: Path, system_id: str) -> None:
    _write(
        tmp_path,
        "main.py",
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n"
        '@app.get("/health")\n'
        'def health():\n    return {"ok": True}\n',
    )
    base = _base(tmp_path, system_id)
    addition = FastAPIAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    routes = _by_kind(addition, "fastapi.route")
    assert len(routes) == 1
    assert routes[0].payload["http_method"] == "GET"
    assert routes[0].payload["path"] == "/health"
    assert routes[0].payload["handler_qualified_name"] == "main.health"


def test_route_path_composes_router_prefix_and_include_router_prefix(
    tmp_path: Path, system_id: str
) -> None:
    _write(
        tmp_path,
        "payments/routers.py",
        "from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n"
        '@router.post("/checkout/")\n'
        "def checkout():\n    pass\n",
    )
    _write(
        tmp_path,
        "main.py",
        "from fastapi import FastAPI\n\nfrom payments.routers import router\n\n"
        "app = FastAPI()\n"
        'app.include_router(router, prefix="/payments")\n',
    )
    base = _base(tmp_path, system_id)
    addition = FastAPIAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    routes = _by_kind(addition, "fastapi.route")
    assert len(routes) == 1
    assert routes[0].payload["path"] == "/payments/checkout/"

    includes = _by_kind(addition, "fastapi.include_router")
    assert len(includes) == 1
    assert includes[0].payload["includer_qualified_name"] == "main.app"
    assert includes[0].payload["included_qualified_name"] == "payments.routers.router"


def test_route_with_a_non_literal_path_is_not_recognized(tmp_path: Path, system_id: str) -> None:
    _write(
        tmp_path,
        "main.py",
        'from fastapi import FastAPI\n\napp = FastAPI()\nPATH = "/health"\n\n\n'
        "@app.get(PATH)\n"
        "def health():\n    pass\n",
    )
    base = _base(tmp_path, system_id)
    addition = FastAPIAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    assert _by_kind(addition, "fastapi.route") == []


def test_non_http_method_decorator_is_not_a_route(tmp_path: Path, system_id: str) -> None:
    _write(
        tmp_path,
        "main.py",
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n"
        "@app.middleware\n"
        "def handler():\n    pass\n",
    )
    base = _base(tmp_path, system_id)
    addition = FastAPIAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    assert _by_kind(addition, "fastapi.route") == []


# --- Depends() is not a plain call ----------------------------------------


def test_depends_default_is_a_dependency_not_a_call(tmp_path: Path, system_id: str) -> None:
    _write(
        tmp_path,
        "main.py",
        "from fastapi import Depends, FastAPI\n\n"
        "app = FastAPI()\n\n\n"
        "def get_service():\n    pass\n\n\n"
        '@app.post("/checkout/")\n'
        "def checkout(service=Depends(get_service)):\n    pass\n",
    )
    base = _base(tmp_path, system_id)
    addition = FastAPIAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    deps = _by_kind(addition, "fastapi.dependency")
    assert len(deps) == 1
    assert deps[0].payload["dependent_qualified_name"] == "main.checkout"
    assert deps[0].payload["parameter_name"] == "service"
    assert deps[0].payload["resolved_dependency_qualified_name"] == "main.get_service"


def test_an_ordinary_call_default_is_not_mistaken_for_depends(
    tmp_path: Path, system_id: str
) -> None:
    _write(
        tmp_path,
        "main.py",
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n"
        "def make_default():\n    return 1\n\n\n"
        '@app.get("/health")\n'
        "def health(limit=make_default()):\n    pass\n",
    )
    base = _base(tmp_path, system_id)
    addition = FastAPIAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    assert _by_kind(addition, "fastapi.dependency") == []


# --- request/response model association -----------------------------------


def test_request_and_response_models_are_associated_via_annotations(
    tmp_path: Path, system_id: str
) -> None:
    _write(
        tmp_path,
        "schemas.py",
        "from pydantic import BaseModel\n\n\n"
        "class PaymentRequest(BaseModel):\n    amount: int\n\n\n"
        "class PaymentResponse(BaseModel):\n    status: str\n",
    )
    _write(
        tmp_path,
        "main.py",
        "from fastapi import FastAPI\n\n"
        "from schemas import PaymentRequest, PaymentResponse\n\n"
        "app = FastAPI()\n\n\n"
        '@app.post("/checkout/")\n'
        "def checkout(body: PaymentRequest) -> PaymentResponse:\n    pass\n",
    )
    base = _base(tmp_path, system_id)
    addition = FastAPIAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    routes = _by_kind(addition, "fastapi.route")
    assert len(routes) == 1
    assert routes[0].payload["request_model_qualified_name"] == "schemas.PaymentRequest"
    assert routes[0].payload["response_model_qualified_name"] == "schemas.PaymentResponse"


def test_response_model_kwarg_is_preferred_over_return_annotation(
    tmp_path: Path, system_id: str
) -> None:
    _write(
        tmp_path,
        "schemas.py",
        "from pydantic import BaseModel\n\n\nclass PaymentResponse(BaseModel):\n    status: str\n",
    )
    _write(
        tmp_path,
        "main.py",
        "from fastapi import FastAPI\n\n"
        "from schemas import PaymentResponse\n\n"
        "app = FastAPI()\n\n\n"
        '@app.post("/checkout/", response_model=PaymentResponse)\n'
        "def checkout():\n    pass\n",
    )
    base = _base(tmp_path, system_id)
    addition = FastAPIAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    routes = _by_kind(addition, "fastapi.route")
    assert routes[0].payload["response_model_qualified_name"] == "schemas.PaymentResponse"


def test_a_non_pydantic_annotation_is_not_associated(tmp_path: Path, system_id: str) -> None:
    _write(
        tmp_path,
        "main.py",
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n"
        "class PlainClass:\n    pass\n\n\n"
        '@app.post("/checkout/")\n'
        "def checkout(body: PlainClass):\n    pass\n",
    )
    base = _base(tmp_path, system_id)
    addition = FastAPIAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    routes = _by_kind(addition, "fastapi.route")
    assert routes[0].payload["request_model_qualified_name"] is None


def test_addition_does_not_echo_the_base_observations(tmp_path: Path, system_id: str) -> None:
    """`enrich()` returns only what it added -- the caller already has
    `base`'s content and merging it again would duplicate everything."""
    _write(tmp_path, "main.py", "class Plain:\n    pass\n")
    base = _base(tmp_path, system_id)
    addition = FastAPIAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    addition_kinds = {o.kind for o in addition.observations}
    assert "python.symbol" not in addition_kinds
    assert "python.module" not in addition_kinds


def test_no_fastapi_shaped_code_produces_no_observations(tmp_path: Path, system_id: str) -> None:
    _write(tmp_path, "plain.py", "def add(a, b):\n    return a + b\n")
    base = _base(tmp_path, system_id)
    addition = FastAPIAdapter().enrich(tmp_path, base, system_id=system_id, revision="rev1")
    assert addition.observations == []
    assert addition.errors == []


def test_capabilities_state_framework_reflection_as_a_known_limitation() -> None:
    """A response model reading a mapped attribute via Pydantic's own
    orm_mode reflection (no source-level access to see at all) is a real,
    undisclosed gap a live agent experiment found twice
    (`docs/IR.md`'s "field-access coverage audit" entry) -- declared here,
    unconditionally, rather than left silent."""
    limitations = FastAPIAdapter().capabilities().known_limitations
    assert any(
        limitation.kind is LimitationKind.FRAMEWORK_REFLECTION
        and limitation.scope is LimitationScope.FRAMEWORK_SERIALIZATION
        for limitation in limitations
    )
