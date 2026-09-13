"""`FastAPIAdapter`: the `FrameworkAdapter` port, satisfied.

Same reuse boundary as `adapters.django.adapter` (see its module docstring
for the general principle, and `adapters._python_index` for the shared
plumbing both enrichers now build on): classes, functions, methods, imports
and *resolved inheritance* are Python's job, done once
(`adapters/python/extractor.py`); this adapter never re-derives them. What it
parses for itself is genuinely FastAPI-specific and has no Python-adapter
counterpart at all:

* **app/router recognition** (`app = FastAPI()`, `router = APIRouter(prefix=...)`)
  -- a plain call-assignment with no Python-general shape;
* **route registration** (`@app.get("/checkout/")`, `@router.post(...)`) --
  a decorator whose *meaning* (an HTTP route) Python's extractor has no
  reason to know, even though it already recorded the decorator's unparsed
  source text on the `python.symbol` observation;
* **router wiring** (`app.include_router(router, prefix="/payments")`) --
  needed to compose a route's full path across files;
* **dependency injection** (`Depends(get_payment_service)` as a parameter
  default) -- deliberately *not* folded into `CALLS` just because the
  syntax contains a call expression; FastAPI's own semantics here are
  stronger than the syntax (see `DEPENDS_ON` in `normalizer.py`);
* **basic request/response model association** -- a parameter or return
  annotation that resolves to a class Python's own `python.inheritance`
  observations confirm extends `pydantic.BaseModel`.

**Detection is evidence-based, not name-based**, same discipline as Django's
`known_bases.py`: an app/router variable is recognized because it is
assigned the result of calling `fastapi.FastAPI`/`fastapi.APIRouter`
(resolved through import bindings), never because a variable happens to be
named `app`. See `known_symbols.py`.

**This adapter produces only `Observation`s**, exactly like `DjangoAdapter`
-- never `Entity`/`Relationship` objects directly; `normalizer.py` builds
the graph in Stage 2, after Python's own normalization, so it can link a
route to the handler function Python already resolved.

**Out of scope for v0.1** (matching the milestone's explicit "keep it
narrow"): SQLAlchemy or any other persistence layer (that is a
`DataAdapter`'s job, not a framework enricher's), field-level access on
Pydantic models (the association is handler-to-model, not model-field-to-
handler -- there is no FastAPI/Pydantic equivalent of Django's
`_extract_field_accesses` here), and multi-hop router nesting (a router
included into another router that is itself included into an app composes
only one level of prefix -- see `_composed_prefix`).
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ...core.base import SourceRef
from ...core.enums import Origin
from ...core.evidence import Evidence, Observation
from ...core.ids import SystemID
from ...ports.adapters import (
    AdapterCapabilities,
    ExtractionResult,
    Limitation,
    LimitationKind,
    LimitationScope,
)
from .._python_index import PythonIndex, PythonTreeCache
from ..python.resolve import ResolutionContext, resolve_expr
from .known_symbols import (
    APP_CLASSES,
    BASE_MODEL_CLASSES,
    DEPENDS_CALLABLES,
    HTTP_METHODS,
    ROUTER_CLASSES,
)

__all__ = ["FastAPIAdapter"]

_MANIFEST_FILES = ("pyproject.toml", "requirements.txt", "Pipfile")


@dataclass(frozen=True, slots=True)
class _AppOrRouter:
    kind: str
    """"app" or "router"."""
    prefix: str
    """Only meaningful for "router" -- the `prefix=` passed to `APIRouter()`."""


@dataclass(frozen=True, slots=True)
class _Include:
    includer_qualified_name: str
    included_qualified_name: str
    prefix: str | None


class FastAPIAdapter:
    """Route/dependency/request-response-model evidence for a FastAPI project."""

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            name="fastapi",
            version="0.1.0",
            ir_versions=["0.1.3"],
            languages=["python"],
            frameworks=["fastapi"],
            # No entity_types/relationship_types: like DjangoAdapter, this
            # produces Observations only -- normalizer.py builds the graph.
            requires_network=False,
            known_limitations=[
                Limitation(
                    kind=LimitationKind.FRAMEWORK_REFLECTION,
                    scope=LimitationScope.FRAMEWORK_SERIALIZATION,
                    detail=(
                        "A response model reading a mapped attribute via "
                        "Pydantic's own orm_mode/from_attributes reflection "
                        "(rather than an explicit `.field` access in source) "
                        "is not represented as a READS edge."
                    ),
                ),
            ],
        )

    def detect(self, root: Path) -> bool:
        """Cheap: FastAPI has no marker file like Django's `manage.py`, so
        this checks the project's own declared dependencies for "fastapi"
        rather than scanning source -- a few short file reads, no parsing."""
        for name in _MANIFEST_FILES:
            path = root / name
            if not path.is_file():
                continue
            try:
                if "fastapi" in path.read_text(encoding="utf-8", errors="ignore").lower():
                    return True
            except OSError:
                continue
        return False

    def enrich(
        self, root: Path, base: ExtractionResult, *, system_id: SystemID, revision: str | None
    ) -> ExtractionResult:
        """Return *only the additions* -- new `fastapi.*` observations found
        by combining Python's own observations with this adapter's own
        targeted parsing. The caller merges this into the run; `base`'s own
        content is not echoed back."""
        now = datetime.now(UTC)
        index = PythonIndex.build(base)
        trees = PythonTreeCache(root)
        result = ExtractionResult()

        # Pass 1: every FastAPI()/APIRouter() instance, keyed by its
        # module-qualified variable name -- needed before route decorators
        # (which reference these vars) can mean anything.
        apps_and_routers: dict[str, _AppOrRouter] = {}
        local_names_by_module: dict[str, set[str]] = {}
        for module_qn, file_rel in index.module_file.items():
            tree = trees.get(file_rel)
            if tree is None:
                continue
            ctx = index.context_for(module_qn)
            for var_name, info in _find_app_and_router_instances(tree, ctx):
                var_qn = f"{module_qn}.{var_name}"
                apps_and_routers[var_qn] = info
                local_names_by_module.setdefault(module_qn, set()).add(var_name)

        if not apps_and_routers:
            return result  # nothing FastAPI-shaped found -- an honest empty result

        # Pass 2: include_router(...) wiring, so route paths can be composed
        # across files (a router's own prefix plus wherever it was attached).
        includes: list[_Include] = []
        for module_qn, file_rel in index.module_file.items():
            tree = trees.get(file_rel)
            if tree is None:
                continue
            ctx = _augmented_context(index, module_qn, local_names_by_module)
            for include in _find_include_router_calls(tree, ctx, apps_and_routers):
                includes.append(include)
                result.observations.append(
                    _emit(
                        result,
                        system_id=system_id,
                        kind="fastapi.include_router",
                        payload={
                            "includer_qualified_name": include.includer_qualified_name,
                            "included_qualified_name": include.included_qualified_name,
                            "prefix": include.prefix,
                        },
                        summary=(
                            f"{include.includer_qualified_name} includes "
                            f"{include.included_qualified_name}"
                        ),
                        file=file_rel,
                        line=None,
                        now=now,
                    )
                )

        for var_qn, info in apps_and_routers.items():
            if info.kind != "router":
                continue
            module_qn = var_qn.rsplit(".", 1)[0]
            result.observations.append(
                _emit(
                    result,
                    system_id=system_id,
                    kind="fastapi.router",
                    payload={
                        "var_qualified_name": var_qn,
                        "prefix": info.prefix,
                        "composed_prefix": _composed_prefix(var_qn, apps_and_routers, includes),
                    },
                    summary=f"{var_qn} is a FastAPI router (prefix={info.prefix!r})",
                    file=index.module_file.get(module_qn, ""),
                    line=None,
                    now=now,
                )
            )

        # Pass 3: route decorators and dependency-injected parameters, over
        # every function/method in every module -- a dependency function can
        # itself take further dependencies, so this is not limited to
        # functions already known to be route handlers.
        current_routes: list[dict[str, object]] = []
        for module_qn, file_rel in index.module_file.items():
            tree = trees.get(file_rel)
            if tree is None:
                continue
            ctx = _augmented_context(index, module_qn, local_names_by_module)
            for func_qn, func_node in _walk_functions(tree, module_qn):
                route = _match_route_decorator(func_node, ctx, apps_and_routers)
                if route is not None:
                    method, var_qn, path = route.method, route.var_qualified_name, route.path
                    full_path = _composed_prefix(var_qn, apps_and_routers, includes) + path
                    request_qn, response_qn = _request_response_models(func_node, ctx, index)
                    if route.response_model_qualified_name and _is_pydantic_model(
                        route.response_model_qualified_name, index
                    ):
                        response_qn = route.response_model_qualified_name
                    current_routes.append(
                        {
                            "file": file_rel,
                            "var_qualified_name": var_qn,
                            "http_method": method,
                            "path": full_path,
                            "handler_qualified_name": func_qn,
                            "line": func_node.lineno,
                        }
                    )
                    result.observations.append(
                        _emit(
                            result,
                            system_id=system_id,
                            kind="fastapi.route",
                            payload={
                                "var_qualified_name": var_qn,
                                "http_method": method,
                                "path": full_path,
                                "handler_qualified_name": func_qn,
                                "request_model_qualified_name": request_qn,
                                "response_model_qualified_name": response_qn,
                            },
                            summary=f"{method} {full_path} -> {func_qn}",
                            file=index.symbol_file.get(func_qn, file_rel),
                            line=func_node.lineno,
                            now=now,
                        )
                    )

                for arg_name, dep_qn, line in _find_dependencies(func_node, ctx):
                    result.observations.append(
                        _emit(
                            result,
                            system_id=system_id,
                            kind="fastapi.dependency",
                            payload={
                                "dependent_qualified_name": func_qn,
                                "parameter_name": arg_name,
                                "resolved_dependency_qualified_name": dep_qn,
                            },
                            summary=f"{func_qn}({arg_name}) depends on {dep_qn or '<unresolved>'}",
                            file=index.symbol_file.get(func_qn, file_rel),
                            line=line,
                            now=now,
                        )
                    )

        # Pass 4: detect framework route evolutions across revisions where a
        # file was MODIFIED and a route on a stable handler/router/method changed path.
        for rename in _detect_route_renames(
            base.observations,
            apps_and_routers=apps_and_routers,
            includes=includes,
            index=index,
            local_names_by_module=local_names_by_module,
            current_routes=current_routes,
        ):
            line_val = rename.get("line")
            line_num = line_val if isinstance(line_val, int) else None
            result.observations.append(
                _emit(
                    result,
                    system_id=system_id,
                    kind="fastapi.declaration_rename",
                    payload=rename,
                    summary=(
                        f"Route {rename['http_method']} {rename['old_path']} evolved to "
                        f"{rename['http_method']} {rename['new_path']} for handler "
                        f"{rename['handler_qualified_name']}"
                    ),
                    file=str(rename.get("file", "")),
                    line=line_num,
                    now=now,
                )
            )

        return result


def _augmented_context(
    index: PythonIndex, module_qn: str, local_names_by_module: dict[str, set[str]]
) -> ResolutionContext:
    """Python's own `python.symbol` observations never track a plain
    `app = FastAPI()` assignment (it is not a class/function/method), so an
    app/router variable this adapter itself found has to be added to the
    module-locals set by hand before `resolve_expr` can see it."""
    base_ctx = index.context_for(module_qn)
    extra = local_names_by_module.get(module_qn, set())
    return ResolutionContext(
        module_qualified_name=module_qn,
        imports=base_ctx.imports,
        module_locals=base_ctx.module_locals | extra,
    )


def _string_kwarg(call: ast.Call, name: str) -> str | None:
    for kw in call.keywords:
        if (
            kw.arg == name
            and isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, str)
        ):
            return kw.value.value
    return None


def _find_app_and_router_instances(
    tree: ast.Module, ctx: ResolutionContext
) -> list[tuple[str, _AppOrRouter]]:
    """`name = FastAPI()` / `name = APIRouter(prefix=...)` at module level."""
    found: list[tuple[str, _AppOrRouter]] = []
    for node in tree.body:
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
        ):
            continue
        resolved = resolve_expr(node.value.func, ctx)
        if resolved.qualified_name in APP_CLASSES:
            found.append((node.targets[0].id, _AppOrRouter(kind="app", prefix="")))
        elif resolved.qualified_name in ROUTER_CLASSES:
            prefix = _string_kwarg(node.value, "prefix") or ""
            found.append((node.targets[0].id, _AppOrRouter(kind="router", prefix=prefix)))
    return found


def _find_include_router_calls(
    tree: ast.Module, ctx: ResolutionContext, apps_and_routers: dict[str, _AppOrRouter]
) -> list[_Include]:
    """`<app_or_router>.include_router(<router>, prefix=...)`, anywhere in
    the module -- not just at module level, since this commonly lives inside
    an app-factory function."""
    includes: list[_Include] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "include_router"
        ):
            continue
        includer = resolve_expr(node.func.value, ctx)
        if includer.qualified_name not in apps_and_routers:
            continue
        if not node.args:
            continue
        included = resolve_expr(node.args[0], ctx)
        if included.qualified_name not in apps_and_routers:
            continue
        includes.append(
            _Include(
                includer_qualified_name=includer.qualified_name,
                included_qualified_name=included.qualified_name,
                prefix=_string_kwarg(node, "prefix"),
            )
        )
    return includes


def _composed_prefix(
    var_qn: str, apps_and_routers: dict[str, _AppOrRouter], includes: list[_Include]
) -> str:
    """A router's own `prefix=` plus wherever it was attached via
    `include_router(..., prefix=...)` -- one hop only (see the module
    docstring's "out of scope" note on deeper nesting)."""
    info = apps_and_routers.get(var_qn)
    if info is None or info.kind != "router":
        return ""
    include = next((inc for inc in includes if inc.included_qualified_name == var_qn), None)
    include_prefix = (include.prefix or "") if include is not None else ""
    return info.prefix + include_prefix


_FuncNode = ast.FunctionDef | ast.AsyncFunctionDef


def _walk_functions(tree: ast.Module, module_qn: str) -> Iterator[tuple[str, _FuncNode]]:
    """Every function and method in the module, paired with the qualified
    name Python's own extractor would give it (`module_qn.function` or
    `module_qn.Class.method`) -- computed independently here since this
    adapter needs the AST node itself (for decorators/parameters), not just
    the name Python already recorded."""

    def walk(body: list[ast.stmt], qn: str) -> Iterator[tuple[str, _FuncNode]]:
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                yield from walk(stmt.body, f"{qn}.{stmt.name}")
            elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                func_qn = f"{qn}.{stmt.name}"
                yield func_qn, stmt
                yield from walk(stmt.body, func_qn)

    yield from walk(tree.body, module_qn)


@dataclass(frozen=True, slots=True)
class _RouteMatch:
    method: str
    var_qualified_name: str
    path: str
    response_model_qualified_name: str | None
    """Resolved from the decorator's own `response_model=` kwarg, if any --
    FastAPI's more idiomatic way to declare a response contract than a bare
    return annotation. Preferred over the return annotation when present
    (see `_request_response_models`)."""


def _match_route_decorator(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    ctx: ResolutionContext,
    apps_and_routers: dict[str, _AppOrRouter],
) -> _RouteMatch | None:
    """`@app.get("/checkout/")` / `@router.post(...)`, or `None` if this
    function carries no such decorator. A route whose path argument is not
    a plain string constant is not recognized -- same discipline as
    `django.adapter`'s `urlpatterns` handling."""
    for dec in func_node.decorator_list:
        if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
            continue
        method = dec.func.attr
        if method not in HTTP_METHODS:
            continue
        var_resolved = resolve_expr(dec.func.value, ctx)
        if var_resolved.qualified_name not in apps_and_routers:
            continue
        if not dec.args or not (
            isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str)
        ):
            continue
        response_model_qn: str | None = None
        for kw in dec.keywords:
            if kw.arg == "response_model":
                response_model_qn = resolve_expr(kw.value, ctx).qualified_name
        return _RouteMatch(
            method=method.upper(),
            var_qualified_name=var_resolved.qualified_name,
            path=dec.args[0].value,
            response_model_qualified_name=response_model_qn,
        )
    return None


