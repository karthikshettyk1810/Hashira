"""`DjangoAdapter`: the `FrameworkAdapter` port, satisfied.

This adapter must not re-derive what the Python adapter already extracted —
classes, functions, methods, imports are Python's job, done once
(`adapters/python/extractor.py`), and this file consumes that output rather
than re-parsing source for it. What it *does* need to parse for itself is
genuinely Django-specific and has no Python-adapter counterpart at all:

* a model's **fields** (`status = models.CharField(...)`) — the Python
  extractor only extracts classes/functions/methods, never class-body
  attribute assignments, because that is not a Python-general concept worth
  the core extractor's complexity;
* **URL patterns** (`urlpatterns = [path(...), ...]`) — a plain module-level
  list literal with no Python-symbol shape at all;
* **field access** (`payment.status = ...` / `... payment.status`) — the
  Python extractor never tracks attribute reads/writes, only calls.

For all three, this adapter re-parses the specific files involved with `ast`
— but reuses `hashira.adapters.python.resolve.resolve_expr` for the actual
name resolution, and seeds its resolution context from the Python adapter's
own `python.import`/`python.symbol` observations rather than re-deriving
import bindings independently. The line is: reuse Python's *understanding of
structure*, do only the Django-*specific* parsing Python had no reason to do.

**Detection is evidence-based, not name-based.** A class is a Django model
only because a `python.inheritance` observation resolves its base to
`django.db.models.Model` — never because its name ends in "Model" (see
`known_bases.py`'s docstring). The same discipline the identity ladder
applies to entity identity applies here to framework detection.

**This adapter produces only `Observation`s**, exactly like `PythonAdapter` —
never `Entity`/`Relationship` objects directly. Turning `django.*`
observations into graph entities is `normalizer.py`'s job (Stage 2, run
*after* Python's own normalization, so it can link a URL route to the view
class Python already resolved).
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

from ...core.base import SourceRef
from ...core.enums import Origin
from ...core.evidence import Evidence, Observation
from ...core.ids import SystemID
from ...ports.adapters import AdapterCapabilities, ExtractionResult
from .._python_index import PythonIndex, PythonTreeCache
from ..python.resolve import ResolutionContext, resolve_expr
from .known_bases import MODEL_BASES, MODEL_FIELD_MODULE_PREFIX, VIEW_BASES

__all__ = ["DjangoAdapter"]

_URL_FUNCTIONS = frozenset(
    {"django.urls.path", "django.urls.re_path", "django.conf.urls.url", "django.conf.urls.re_path"}
)


class DjangoAdapter:
    """Model/view/field/URL-route/field-access evidence for a Django project."""

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            name="django",
            version="0.1.0",
            ir_versions=["0.1.2"],
            languages=["python"],
            frameworks=["django"],
            # No entity_types/relationship_types: like GitAdapter, this
            # produces Observations only -- normalizer.py builds the graph.
            requires_network=False,
        )

    def detect(self, root: Path) -> bool:
        """Cheap: a `manage.py` at the project root is Django's own
        convention for "this is a Django project" — no parsing needed."""
        return (root / "manage.py").is_file()

    def enrich(
        self, root: Path, base: ExtractionResult, *, system_id: SystemID, revision: str | None
    ) -> ExtractionResult:
        """Return *only the additions* — new `django.*` observations found
        by combining Python's own observations with this adapter's own
        targeted parsing. The caller merges this into the run; `base`'s own
        content is not echoed back."""
        now = datetime.now(UTC)
        index = PythonIndex.build(base)
        result = ExtractionResult()

        detected_models: dict[str, str] = {}  # class_qn -> module_qn
        detected_views: dict[str, str] = {}  # class_qn -> module_qn
        for class_qn, module_qn in index.class_module.items():
            for base_qn in index.inheritance.get(class_qn, ()):
                if base_qn in MODEL_BASES:
                    detected_models[class_qn] = module_qn
                    result.observations.append(
                        _emit(
                            result,
                            system_id=system_id,
                            kind="django.model",
                            payload={
                                "class_qualified_name": class_qn,
                                "module_qualified_name": module_qn,
                            },
                            summary=f"{class_qn} is a Django model",
                            file=index.symbol_file.get(class_qn, ""),
                            now=now,
                        )
                    )
                elif base_qn in VIEW_BASES:
                    detected_views[class_qn] = module_qn
                    result.observations.append(
                        _emit(
                            result,
                            system_id=system_id,
                            kind="django.view",
                            payload={
                                "class_qualified_name": class_qn,
                                "module_qualified_name": module_qn,
                                "base_qualified_name": base_qn,
                            },
                            summary=f"{class_qn} is a Django view (extends {base_qn})",
                            file=index.symbol_file.get(class_qn, ""),
                            now=now,
                        )
                    )

        trees = PythonTreeCache(root)

        fields_by_model: dict[str, dict[str, str]] = {}
        for class_qn, module_qn in detected_models.items():
            file_rel = index.symbol_file.get(class_qn)
            if file_rel is None:
                continue
            tree = trees.get(file_rel)
            if tree is None:
                result.errors.append(f"{file_rel}: could not parse for field detection")
                continue
            class_node = _find_class(tree, class_qn.rsplit(".", 1)[-1])
            if class_node is None:
                continue
            ctx = index.context_for(module_qn)
            model_fields: dict[str, str] = {}
            for field_name, field_type_qn, line in _extract_model_fields(class_node, ctx):
                model_fields[field_name] = field_type_qn
                result.observations.append(
                    _emit(
                        result,
                        system_id=system_id,
                        kind="django.model_field",
                        payload={
                            "model_qualified_name": class_qn,
                            "field_name": field_name,
                            "field_type_qualified_name": field_type_qn,
                            "line": line,
                        },
                        summary=f"{class_qn}.{field_name}: {field_type_qn.rsplit('.', 1)[-1]}",
                        file=file_rel,
                        now=now,
                    )
                )
            if model_fields:
                fields_by_model[class_qn] = model_fields

        for module_qn, file_rel in index.module_file.items():
            tree = trees.get(file_rel)
            if tree is None:
                continue
            ctx = index.context_for(module_qn)
            for route in _extract_url_patterns(tree, ctx):
                result.observations.append(
                    _emit(
                        result,
                        system_id=system_id,
                        kind="django.url_route",
                        payload={**route, "urls_module_qualified_name": module_qn},
                        summary=f"route {route['route']!r} -> {route['view_expr']}",
                        file=file_rel,
                        now=now,
                    )
                )

        if fields_by_model:
            for module_qn, file_rel in index.module_file.items():
                tree = trees.get(file_rel)
                if tree is None:
                    continue
                ctx = index.context_for(module_qn)
                for access in _extract_field_accesses(tree, module_qn, ctx, fields_by_model):
                    access_kind = str(access["access_kind"])
                    result.observations.append(
                        _emit(
                            result,
                            system_id=system_id,
                            kind="django.field_access",
                            payload=access,
                            summary=(
                                f"{access['accessor_qualified_name']} "
                                f"{access_kind.lower()}s "
                                f"{access['model_qualified_name']}.{access['field_name']}"
                            ),
                            file=file_rel,
                            now=now,
                        )
                    )

        return result


def _find_class(tree: ast.Module, simple_name: str) -> ast.ClassDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == simple_name:
            return node
    return None


def _extract_model_fields(
    class_node: ast.ClassDef, ctx: ResolutionContext
) -> list[tuple[str, str, int]]:
    """`name = models.SomeField(...)` at class-body level only — not inside
    a method, and not a plain non-call assignment (a docstring, a Meta
    inner class, a class-level constant unrelated to Django)."""
    fields: list[tuple[str, str, int]] = []
    for stmt in class_node.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
        ):
            target, value = stmt.targets[0], stmt.value
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.value is not None
        ):
            target, value = stmt.target, stmt.value
        if target is None or not isinstance(value, ast.Call):
            continue
        assert isinstance(target, ast.Name)
        resolved = resolve_expr(value.func, ctx)
        if resolved.qualified_name and resolved.qualified_name.startswith(
            MODEL_FIELD_MODULE_PREFIX
        ):
            fields.append((target.id, resolved.qualified_name, stmt.lineno))
    return fields


def _extract_url_patterns(tree: ast.Module, ctx: ResolutionContext) -> list[dict[str, object]]:
    routes: list[dict[str, object]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "urlpatterns"
            and isinstance(node.value, ast.List | ast.Tuple)
        ):
            continue
        for elt in node.value.elts:
            if not isinstance(elt, ast.Call):
                continue
            call_resolved = resolve_expr(elt.func, ctx)
            if call_resolved.qualified_name not in _URL_FUNCTIONS:
                continue
            if not elt.args:
                continue
            route_arg = elt.args[0]
            route = (
                route_arg.value
                if isinstance(route_arg, ast.Constant) and isinstance(route_arg.value, str)
                else None
            )
            if route is None:
                continue
            name = None
            for kw in elt.keywords:
                if (
                    kw.arg == "name"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ):
                    name = kw.value.value

            view_expr: ast.expr | None = elt.args[1] if len(elt.args) > 1 else None
            if (
                isinstance(view_expr, ast.Call)
                and isinstance(view_expr.func, ast.Attribute)
                and view_expr.func.attr == "as_view"
            ):
                view_expr = view_expr.func.value  # unwrap ClassName.as_view() -> ClassName
            view_resolved = resolve_expr(view_expr, ctx) if view_expr is not None else None

            routes.append(
                {
                    "route": route,
                    "name": name,
                    "view_expr": view_resolved.text if view_resolved else None,
                    "resolved_view_qualified_name": (
                        view_resolved.qualified_name if view_resolved else None
                    ),
                    "line": node.lineno,
                }
            )
    return routes


def _local_model_instances(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    ctx: ResolutionContext,
    fields_by_model: dict[str, dict[str, str]],
) -> dict[str, str]:
    """`name = ModelClass(...)` within this function, resolved only against
    known Django models — mirrors `adapters.python.extractor._local_instance_types`'s
    pattern (a local var assigned the result of a constructor call), scoped
    down to models specifically since that is all this adapter needs."""
    types: dict[str, str] = {}

    def check(node: ast.AST) -> None:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target, value = node.targets[0], node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            target, value = node.target, node.value
        if target is None or not isinstance(value, ast.Call):
            return
        assert isinstance(target, ast.Name)
        resolved = resolve_expr(value.func, ctx)
        if resolved.qualified_name in fields_by_model:
            types[target.id] = resolved.qualified_name

    def walk(node: ast.AST) -> None:
        check(node)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            return
        for child in ast.iter_child_nodes(node):
            walk(child)

    for stmt in func_node.body:
        walk(stmt)
    return types


def _extract_field_accesses(
    tree: ast.Module,
    module_qn: str,
    ctx: ResolutionContext,
    fields_by_model: dict[str, dict[str, str]],
) -> list[dict[str, object]]:
    """Every `instance.field` read or write, for an `instance` whose type is
    a *local, direct* assignment to a known Django model
    (`payment = Payment(...)`). `self.attr` access to an instance attribute
    is not tracked in v0.1 — only local variables, mirroring
    `adapters.python.extractor`'s own `LOCAL_INSTANCE` scope. A field access
    through anything else (an attribute chain, a function parameter, a
    dict/list element) stays unresolved rather than guessed at."""
    accesses: list[dict[str, object]] = []

    def collect(body: list[ast.stmt], func_qn: str, func_ctx: ResolutionContext) -> None:
        def walk(node: ast.AST) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.Attribute):
                    base_resolved = resolve_expr(child.value, func_ctx)
                    model_qn = base_resolved.qualified_name
                    if model_qn in fields_by_model and child.attr in fields_by_model[model_qn]:
                        if isinstance(child.ctx, ast.Store):
                            kind = "WRITE"
                        elif isinstance(child.ctx, ast.Load):
                            kind = "READ"
                        else:
                            kind = None
                        if kind:
                            accesses.append(
                                {
                                    "accessor_qualified_name": func_qn,
                                    "model_qualified_name": model_qn,
                                    "field_name": child.attr,
                                    "access_kind": kind,
                                    "line": child.lineno,
                                }
                            )
                if isinstance(
                    child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda
                ):
                    continue
                walk(child)

        for stmt in body:
            walk(stmt)

    def walk_body(body: list[ast.stmt], qualified_name: str) -> None:
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                walk_body(stmt.body, f"{qualified_name}.{stmt.name}")
            elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                func_qn = f"{qualified_name}.{stmt.name}"
                local_types = _local_model_instances(stmt, ctx, fields_by_model)
                func_ctx = ResolutionContext(
                    module_qualified_name=ctx.module_qualified_name,
                    imports=ctx.imports,
                    module_locals=ctx.module_locals,
                    local_instance_types=local_types,
                )
                collect(stmt.body, func_qn, func_ctx)
                walk_body(stmt.body, func_qn)

    walk_body(tree.body, module_qn)
    return accesses


def _emit(
    result: ExtractionResult,
    *,
    system_id: SystemID,
    kind: str,
    payload: dict[str, object],
    summary: str,
    file: str,
    now: datetime,
) -> Observation:
    evidence = Evidence(
        system_id=system_id,
        origin=Origin.STATIC_ANALYSIS,
        source=SourceRef(provider="django", reference=file, retrieved_at=now),
        summary=summary,
        locator=f"{file}:{payload.get('line', '')}",
        observed_at=now,
    )
    result.evidence.append(evidence)
    return Observation(
        system_id=system_id,
        adapter="django@0.1.0",
        origin=Origin.STATIC_ANALYSIS,
        kind=kind,
        payload=payload,
        evidence_ids=[evidence.id],
        observed_at=now,
    )
