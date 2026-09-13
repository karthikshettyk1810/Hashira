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
from ...ports.adapters import (
    AdapterCapabilities,
    ExtractionResult,
    Limitation,
    LimitationKind,
    LimitationScope,
)
from .._python_index import PythonIndex, PythonTreeCache, find_class_node
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
            known_limitations=[
                Limitation(
                    kind=LimitationKind.FRAMEWORK_REFLECTION,
                    scope=LimitationScope.FRAMEWORK_SERIALIZATION,
                    detail=(
                        "A DRF serializer/admin/signal reading a mapped model "
                        "attribute via Django's own reflection mechanisms "
                        "(ModelSerializer field introspection, admin "
                        "list_display, get_FOO_display(), signal receivers "
                        "connected at runtime) rather than an explicit "
                        "`.field` access in source is not represented as a "
                        "READS/WRITES edge. Mirrors `FastAPIAdapter`'s own "
                        "declaration for Pydantic's orm_mode -- the "
                        "equivalent structural gap for Django's own "
                        "reflection surface, previously undeclared entirely."
                    ),
                ),
            ],
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

        # `MODEL_BASES`/`VIEW_BASES` expanded to a fixpoint over each
        # class's own *resolved* base -- a real-repository finding: a
        # project's own multi-level custom abstract base (`class
        # TimestampedModel(UUIDModel)`, `class UUIDModel(models.Model)`,
        # then every real domain model extending `TimestampedModel`) was
        # previously invisible entirely, since only a class's *direct*
        # base was ever checked against the literal seed set. Simpler than
        # `adapters/sqlalchemy/adapter.py`'s own fixpoint (`known_bases`):
        # Django has no equivalent of the factory-declaration style that
        # needs re-parsing class bodies, so a single pass over
        # `index.inheritance` -- Python's own observations, already
        # correctly resolved -- is sufficient.
        known_model_bases = _expand_known_bases(index, MODEL_BASES)
        known_view_bases = _expand_known_bases(index, VIEW_BASES)

        detected_models: dict[str, str] = {}  # class_qn -> module_qn
        detected_views: dict[str, str] = {}  # class_qn -> module_qn
        for class_qn, module_qn in index.class_module.items():
            for base_qn in index.inheritance.get(class_qn, ()):
                if base_qn in known_model_bases:
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
                elif base_qn in known_view_bases:
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
            class_node = find_class_node(tree, class_qn.rsplit(".", 1)[-1])
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
                accesses, unresolved_accesses = _extract_field_accesses(
                    tree, module_qn, ctx, fields_by_model
                )
                for access in accesses:
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
                for unresolved in unresolved_accesses:
                    result.observations.append(
                        _emit(
                            result,
                            system_id=system_id,
                            kind="django.unresolved_field_access",
                            payload=unresolved,
                            summary=(
                                f"{unresolved['accessor_qualified_name']} "
                                f"accesses {unresolved['attribute_name']} dynamically "
                                f"({unresolved['limitation_kind']})"
                            ),
                            file=file_rel,
                            now=now,
                        )
                    )

        return result


def _expand_known_bases(index: PythonIndex, seed: frozenset[str]) -> set[str]:
    """`seed` (`MODEL_BASES`/`VIEW_BASES`) expanded to a fixpoint over every
    class's own resolved base -- a real-repository finding: a project's own
    multi-level custom abstract base (`class TimestampedModel(UUIDModel)`,
    `class UUIDModel(models.Model)`, then every real domain model extending
    `TimestampedModel`) was previously invisible entirely, since only a
    class's *direct* base was ever checked against the literal seed set --
    silently missing the majority of a real Django app's own models (a
    project's own shared abstract base is idiomatic, not a rare shape).
    Mirrors `adapters/sqlalchemy/adapter.py`'s own `known_bases` fixpoint,
    simplified: Django has no equivalent of the factory-declaration style
    (`Base = declarative_base()`) that needs re-parsing class bodies to see
    past a locally-assigned variable, so a single pass over
    `index.inheritance` -- Python's own observations, already correctly
    resolved for ordinary class-based inheritance -- is sufficient here."""
    known = set(seed)
    changed = True
    while changed:
        changed = False
        for class_qn, bases in index.inheritance.items():
            if class_qn in known:
                continue
            if any(base in known for base in bases):
                known.add(class_qn)
                changed = True
    return known


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