def _defaults_by_arg_name(args: ast.arguments) -> dict[str, ast.expr]:
    pairs: dict[str, ast.expr] = {}
    positional = [*args.posonlyargs, *args.args]
    offset = len(positional) - len(args.defaults)
    for arg, default in zip(positional[offset:], args.defaults, strict=True):
        pairs[arg.arg] = default
    for kwarg, kw_default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        if kw_default is not None:
            pairs[kwarg.arg] = kw_default
    return pairs


def _find_dependencies(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef, ctx: ResolutionContext
) -> list[tuple[str, str | None, int]]:
    """Every parameter defaulted to `Depends(...)`, resolved to whatever
    callable was passed in -- `None` if that callable does not resolve,
    reported anyway (uncertainty is data, not a reason to say nothing)."""
    deps: list[tuple[str, str | None, int]] = []
    for arg_name, default in _defaults_by_arg_name(func_node.args).items():
        if not isinstance(default, ast.Call):
            continue
        callee = resolve_expr(default.func, ctx)
        if callee.qualified_name not in DEPENDS_CALLABLES:
            continue
        dep_qn = resolve_expr(default.args[0], ctx).qualified_name if default.args else None
        deps.append((arg_name, dep_qn, default.lineno))
    return deps


def _is_pydantic_model(qualified_name: str, index: PythonIndex) -> bool:
    return any(base in BASE_MODEL_CLASSES for base in index.inheritance.get(qualified_name, ()))


