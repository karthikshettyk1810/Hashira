"""AST walking for one Python file: source in, `Observation`/`Evidence` out.

This module never touches another file and never decides what anything
*means* at the system level — spec §18: an adapter reports what it saw. A call
this file cannot resolve stays a call with ``resolution: "UNRESOLVED"``; it
never becomes a fake entity or a guessed relationship (see `resolve.py`'s
docstring). Turning a resolved-looking observation into an actual `Entity`/
`Relationship`, checked against everything else in the indexing run, is
`normalizer.py`'s job, not this one's.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import UTC, datetime
from pathlib import Path

from ...core.base import SourceRef
from ...core.enums import Origin
from ...core.evidence import Evidence, Observation
from ...core.ids import SystemID
from .resolve import (
    ResolutionContext,
    bindings_for_import,
    bindings_for_import_from,
    function_local_imports,
    resolve_expr,
)

__all__ = ["ExtractedFile", "extract_file", "module_qualified_name"]


def module_qualified_name(file: Path, import_root: Path) -> str:
    """The dotted module name a file would have on ``sys.path`` rooted at
    ``import_root`` — e.g. ``src/shop/checkout.py`` under ``src/`` becomes
    ``shop.checkout``. Works for namespace packages too: nothing here
    requires an ``__init__.py`` to exist.

    A real repository crash (indexing a Django project's own settings
    package as the import root — ``<root>/__init__.py`` alongside
    ``settings.py``/``wsgi.py``, itself importable by the root directory's
    own name, while everything *under* it is imported unprefixed) surfaced
    the one input this computation cannot express: when ``file`` *is*
    ``import_root/__init__.py``, the path-relative name is empty by
    construction — the root is a package relative to itself. Falling back to
    the root directory's own name keeps this a real, syntactically-grounded
    identifier (not a fabricated one — it is genuinely importable as such
    one level up) instead of minting `Entity(name="")`, which crashed the
    entire indexing run rather than merely skipping one file.
    """
    rel = file.relative_to(import_root)
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return import_root.name
    return ".".join(parts)


@dataclasses.dataclass(frozen=True, slots=True)
class ExtractedFile:
    observations: list[Observation]
    evidence: list[Evidence]
    errors: list[str]


def extract_file(
    file: Path,
    *,
    import_root: Path,
    project_root: Path | None = None,
    system_id: SystemID,
    revision: str | None,
    now: datetime | None = None,
) -> ExtractedFile:
    """Parse one file and return everything this file, on its own, can say.

    ``import_root`` (e.g. a ``src/`` directory) computes the dotted module
    name; ``project_root`` (the repository root — defaults to ``import_root``
    when a caller has no better one, e.g. in isolated unit tests) computes
    the *file path* recorded on every observation and entity. These are
    deliberately different roots: a Git adapter always reports paths
    relative to the repository root, and every path this adapter records
    must agree with that or rename-evidence pairing (identity/git_evidence.py)
    silently finds nothing to pair.
    """
    module_qn = module_qualified_name(file, import_root)
    try:
        source = file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file))
    except (SyntaxError, UnicodeDecodeError) as exc:
        return ExtractedFile(
            observations=[], evidence=[], errors=[f"{file}: could not parse ({exc})"]
        )

    walker = _Walker(
        module_qn=module_qn,
        file=file,
        project_root=project_root or import_root,
        system_id=system_id,
        revision=revision,
        now=now or datetime.now(UTC),
    )
    walker.run(tree)
    return ExtractedFile(observations=walker.observations, evidence=walker.evidence, errors=[])


# Node types that open a new lexical scope; a module-level import/def scan
# must not descend into them when looking for *module*-level bindings.
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _module_level_locals(module: ast.Module) -> set[str]:
    return {
        node.name
        for node in module.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _module_level_imports(
    module_qn: str, module: ast.Module, *, is_package_init: bool = False
) -> dict[str, str]:
    """Import bindings visible at module scope, found anywhere outside a
    function/class body (so a module-level `if`/`try` guarding an import
    still counts, matching how these are almost always used in practice).
    ``is_package_init`` — see `resolve.py`'s `_package_of` — must be true
    when this file is a package's own ``__init__.py``, so a relative import
    written there resolves against the right anchor."""
    imports: dict[str, str] = {}

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import):
                for binding in bindings_for_import(child):
                    imports[binding.bound_name] = binding.target
            elif isinstance(child, ast.ImportFrom):
                for binding in bindings_for_import_from(
                    module_qn, child, is_package_init=is_package_init
                ):
                    imports[binding.bound_name] = binding.target
            elif not isinstance(child, _SCOPE_NODES):
                walk(child)

    walk(module)
    return imports


def _constructor_call(value: ast.expr) -> ast.Call | None:
    """The bare constructor call `value` mechanically reduces to, if any --
    either `value` itself, or, for the fallback-default idiom (`provided or
    Constructor()`, e.g. `self._mcube = mcube_client or MCubeClient()`), the
    last operand of an `or` chain when that last operand is itself a bare
    call. A real production repository's dominant DI idiom (found by the
    "Real Repository Pilot" benchmark, `docs/ROADMAP.md`) turned out to be
    exactly this shape, not the plain `x = Cls()` form `_local_instance_types`/
    `_self_attribute_types` already handled. Nothing else reduces: no
    ternaries, no `and`, no arbitrary expression evaluation -- if the last
    operand isn't a literal call, this returns `None`, the same as any other
    unresolvable shape.

    Caller beware, and this is by design, not an oversight: this function
    only ever hands back a `Call` node, never a verdict on whether that
    call's callee is actually a *class*. `resolve.py`'s own docstring on
    this exact widening explains why a factory-function fallback
    (`settings or get_settings()`) still produces a syntactically-derived
    but semantically empty qualified name -- distinguishing "class" from
    "function that returns one" needs the imported name's own definition,
    which is a different file, which this stage never reads."""
    if isinstance(value, ast.Call):
        return value
    if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or) and value.values:
        last = value.values[-1]
        if isinstance(last, ast.Call):
            return last
    return None


def _local_instance_types(
    func: ast.FunctionDef | ast.AsyncFunctionDef, ctx: ResolutionContext
) -> dict[str, str]:
    """`name = ClassName(...)` (or `name = provided or ClassName(...)` --
    see `_constructor_call`) assignments anywhere in the function's own
    body, resolved only against imports/module-locals (never against other
    locals, so ordering ambiguity can't compound into a wrong chain of
    guesses). Nested defs are not descended into: their assignments belong to
    their own scope, not this one."""
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
        call = _constructor_call(value) if value is not None else None
        if target is not None and call is not None:
            resolved = resolve_expr(call.func, ctx)
            if resolved.qualified_name is not None and resolved.resolution in (
                "IMPORT",
                "MODULE_LOCAL",
            ):
                assert isinstance(target, ast.Name)
                types[target.id] = resolved.qualified_name

    def walk(node: ast.AST) -> None:
        check(node)
        if isinstance(node, _SCOPE_NODES):
            return
        for child in ast.iter_child_nodes(node):
            walk(child)

    for stmt in func.body:
        walk(stmt)
    return types


def _self_attribute_types(class_node: ast.ClassDef, ctx: ResolutionContext) -> dict[str, str]:
    """`self.<attr> = KnownCallable(...)` / `self.<attr>: T = KnownCallable(...)`
    (or the `provided or KnownCallable(...)` fallback-default idiom -- see
    `_constructor_call`) assignments in the class's own `__init__`, resolved
    the same restricted way `_local_instance_types` resolves a same-function
    local: only against imports/module-locals, never against another guess.
    Scoped to `__init__` alone, deliberately -- see `resolve.py`'s
    `SELF_ATTRIBUTE` docstring for why this stays a bounded fix rather than
    general attribute-flow analysis.
    """
    init = next(
        (
            node
            for node in class_node.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__init__"
        ),
        None,
    )
    if init is None:
        return {}

    types: dict[str, str] = {}

    def check(node: ast.AST) -> None:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Attribute)
            and isinstance(node.targets[0].value, ast.Name)
            and node.targets[0].value.id == "self"
        ):
            target, value = node.targets[0], node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Attribute)
            and isinstance(node.target.value, ast.Name)
            and node.target.value.id == "self"
            and node.value is not None
        ):
            target, value = node.target, node.value
        call = _constructor_call(value) if value is not None else None
        if target is not None and call is not None:
            assert isinstance(target, ast.Attribute)
            resolved = resolve_expr(call.func, ctx)
            if resolved.qualified_name is not None and resolved.resolution in (
                "IMPORT",
                "MODULE_LOCAL",
            ):
                types[target.attr] = resolved.qualified_name

    def walk(node: ast.AST) -> None:
        check(node)
        if isinstance(node, _SCOPE_NODES):
            return
        for child in ast.iter_child_nodes(node):
            walk(child)

    for stmt in init.body:
        walk(stmt)
    return types


def _annotated_inner_type(annotation: ast.expr) -> ast.expr:
    """`Annotated[T, ...]` (FastAPI's own `Depends(...)`/`Query(...)` idiom)
    reduces to `T`; anything else is its own inner type, unchanged. Detected
    syntactically -- the subscripted name/attribute's own text is
    "Annotated" -- not via import resolution, matching how lightly this
    module already treats syntactic shape elsewhere (e.g. `self`/`cls` by
    name in `resolve.py`). No attempt is made to resolve `Depends(...)`'s
    own callable or any other metadata argument -- only the first, type
    position of the `Annotated[...]` subscript."""
    if (
        isinstance(annotation, ast.Subscript)
        and isinstance(annotation.slice, ast.Tuple)
        and annotation.slice.elts
    ):
        head = annotation.value
        head_name = (
            head.attr
            if isinstance(head, ast.Attribute)
            else head.id
            if isinstance(head, ast.Name)
            else None
        )
        if head_name == "Annotated":
            return annotation.slice.elts[0]
    return annotation


def _parameter_instance_types(
    func: ast.FunctionDef | ast.AsyncFunctionDef, ctx: ResolutionContext
) -> dict[str, str]:
    """Every ordinary parameter's own type annotation (`Annotated[T, ...]`
    reduced to `T` first -- `_annotated_inner_type`), resolved exactly as
    restrictively as `_local_instance_types` resolves a same-function local:
    only against imports/module-locals, never a guess layered on a guess.

    A real production repository's route handlers -- `def receive(inbound:
    IvrWebhookInbound, service: Annotated[IvrService, Depends(...)]):
    inbound.resolve(...)` -- turned out to depend entirely on this: a plain
    typed parameter calling a method on itself is arguably *the* most common
    shape in any framework's request-handling code, and nothing in this
    adapter resolved it before (`docs/ROADMAP.md`'s "Real Repository Pilot
    v0.1, Phase 2" continuation has the full account). This is deliberately
    a general Python-level fix, separate from `adapters/sqlalchemy`'s own
    typed-parameter mechanism (`_typed_parameter_instances`), which exists
    only to detect ORM field reads/writes on a narrower set of known model
    classes -- this one feeds ordinary `CALLS` resolution for any
    IMPORT/MODULE_LOCAL-resolvable type, model or not.

    A parameter with no annotation, or one that doesn't resolve to
    IMPORT/MODULE_LOCAL (an unannotated parameter, a builtin type, a type
    variable, anything this adapter can't confirm against this file's own
    imports/module-locals), is simply absent from the result -- never
    guessed, never recorded as anything other than what it is: unresolved.
    """
    types: dict[str, str] = {}
    params = [*func.args.posonlyargs, *func.args.args, *func.args.kwonlyargs]
    for param in params:
        if param.annotation is None:
            continue
        inner = _annotated_inner_type(param.annotation)
        resolved = resolve_expr(inner, ctx)
        if resolved.qualified_name is not None and resolved.resolution in (
            "IMPORT",
            "MODULE_LOCAL",
        ):
            types[param.arg] = resolved.qualified_name
    return types


def _module_level_instance_assignments(
    module: ast.Module,
) -> list[tuple[ast.stmt, str, ast.expr]]:
    """`name = ClassName(...)` (or the `provided or ClassName()` fallback
    idiom -- see `_constructor_call`) assignments at module scope -- e.g. a
    singleton client (`kafka_publisher = KafkaEventPublisher()`, exported for
    other modules to `from x import kafka_publisher` and call methods on).
    Real-repository finding: this is a common way Python codebases share a
    client/publisher/settings instance, and Stage 1's own IMPORT resolution
    of `kafka_publisher.push_notification(...)` in a *different* file
    produces the syntactically literal-but-wrong qualified name
    `common.kafka.kafka_publisher.push_notification` -- Stage 1 has no way
    to know the imported name is an *instance* of a class rather than a
    class/function/submodule itself, without reading the defining file,
    which its own contract rules out. Found anywhere outside a function/class
    body, including inside a module-level `if`/`try` (mirroring
    `_module_level_imports`' own guarded-import handling) -- only the
    `(stmt, name, value)` triple is returned; resolving `value`'s callee
    against this module's own imports/module-locals is the caller's job,
    with the right `ResolutionContext` already in hand."""
    found: list[tuple[ast.stmt, str, ast.expr]] = []

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if (
                isinstance(child, ast.Assign)
                and len(child.targets) == 1
                and isinstance(child.targets[0], ast.Name)
            ):
                found.append((child, child.targets[0].id, child.value))
            elif (
                isinstance(child, ast.AnnAssign)
                and isinstance(child.target, ast.Name)
                and child.value is not None
            ):
                found.append((child, child.target.id, child.value))
            elif not isinstance(child, _SCOPE_NODES):
                walk(child)

    walk(module)
    return found


class _Walker:
    def __init__(
        self,
        *,
        module_qn: str,
        file: Path,
        project_root: Path,
        system_id: SystemID,
        revision: str | None,
        now: datetime,
    ) -> None:
        self.module_qn = module_qn
        self.file = file
        self.rel_file = file.relative_to(project_root).as_posix()
        self.is_package_init = file.name == "__init__.py"
        self.system_id = system_id
        self.revision = revision
        self.now = now
        self.observations: list[Observation] = []
        self.evidence: list[Evidence] = []

    def run(self, tree: ast.Module) -> None:
        base_ctx = ResolutionContext(
            module_qualified_name=self.module_qn,
            imports=_module_level_imports(
                self.module_qn, tree, is_package_init=self.is_package_init
            ),
            module_locals=_module_level_locals(tree),
        )
        self._emit(
            kind="python.module",
            payload={"qualified_name": self.module_qn, "file": self.rel_file},
            summary=f"module {self.module_qn}",
            line=1,
            col=0,
        )
        for binding_kind, node, is_module_scope in self._all_imports(tree):
            self._emit_import(binding_kind, node, is_module_scope=is_module_scope)
        for stmt, name, value in _module_level_instance_assignments(tree):
            self._emit_module_instance(stmt, name, value, ctx=base_ctx)
        self._walk_body(
            tree.body,
            ctx=base_ctx,
            parent_qualified_name=self.module_qn,
            parent_kind="module",
            caller_qualified_name=self.module_qn,
        )

    def _all_imports(
        self, module: ast.Module
    ) -> list[tuple[str, ast.Import | ast.ImportFrom, bool]]:
        """Every import statement in the file, at any nesting depth, each
        tagged with whether it sits at genuine module scope. Unlike
        `_module_level_imports`/`_function_local_imports` (which scope a name
        *binding* to the scope that can see it), an `IMPORTS` relationship is
        a module-level fact regardless of which scope triggers it — Python
        imports the target module the first time execution reaches the
        statement, function-local or not. The scope tag exists for a
        different, real reason: `PythonIndex` (`adapters/_python_index.py`,
        shared plumbing every framework enricher uses) treats a module's
        `python.import` observations as *its own* flat import namespace —
        without this tag, two unrelated functions in the same file locally
        importing the same name to different targets would silently
        overwrite each other in that shared index, a real regression a
        wider walk here would otherwise introduce."""
        found: list[tuple[str, ast.Import | ast.ImportFrom, bool]] = []

        def walk(node: ast.AST, *, module_scope: bool) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.Import):
                    found.append(("import", child, module_scope))
                elif isinstance(child, ast.ImportFrom):
                    found.append(("import_from", child, module_scope))
                else:
                    walk(child, module_scope=module_scope and not isinstance(child, _SCOPE_NODES))

        walk(module, module_scope=True)
        return found

    def _emit_import(
        self, kind: str, node: ast.Import | ast.ImportFrom, *, is_module_scope: bool
    ) -> None:
        bindings = (
            bindings_for_import(node)
            if isinstance(node, ast.Import)
            else bindings_for_import_from(
                self.module_qn, node, is_package_init=self.is_package_init
            )
        )
        raw = ast.unparse(node)
        for binding in bindings:
            self._emit(
                kind="python.import",
                payload={
                    "bound_name": binding.bound_name,
                    "target": binding.target,
                    "is_module_import": binding.is_module_import,
                    "is_module_scope": is_module_scope,
                    "raw": raw,
                    "importer_qualified_name": self.module_qn,
                },
                summary=f"{self.module_qn} imports {binding.target} as {binding.bound_name}",
                line=node.lineno,
                col=node.col_offset,
            )

    def _emit_module_instance(
        self, stmt: ast.stmt, name: str, value: ast.expr, *, ctx: ResolutionContext
    ) -> None:
        call = _constructor_call(value)
        if call is None:
            return
        resolved = resolve_expr(call.func, ctx)
        if resolved.qualified_name is None or resolved.resolution not in ("IMPORT", "MODULE_LOCAL"):
            return
        self._emit(
            kind="python.module_instance",
            payload={
                "module_qualified_name": self.module_qn,
                "name": name,
                "qualified_name": f"{self.module_qn}.{name}",
                "instance_of": resolved.qualified_name,
            },
            summary=f"{self.module_qn}.{name} = {resolved.text}",
            line=stmt.lineno,
            col=stmt.col_offset,
        )

    def _walk_body(
        self,
        body: list[ast.stmt],
        *,
        ctx: ResolutionContext,
        parent_qualified_name: str,
        parent_kind: str,
        caller_qualified_name: str,
    ) -> None:
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                self._handle_class(
                    stmt,
                    ctx=ctx,
                    parent_qualified_name=parent_qualified_name,
                    parent_kind=parent_kind,
                )
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._handle_function(
                    stmt,
                    ctx=ctx,
                    parent_qualified_name=parent_qualified_name,
                    parent_kind=parent_kind,
                )
            else:
                self._collect_calls(stmt, ctx=ctx, caller_qualified_name=caller_qualified_name)

    def _handle_class(
        self,
        node: ast.ClassDef,
        *,
        ctx: ResolutionContext,
        parent_qualified_name: str,
        parent_kind: str,
    ) -> None:
        qualified_name = f"{parent_qualified_name}.{node.name}"
        bases = [ast.unparse(base) for base in node.bases]
        decorators = [ast.unparse(dec) for dec in node.decorator_list]
        self._emit(
            kind="python.symbol",
            payload={
                "kind": "class",
                "name": node.name,
                "qualified_name": qualified_name,
                "parent_qualified_name": parent_qualified_name,
                "parent_kind": parent_kind,
                "module_qualified_name": self.module_qn,
                "file": self.rel_file,
                "decorators": decorators,
                "bases": bases,
                "line_start": node.lineno,
                "line_end": node.end_lineno or node.lineno,
                "col_start": node.col_offset,
                "col_end": node.end_col_offset or node.col_offset,
            },
            summary=f"class {qualified_name}",
            line=node.lineno,
            col=node.col_offset,
        )
        for base in node.bases:
            resolved = resolve_expr(base, ctx)
            self._emit(
                kind="python.inheritance",
                payload={
                    "class_qualified_name": qualified_name,
                    "module_qualified_name": self.module_qn,
                    "base_expr": resolved.text,
                    "resolution": resolved.resolution,
                    "resolved_qualified_name": resolved.qualified_name,
                },
                summary=f"{qualified_name} extends {resolved.text}",
                line=node.lineno,
                col=node.col_offset,
            )
        class_ctx = dataclasses.replace(ctx, enclosing_class_qualified_name=qualified_name)
        self_types = _self_attribute_types(node, class_ctx)
        class_ctx = dataclasses.replace(class_ctx, self_attribute_types=self_types)
        self._walk_body(
            node.body,
            ctx=class_ctx,
            parent_qualified_name=qualified_name,
            parent_kind="class",
            caller_qualified_name=qualified_name,
        )

    def _handle_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        ctx: ResolutionContext,
        parent_qualified_name: str,
        parent_kind: str,
    ) -> None:
        qualified_name = f"{parent_qualified_name}.{node.name}"
        is_async = isinstance(node, ast.AsyncFunctionDef)
        symbol_kind = (
            "async_function" if is_async else ("method" if parent_kind == "class" else "function")
        )
        decorators = [ast.unparse(dec) for dec in node.decorator_list]
        parameters = [
            *({"kind": "positional_only", "name": arg.arg} for arg in node.args.posonlyargs),
            *({"kind": "positional_or_keyword", "name": arg.arg} for arg in node.args.args),
        ]
        if node.args.vararg is not None:
            parameters.append({"kind": "var_positional", "name": node.args.vararg.arg})
        parameters.extend({"kind": "keyword_only", "name": arg.arg} for arg in node.args.kwonlyargs)
        if node.args.kwarg is not None:
            parameters.append({"kind": "var_keyword", "name": node.args.kwarg.arg})
        payload = {
            "kind": symbol_kind,
            "name": node.name,
            "qualified_name": qualified_name,
            "parent_qualified_name": parent_qualified_name,
            "parent_kind": parent_kind,
            "module_qualified_name": self.module_qn,
            "file": self.rel_file,
            "decorators": decorators,
            "bases": [],
            "line_start": node.lineno,
            "line_end": node.end_lineno or node.lineno,
            "col_start": node.col_offset,
            "col_end": node.end_col_offset or node.col_offset,
        }
        if parent_kind == "class":
            payload["parameters"] = parameters
        self._emit(
            kind="python.symbol",
            payload=payload,
            summary=f"{symbol_kind} {qualified_name}",
            line=node.lineno,
            col=node.col_offset,
        )
        local_imports = function_local_imports(
            node, self.module_qn, is_package_init=self.is_package_init
        )
        ctx = dataclasses.replace(ctx, imports={**ctx.imports, **local_imports})
        param_types = _parameter_instance_types(node, ctx)
        local_types = _local_instance_types(node, ctx)
        func_ctx = dataclasses.replace(ctx, local_instance_types={**param_types, **local_types})
        # `_walk_body` below already collects calls for every non-def statement
        # in `node.body` (via its `else: self._collect_calls(...)` branch) and
        # recurses into nested defs with their own scope — a second explicit
        # call-collection pass here would double-emit every call.
        self._walk_body(
            node.body,
            ctx=func_ctx,
            parent_qualified_name=qualified_name,
            parent_kind="function",
            caller_qualified_name=qualified_name,
        )

    def _collect_calls(
        self, stmt: ast.stmt, *, ctx: ResolutionContext, caller_qualified_name: str
    ) -> None:
        self._collect_calls_in_body([stmt], ctx=ctx, caller_qualified_name=caller_qualified_name)

    def _collect_calls_in_body(
        self, body: list[ast.stmt], *, ctx: ResolutionContext, caller_qualified_name: str
    ) -> None:
        # A hand-rolled walk, not `ast.walk` (which cannot be pruned mid-generator):
        # nested defs must not be descended into here, since `_handle_function`/
        # `_handle_class` walk them separately with their own scope — descending
        # here too would double-emit every call inside a nested def or lambda.
        def walk(node: ast.AST) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.Call):
                    resolved = resolve_expr(child.func, ctx)
                    self._emit(
                        kind="python.call",
                        payload={
                            "caller_qualified_name": caller_qualified_name,
                            "module_qualified_name": self.module_qn,
                            "callee_expr": resolved.text,
                            "resolution": resolved.resolution,
                            "resolved_qualified_name": resolved.qualified_name,
                        },
                        summary=f"{caller_qualified_name} calls {resolved.text}",
                        line=child.lineno,
                        col=child.col_offset,
                    )
                if isinstance(child, _SCOPE_NODES):
                    continue
                walk(child)

        for stmt in body:
            walk(stmt)

    def _emit(
        self, *, kind: str, payload: dict[str, object], summary: str, line: int, col: int
    ) -> None:
        ev = Evidence(
            system_id=self.system_id,
            origin=Origin.PARSER,
            source=SourceRef(provider="python-ast", reference=self.rel_file),
            summary=summary,
            locator=f"{self.rel_file}:{line}:{col}",
            observed_at=self.now,
        )
        self.evidence.append(ev)
        self.observations.append(
            Observation(
                system_id=self.system_id,
                adapter="python@0.1.0",
                origin=Origin.PARSER,
                kind=kind,
                payload=payload,
                evidence_ids=[ev.id],
                revision=self.revision,
                observed_at=self.now,
            )
        )