def _typed_parameter_model_instances(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    ctx: ResolutionContext,
    fields_by_model: dict[str, dict[str, str]],
) -> dict[str, str]:
    """A parameter's own type annotation (`def f(tour: Tour)`), resolved
    only against known Django models -- the second "typed object
    provenance" form (`_local_model_instances` above is the first),
    ported unchanged in spirit from `adapters/sqlalchemy/adapter.py`'s own
    `_typed_parameter_instances`. A real, high-prevalence gap this
    milestone's own real-repository verification found: a typed parameter
    is arguably *the* most common way a Django service function receives a
    model instance at all (a view or caller already looked it up), and
    was completely invisible to field-access tracking before this -- not
    a rare shape needing a narrower fix, but the dominant one."""
    types: dict[str, str] = {}
    params = [*func_node.args.posonlyargs, *func_node.args.args, *func_node.args.kwonlyargs]
    for param in params:
        if param.annotation is None:
            continue
        resolved = resolve_expr(param.annotation, ctx)
        if resolved.qualified_name in fields_by_model:
            types[param.arg] = resolved.qualified_name
    return types


def _queryset_local_instances(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    ctx: ResolutionContext,
    fields_by_model: dict[str, dict[str, str]],
) -> set[str]:
    """Detect `name = Model.objects.<method>(...)` -- a queryset-derived
    local alias.  Returns the set of names recognized as sourced from a
    known Django model's queryset, for *disclosure* only (not resolution).

    The detection is narrow and idiomatic: a call chain is model-derived
    only when the root of the chain resolves to a known model and the
    first attribute access after it is `.objects` -- Django's own manager
    convention.  `foo.bar.baz()` where `foo` merely happens to resolve to
    a known name is NOT matched unless `.objects` appears as the second
    component.  This avoids classifying arbitrary chains as model-derived
    merely because a root name happens to share a name with a model.

    Examples matched:
        tour = Tour.objects.filter(stop=stop).first()
        tour = Tour.objects.get(id=tour_id)
        tours = Tour.objects.all()

    Examples NOT matched (correctly silent):
        result = some_service.get_tour()  -- no `.objects` component
        foo = bar.baz()                   -- root is not a model
        tour = Tour()                     -- direct constructor, handled
                                            by `_local_model_instances`
    """
    names: set[str] = set()

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
        # Walk down the call chain to find the root: for
        # `Tour.objects.filter(...).first()`, the value is
        # Call(func=Attr(value=Call(func=Attr(value=Attr(
        #   value=Name('Tour'), attr='objects'), attr='filter')),
        #   attr='first'))
        # We peel away Call/Attribute wrappers to find the deepest
        # Attribute node whose value is a Name — and check that the
        # attribute is `.objects`.
        cursor: ast.expr = value
        while isinstance(cursor, ast.Call):
            cursor = cursor.func
        # cursor is now the outermost func (e.g. Tour.objects.filter(...).first)
        # We need to find a `.objects` attribute whose value resolves to
        # a known model.  Walk the Attribute chain down.
        if not isinstance(cursor, ast.Attribute):
            return
        # Collect the full attribute chain above a Name root.
        chain: list[str] = []
        node_cursor: ast.expr = cursor
        while isinstance(node_cursor, ast.Attribute):
            chain.append(node_cursor.attr)
            node_cursor = node_cursor.value
            # Skip over intermediate Call nodes in the chain
            # (e.g. filter(...) in Tour.objects.filter(...).first())
            while isinstance(node_cursor, ast.Call):
                node_cursor = node_cursor.func
        chain.reverse()  # now chain is e.g. ['objects', 'filter', 'first']
        if not isinstance(node_cursor, ast.Name):
            return
        # The root Name must resolve to a known model.
        resolved = resolve_expr(node_cursor, ctx)
        if resolved.qualified_name not in fields_by_model:
            return
        # The chain must start with `.objects` (Django manager convention).
        if not chain or chain[0] != "objects":
            return
        names.add(target.id)

    def walk(node: ast.AST) -> None:
        check(node)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            return
        for child in ast.iter_child_nodes(node):
            walk(child)

    for stmt in func_node.body:
        walk(stmt)
    return names