def _request_response_models(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef, ctx: ResolutionContext, index: PythonIndex
) -> tuple[str | None, str | None]:
    """The first parameter (excluding `self`/`cls` and anything defaulted to
    `Depends(...)`) whose annotation resolves to a known Pydantic model is
    the request contract; the return annotation, resolved the same way, is
    the response contract. Basic association only -- see the module
    docstring's "out of scope" note on field-level tracking."""
    defaults = _defaults_by_arg_name(func_node.args)
    request_qn: str | None = None
    for arg in [*func_node.args.posonlyargs, *func_node.args.args]:
        if arg.arg in ("self", "cls") or arg.annotation is None:
            continue
        default = defaults.get(arg.arg)
        if isinstance(default, ast.Call):
            callee = resolve_expr(default.func, ctx)
            if callee.qualified_name in DEPENDS_CALLABLES:
                continue
        annotated = resolve_expr(arg.annotation, ctx)
        if annotated.qualified_name and _is_pydantic_model(annotated.qualified_name, index):
            request_qn = annotated.qualified_name
            break

    response_qn: str | None = None
    if func_node.returns is not None:
        returned = resolve_expr(func_node.returns, ctx)
        if returned.qualified_name and _is_pydantic_model(returned.qualified_name, index):
            response_qn = returned.qualified_name

    return request_qn, response_qn


