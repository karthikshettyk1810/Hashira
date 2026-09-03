"""`SQLAlchemyAdapter`: the `DataAdapter` port, satisfied.

**This is a `DataAdapter`, not a `FrameworkAdapter`.** It must never care
whether the code it is enriching belongs to a FastAPI app, a Django project,
a CLI, or nothing at all -- a declarative model means the same thing either
way. Concretely: this adapter reads only Python's own observations
(`_python_index.PythonIndex`, exactly like `adapters.django`/`adapters.fastapi`
do), never a `django.*`/`fastapi.*` observation kind. `tests/integration/
test_sqlalchemy_identity.py` proves this against a plain, framework-free
fixture first; `tests/integration/test_fastapi_sqlalchemy_together.py` then
plugs this same adapter, completely unmodified, into the FastAPI fixture.

**Same reuse discipline as every other enricher here, applied to a third
kind of framework surface.** Django detects via resolved inheritance;
FastAPI detects via resolved decorators and call expressions; SQLAlchemy
detects via a *declarative base* that a model class is rooted in, which
shows up two different ways in real code (see `known_symbols.py`):

* **class-based** (SQLAlchemy 2.0): `class Base(DeclarativeBase): pass`,
  then `class User(Base): ...` -- both resolvable through Python's own
  `python.inheritance` observations, since `DeclarativeBase` is always
  reached by import;
* **factory-based** (SQLAlchemy <2.0, still extremely common):
  `Base = declarative_base()`, then `class User(Base): ...` -- `Base` here
  is a plain module-level variable Python's extractor never tracks, so this
  adapter finds it itself, the same way `adapters.fastapi.adapter` finds
  `app = FastAPI()`.

Both styles feed one `known_bases` set, expanded to a fixpoint over
`python.inheritance` so a multi-level base hierarchy (a project's own
`TimestampedBase(Base)` mixin, say) still resolves. A class extending a
known base is only actually a *model* -- not an intermediate abstract base
-- if its own body declares `__tablename__`; `Base` and any mixin without
one are correctly never classified as tables.

**The ORM class and its table are not the same conceptual thing** (unlike a
FastAPI handler, which *is* the Python function). `normalizer.py` mints two
linked entities -- `EntityType.SYMBOL` for the class (tagged, not
duplicated, exactly like every other enricher's class-level rule) and a
fresh `EntityType.DATA_ENTITY` for the table, connected by `MAPS_TO`
(`core/enums.py`'s `RelationshipType` docstring has the full "why not an
existing type" reasoning). Columns are `EntityType.SYMBOL` entities
`CONTAINS`-related to the table, matching Django's model-field pattern;
`Column`/`mapped_column`'s `ForeignKey("table.column")` argument becomes a
`REFERENCES` edge between two column entities, resolved directly by string
match against `"table.column"` qualified names -- deliberately not against
`ForeignKey(Account.id)`'s attribute-reference form, which needs
cross-class resolution this milestone does not attempt.

**Basic read/write evidence only.** `_extract_field_accesses` mirrors
Django's `LOCAL_INSTANCE`-scoped pattern exactly (`payment = Payment()`,
then `payment.status = ...` / `... payment.status`) -- kept as this
adapter's own, independent copy rather than shared with Django's, on
purpose: the milestone this was built from was explicit that Django's model
handling should not be refactored into shared plumbing *yet*, only once a
second adapter's independent needs prove what is actually common (see
`docs/ROADMAP.md`'s entry on this milestone).

**Out of scope for v0.1**, matching the milestone's explicit "keep it
narrow": query-shape analysis, sessions/transactions, async SQLAlchemy,
Alembic migrations, raw SQL, hybrid properties, `relationship(...)`
construct parsing (SQLAlchemy's own ORM-level association helper -- "deep
relationship inference" was explicitly excluded; only a column's direct
`ForeignKey(...)` argument is read), and multi-hop `ForeignKey` chains
beyond a direct string reference.
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
from .._python_index import PythonIndex, PythonTreeCache, find_class_node
from ..python.resolve import ResolutionContext, resolve_expr
from .known_symbols import (
    COLUMN_CALLABLES,
    DECLARATIVE_BASE_FACTORIES,
    DECLARATIVE_BASE_SEED,
    FOREIGN_KEY_CALLABLE,
)

__all__ = ["SQLAlchemyAdapter"]

_MANIFEST_FILES = ("pyproject.toml", "requirements.txt", "Pipfile")


class SQLAlchemyAdapter:
    """Table/column/foreign-key/read-write evidence for a SQLAlchemy project."""

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            name="sqlalchemy",
            version="0.1.0",
            ir_versions=["0.1.4"],
            languages=["python"],
            frameworks=["sqlalchemy"],
            # No entity_types/relationship_types: like every other enricher
            # here, this produces Observations only -- normalizer.py builds
            # the graph.
            requires_network=False,
        )

    def detect(self, root: Path) -> bool:
        """Cheap: like FastAPI, SQLAlchemy has no marker file, so this
        checks declared dependencies rather than scanning source."""
        for name in _MANIFEST_FILES:
            path = root / name
            if not path.is_file():
                continue
            try:
                if "sqlalchemy" in path.read_text(encoding="utf-8", errors="ignore").lower():
                    return True
            except OSError:
                continue
        return False

    def enrich(
        self, root: Path, base: ExtractionResult, *, system_id: SystemID, revision: str | None
    ) -> ExtractionResult:
        """Return *only the additions* -- new `sqlalchemy.*` observations
        found by combining Python's own observations with this adapter's
        own targeted parsing. The caller merges this into the run; `base`'s
        own content is not echoed back."""
        now = datetime.now(UTC)
        index = PythonIndex.build(base)
        trees = PythonTreeCache(root)
        result = ExtractionResult()

        known_bases, local_names_by_module = _discover_declarative_bases(index, trees)
        if len(known_bases) <= 1:  # only the literal seed -- nothing SQLAlchemy-shaped found
            return result

        models: dict[str, dict[str, object]] = {}
        for class_qn, module_qn in index.class_module.items():
            file_rel = index.symbol_file.get(class_qn)
            if file_rel is None:
                continue
            tree = trees.get(file_rel)
            if tree is None:
                continue
            class_node = find_class_node(tree, class_qn.rsplit(".", 1)[-1])
            if class_node is None:
                continue
            ctx = _augmented_context(index, module_qn, local_names_by_module)
            matched_base = _matches_known_base(class_node, ctx, known_bases)
            if matched_base is None:
                continue
            table_name = _find_tablename(class_node)
            if table_name is None:
                continue  # an abstract declarative base/mixin, not a table
            models[class_qn] = {"module_qn": module_qn, "table_name": table_name, "file": file_rel}
            result.observations.append(
                _emit(
                    result,
                    system_id=system_id,
                    kind="sqlalchemy.model",
                    payload={
                        "class_qualified_name": class_qn,
                        "module_qualified_name": module_qn,
                        "table_name": table_name,
                        "base_qualified_name": matched_base,
                    },
                    summary=f"{class_qn} maps to table {table_name!r}",
                    file=file_rel,
                    line=class_node.lineno,
                    now=now,
                )
            )

        columns_by_model: dict[str, dict[str, str]] = {}  # class_qn -> {field_name: table.column}
        for class_qn, info in models.items():
            file_rel = str(info["file"])
            tree = trees.get(file_rel)
            if tree is None:
                continue
            class_node = find_class_node(tree, class_qn.rsplit(".", 1)[-1])
            if class_node is None:
                continue
            ctx = _augmented_context(index, str(info["module_qn"]), local_names_by_module)
            table_name = str(info["table_name"])
            model_columns: dict[str, str] = {}
            for column in _extract_columns(class_node, ctx):
                field_name = str(column["field_name"])
                model_columns[field_name] = f"{table_name}.{field_name}"
                result.observations.append(
                    _emit(
                        result,
                        system_id=system_id,
                        kind="sqlalchemy.column",
                        payload={
                            "class_qualified_name": class_qn,
                            "table_name": table_name,
                            **column,
                        },
                        summary=f"{table_name}.{field_name}",
                        file=file_rel,
                        line=int(column["line"]),  # type: ignore[call-overload]
                        now=now,
                    )
                )
            if model_columns:
                columns_by_model[class_qn] = model_columns

        if columns_by_model:
            for module_qn, file_rel in index.module_file.items():
                tree = trees.get(file_rel)
                if tree is None:
                    continue
                ctx = _augmented_context(index, module_qn, local_names_by_module)
                accesses = _extract_field_accesses(tree, module_qn, ctx, models, columns_by_model)
                for access in accesses:
                    access_kind = str(access["access_kind"])
                    result.observations.append(
                        _emit(
                            result,
                            system_id=system_id,
                            kind="sqlalchemy.field_access",
                            payload=access,
                            summary=(
                                f"{access['accessor_qualified_name']} "
                                f"{access_kind.lower()}s "
                                f"{access['column_qualified_name']}"
                            ),
                            file=file_rel,
                            line=int(access["line"]),  # type: ignore[call-overload]
                            now=now,
                        )
                    )

        return result


def _augmented_context(
    index: PythonIndex, module_qn: str, local_names_by_module: dict[str, set[str]]
) -> ResolutionContext:
    """A `Base = declarative_base()` assignment is never a `python.symbol`
    Python's own extractor tracked (it is not a class/function), so a
    locally-discovered declarative-base variable has to be added to the
    module-locals set by hand before `resolve_expr` can see it -- mirrors
    `adapters.fastapi.adapter`'s identical need for `app`/`router` vars."""
    base_ctx = index.context_for(module_qn)
    extra = local_names_by_module.get(module_qn, set())
    return ResolutionContext(
        module_qualified_name=module_qn,
        imports=base_ctx.imports,
        module_locals=base_ctx.module_locals | extra,
    )