def _check_dynamic_attribute_call(
    call: ast.Call,
    func_qn: str,
    known_field_names: set[str],
    unresolved: list[dict[str, object]],
) -> None:
    """`getattr(x, "field")`/`setattr(x, "field", value)` naming a real
    model field by a literal string -- flagged as `DYNAMIC_ATTRIBUTE_ACCESS`
    regardless of whether `x`'s own type is separately resolvable. Ported
    from `adapters/sqlalchemy/adapter.py`'s own, already-proven mechanism
    (Resolution Integrity R3): Django's field-access extraction had no
    getattr/setattr detection at all -- a real, undisclosed gap relative to
    SQLAlchemy's own, more mature disclosure surface, not a hypothetical
    one. A non-literal second argument (`getattr(x, field_name)`) names
    nothing this adapter can check against a field list, so it is
    correctly not flagged -- supporting that would mean guessing at a
    runtime value, exactly what this adapter refuses to do."""
    if len(call.args) < 2 or not isinstance(call.args[0], ast.Name):
        return
    name_arg = call.args[1]
    if not (isinstance(name_arg, ast.Constant) and isinstance(name_arg.value, str)):
        return
    attribute_name = name_arg.value
    if attribute_name not in known_field_names:
        return
    unresolved.append(
        {
            "accessor_qualified_name": func_qn,
            "attribute_name": attribute_name,
            "limitation_kind": LimitationKind.DYNAMIC_ATTRIBUTE_ACCESS.value,
            "line": call.lineno,
        }
    )



def _chain_root_is_known_instance(
    attr_node: ast.Attribute,
    func_ctx: ResolutionContext,
    fields_by_model: dict[str, dict[str, str]],
) -> bool:
    """True when the root name of an attribute chain resolves to a known
    model instance in `func_ctx.local_instance_types`.

    For `stop.tour.status`, `attr_node` is `stop.tour` — the chain's root
    name is `stop`, checked against known model instances.  For deeper
    chains (`a.b.c.status`, `attr_node` is `a.b.c`) we walk down to the
    root `a`.

    This is the precision guard for chained-attribute disclosure: without
    it, `bar.tour.status` where `bar` is an unrelated object would produce
    a false limitation merely because `status` is a known field name.
    With it, the disclosure only fires when the chain root is itself a
    variable whose type we *do* know is a Django model — establishing
    model provenance for the chain, even though the intermediate hop
    (`tour`) is unresolvable."""
    cursor: ast.expr = attr_node
    while isinstance(cursor, ast.Attribute):
        cursor = cursor.value
    if not isinstance(cursor, ast.Name):
        return False
    return cursor.id in func_ctx.local_instance_types and (
        func_ctx.local_instance_types[cursor.id] in fields_by_model
    )