def _emit(
    result: ExtractionResult,
    *,
    system_id: SystemID,
    kind: str,
    payload: dict[str, object],
    summary: str,
    file: str,
    line: int | None,
    now: datetime,
) -> Observation:
    evidence = Evidence(
        system_id=system_id,
        origin=Origin.STATIC_ANALYSIS,
        source=SourceRef(provider="fastapi", reference=file, retrieved_at=now),
        summary=summary,
        locator=f"{file}:{line}" if line is not None else file,
        observed_at=now,
    )
    result.evidence.append(evidence)
    return Observation(
        system_id=system_id,
        adapter="fastapi@0.1.0",
        origin=Origin.STATIC_ANALYSIS,
        kind=kind,
        payload=payload,
        evidence_ids=[evidence.id],
        observed_at=now,
    )


def _detect_route_renames(
    observations: Sequence[Observation],
    *,
    apps_and_routers: dict[str, _AppOrRouter],
    includes: list[_Include],
    index: PythonIndex,
    local_names_by_module: dict[str, set[str]],
    current_routes: list[dict[str, object]],
) -> list[dict[str, object]]:
    """For each file that was `MODIFIED` in Git history, re-parse the file's old
    content and extract route declarations.

    A route evolution is proposed when:
    * The router variable qualified name matches (`var_qualified_name`)
    * The HTTP method matches (`http_method`)
    * The handler function qualified name matches (`handler_qualified_name`)
    * The route path changed (`old_full_path != new_full_path`)

    If the handler, router, or method changed, or if there is ambiguity, no rename
    fact is emitted.
    """
    old_content_by_file: dict[str, str] = {}
    for obs in observations:
        if obs.kind != "git.file_change" or obs.payload.get("status") != "MODIFIED":
            continue
        old_content = obs.payload.get("old_content")
        path = obs.payload.get("path")
        if isinstance(old_content, str) and isinstance(path, str):
            old_content_by_file[path] = old_content
    if not old_content_by_file:
        return []

    current_by_anchor: dict[tuple[str, str, str, str], dict[str, object]] = {}
    for r in current_routes:
        file_rel = str(r["file"])
        var_qn = str(r["var_qualified_name"])
        method = str(r["http_method"])
        handler_qn = str(r["handler_qualified_name"])
        current_by_anchor[(file_rel, var_qn, method, handler_qn)] = r

    renames: list[dict[str, object]] = []

    for file_rel, old_content in old_content_by_file.items():
        module_qn = next((m for m, f in index.module_file.items() if f == file_rel), None)
        if module_qn is None:
            continue
        try:
            old_tree = ast.parse(old_content)
        except SyntaxError:
            continue

        ctx = _augmented_context(index, module_qn, local_names_by_module)
        old_apps_routers = dict(apps_and_routers)
        for var_name, info in _find_app_and_router_instances(old_tree, ctx):
            var_qn = f"{module_qn}.{var_name}"
            old_apps_routers[var_qn] = info

        old_routes: list[dict[str, object]] = []
        for func_qn, func_node in _walk_functions(old_tree, module_qn):
            route = _match_route_decorator(func_node, ctx, old_apps_routers)
            if route is not None:
                method, var_qn, path = route.method, route.var_qualified_name, route.path
                full_path = _composed_prefix(var_qn, old_apps_routers, includes) + path
                old_routes.append(
                    {
                        "file": file_rel,
                        "var_qualified_name": var_qn,
                        "http_method": method,
                        "path": full_path,
                        "handler_qualified_name": func_qn,
                        "line": func_node.lineno,
                    }
                )

        for old_r in old_routes:
            var_qn = str(old_r["var_qualified_name"])
            method = str(old_r["http_method"])
            handler_qn = str(old_r["handler_qualified_name"])
            old_full_path = str(old_r["path"])

            anchor = (file_rel, var_qn, method, handler_qn)
            new_r = current_by_anchor.get(anchor)
            if new_r is None:
                continue

            new_full_path = str(new_r["path"])
            if old_full_path == new_full_path:
                continue

            old_route_qn = f"{var_qn}:{method} {old_full_path}"
            new_route_qn = f"{var_qn}:{method} {new_full_path}"

            renames.append(
                {
                    "container_qualified_name": var_qn,
                    "old_qualified_name": old_route_qn,
                    "new_qualified_name": new_route_qn,
                    "shape_matched": True,
                    "old_path": old_full_path,
                    "new_path": new_full_path,
                    "handler_qualified_name": handler_qn,
                    "http_method": method,
                    "file": file_rel,
                    "line": new_r.get("line"),
                }
            )

    return renames