def _discover_declarative_bases(
    index: PythonIndex, trees: PythonTreeCache
) -> tuple[set[str], dict[str, set[str]]]:
    """Every qualified name that counts as "a declarative base a model may
    extend" -- both the class-based style (`class Base(DeclarativeBase)`)
    and the factory-based style (`Base = declarative_base()`), expanded to
    a fixpoint so a multi-level hierarchy resolves regardless of which
    style introduced its root (a project's own `TimestampedBase(Base)`
    mixin, say). Returns `(known_bases, local_var_names_by_module)`; the
    latter is what makes the factory style's *subclasses* resolvable at all
    (see `_augmented_context`) -- Python's own `python.inheritance` cannot
    see past a locally-assigned `Base` variable, so this fixpoint always
    re-resolves each class's own bases itself rather than trusting that
    data, which is exactly why a single pass over `index.inheritance`
    cannot handle a hierarchy rooted in the factory style.
    """
    known: set[str] = {DECLARATIVE_BASE_SEED}
    local_names_by_module: dict[str, set[str]] = {}

    for module_qn, file_rel in index.module_file.items():
        tree = trees.get(file_rel)
        if tree is None:
            continue
        ctx = index.context_for(module_qn)
        for node in tree.body:
            if not (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)
            ):
                continue
            resolved = resolve_expr(node.value.func, ctx)
            if resolved.qualified_name in DECLARATIVE_BASE_FACTORIES:
                var_qn = f"{module_qn}.{node.targets[0].id}"
                known.add(var_qn)
                local_names_by_module.setdefault(module_qn, set()).add(node.targets[0].id)

    classes: list[tuple[str, str, ast.ClassDef]] = []
    for class_qn, module_qn in index.class_module.items():
        class_file = index.symbol_file.get(class_qn)
        if class_file is None:
            continue
        tree = trees.get(class_file)
        if tree is None:
            continue
        class_node = find_class_node(tree, class_qn.rsplit(".", 1)[-1])
        if class_node is not None:
            classes.append((class_qn, module_qn, class_node))

    changed = True
    while changed:
        changed = False
        for class_qn, module_qn, class_node in classes:
            if class_qn in known:
                continue
            ctx = _augmented_context(index, module_qn, local_names_by_module)
            if _matches_known_base(class_node, ctx, known) is not None:
                known.add(class_qn)
                changed = True

    return known, local_names_by_module