def _extract_field_accesses(
    tree: ast.Module,
    module_qn: str,
    ctx: ResolutionContext,
    fields_by_model: dict[str, dict[str, str]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Every `instance.field` read or write, for an `instance` whose type is
    a *local, direct* assignment to a known Django model
    (`payment = Payment(...)`), or a typed parameter whose annotation
    resolves to a known model (`def f(payment: Payment)`).

    Unresolved patterns — emitted as `django.unresolved_field_access`:

    - `getattr`/`setattr` calls naming a real field by a literal string
      (`_check_dynamic_attribute_call`), tagged `DYNAMIC_ATTRIBUTE_ACCESS`.

    - **Queryset-derived local aliases** (`tour = Tour.objects.filter(...)
      .first()` then `tour.status`): `tour` is recognized as model-sourced
      through `_queryset_local_instances` (narrowly: root resolves to a
      known model and the chain passes through `.objects`), disclosed as
      `RETURN_VALUE_PROVENANCE`.

    - **Chained attribute access on a known model instance**
      (`stop.tour.status` where `stop` is a typed parameter or local
      instance of a known model): the base is an `ast.Attribute` whose
      own root is a known model instance, but the intermediate hop
      (`.tour`) is unresolvable — disclosed as `RETURN_VALUE_PROVENANCE`.
      Only fires when the chain root is a name whose type is known (a
      resolved model instance), not for arbitrary chained accesses where
      the field name coincidentally matches a model field."""
    accesses: list[dict[str, object]] = []
    unresolved: list[dict[str, object]] = []
    known_field_names = {name for fields in fields_by_model.values() for name in fields}

    def collect(
        body: list[ast.stmt],
        func_qn: str,
        func_ctx: ResolutionContext,
        queryset_names: set[str],
    ) -> None:
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
                    elif (
                        child.attr in known_field_names
                        and isinstance(child.value, ast.Name)
                        and child.value.id in queryset_names
                    ):
                        # Queryset-local-alias: `tour = Tour.objects
                        # .filter(...).first()` then `tour.status` --
                        # `tour`'s type is model-sourced (through the
                        # `.objects` manager) but not resolvable to a
                        # specific model instance type without tracking
                        # queryset return values.
                        unresolved.append(
                            {
                                "accessor_qualified_name": func_qn,
                                "attribute_name": child.attr,
                                "limitation_kind": LimitationKind.RETURN_VALUE_PROVENANCE.value,
                                "line": child.lineno,
                            }
                        )
                    elif (
                        child.attr in known_field_names
                        and isinstance(child.value, ast.Attribute)
                        and _chain_root_is_known_instance(child.value, func_ctx, fields_by_model)
                    ):
                        # Chained attribute access on a known model
                        # instance: `stop.tour.status` where `stop` is
                        # a typed parameter or local instance of a known
                        # model.  The intermediate `.tour` hop is
                        # unresolvable (would require FK traversal),
                        # but the chain root's model provenance is
                        # established.
                        unresolved.append(
                            {
                                "accessor_qualified_name": func_qn,
                                "attribute_name": child.attr,
                                "limitation_kind": LimitationKind.RETURN_VALUE_PROVENANCE.value,
                                "line": child.lineno,
                            }
                        )
                elif isinstance(child, ast.Call):
                    if isinstance(child.func, ast.Name) and child.func.id in (
                        "getattr",
                        "setattr",
                    ):
                        _check_dynamic_attribute_call(child, func_qn, known_field_names, unresolved)
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
                param_types = _typed_parameter_model_instances(stmt, ctx, fields_by_model)
                local_types = _local_model_instances(stmt, ctx, fields_by_model)
                queryset_names = _queryset_local_instances(stmt, ctx, fields_by_model)
                # Remove names already resolved through constructor or
                # typed-parameter provenance — those are tracked, not
                # disclosed.
                queryset_names -= set(param_types) | set(local_types)
                func_ctx = ResolutionContext(
                    module_qualified_name=ctx.module_qualified_name,
                    imports=ctx.imports,
                    module_locals=ctx.module_locals,
                    local_instance_types={**param_types, **local_types},
                )
                collect(stmt.body, func_qn, func_ctx, queryset_names)
                walk_body(stmt.body, func_qn)

    walk_body(tree.body, module_qn)
    return accesses, unresolved



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