def _matches_known_base(
    class_node: ast.ClassDef, ctx: ResolutionContext, known_bases: set[str]
) -> str | None:
    for base_expr in class_node.bases:
        resolved = resolve_expr(base_expr, ctx)
        if resolved.qualified_name in known_bases:
            return resolved.qualified_name
    return None


def _find_tablename(class_node: ast.ClassDef) -> str | None:
    for stmt in class_node.body:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id == "__tablename__"
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            return stmt.value.value
    return None


def _bool_kwarg(call: ast.Call, name: str) -> bool:
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return True
    return False


def _foreign_key_target(call: ast.Call, ctx: ResolutionContext) -> str | None:
    """The `"table.column"` string out of a nested `ForeignKey(...)` call
    among this column's arguments, if any -- resolved the same
    evidence-based way as every other detection in this codebase (never a
    name-suffix guess), so a locally-defined callable that merely happens
    to be named `ForeignKey` is not mistaken for SQLAlchemy's. See
    `known_symbols.py` on why only the string-literal argument form is
    recognized."""
    for arg in call.args:
        if not isinstance(arg, ast.Call):
            continue
        resolved = resolve_expr(arg.func, ctx)
        if resolved.qualified_name != FOREIGN_KEY_CALLABLE:
            continue
        first = arg.args[0] if arg.args else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def _extract_columns(class_node: ast.ClassDef, ctx: ResolutionContext) -> list[dict[str, object]]:
    """`name = Column(...)` / `name: Mapped[...] = mapped_column(...)` at
    class-body level only -- not inside a method, matching
    `adapters.django.adapter._extract_model_fields`'s identical scope."""
    columns: list[dict[str, object]] = []
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
        if resolved.qualified_name not in COLUMN_CALLABLES:
            continue
        type_arg = value.args[0] if value.args else None
        columns.append(
            {
                "field_name": target.id,
                "is_primary_key": _bool_kwarg(value, "primary_key"),
                "foreign_key_target": _foreign_key_target(value, ctx),
                "column_type_text": ast.unparse(type_arg) if type_arg is not None else None,
                "line": stmt.lineno,
            }
        )
    return columns


def _local_model_instances(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    ctx: ResolutionContext,
    models: dict[str, dict[str, object]],
) -> dict[str, str]:
    """`name = ModelClass(...)` within this function, resolved only against
    known SQLAlchemy models -- mirrors
    `adapters.django.adapter._local_model_instances` (kept as an
    independent copy, deliberately not shared -- see the module docstring).
    """
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
        if resolved.qualified_name in models:
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
    models: dict[str, dict[str, object]],
    columns_by_model: dict[str, dict[str, str]],
) -> list[dict[str, object]]:
    """Every `instance.column` read or write, for an `instance` that is a
    *local, direct* assignment to a known model (`payment = Payment(...)`)
    -- mirrors `adapters.django.adapter._extract_field_accesses`'s scope
    exactly (kept independent; see the module docstring)."""
    accesses: list[dict[str, object]] = []

    def collect(body: list[ast.stmt], func_qn: str, func_ctx: ResolutionContext) -> None:
        def walk(node: ast.AST) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.Attribute):
                    base_resolved = resolve_expr(child.value, func_ctx)
                    model_qn = base_resolved.qualified_name
                    fields = columns_by_model.get(model_qn) if model_qn else None
                    if fields is not None and child.attr in fields:
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
                                    "column_qualified_name": fields[child.attr],
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
                local_types = _local_model_instances(stmt, ctx, models)
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
    line: int,
    now: datetime,
) -> Observation:
    evidence = Evidence(
        system_id=system_id,
        origin=Origin.STATIC_ANALYSIS,
        source=SourceRef(provider="sqlalchemy", reference=file, retrieved_at=now),
        summary=summary,
        locator=f"{file}:{line}",
        observed_at=now,
    )
    result.evidence.append(evidence)
    return Observation(
        system_id=system_id,
        adapter="sqlalchemy@0.1.0",
        origin=Origin.STATIC_ANALYSIS,
        kind=kind,
        payload=payload,
        evidence_ids=[evidence.id],
        observed_at=now,
    )
