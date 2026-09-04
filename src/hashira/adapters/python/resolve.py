"""Best-effort, syntax-only reference resolution for Python source.

This is deliberately *not* type inference. It answers one narrow question:
"given the names visible at this point in the file, what dotted path does this
expression plausibly refer to?" It never consults another file and never
proves anything — Stage 2 (`normalizer.py`) is what checks whether a guessed
path actually names something real, across the whole indexing run.

Spec §10 / this project's identity model both hinge on distinguishing a fact
from a guess. This module produces guesses. `resolution` on every result says
exactly how much to trust one: `SELF`/`SELF_ATTRIBUTE`/`LOCAL_INSTANCE`/
`IMPORT`/`MODULE_LOCAL` are syntactic certainties *about what the code says*,
not proof of what it does — `UNRESOLVED` is the honest default when even that
runs out. Rule of thumb, matching the "uncertainty is data, not failure"
principle this was built to satisfy: never invent a target. If in doubt,
resolve to nothing.

## `SELF_ATTRIBUTE`: constructor-composed dependencies, not arbitrary attribute flow

A real-repository pilot (`docs/ROADMAP.md`'s "Real Repository Pilot v0.1,
Phase 2" entry) found `self._notification_service.send_notifications(...)`
-- `self._notification_service` assigned once in `__init__`, called from a
different method entirely -- completely unresolved, silently, with no
disclosed limitation describing the gap. `SELF` only ever handled a single
attribute hop (`self.method()`); `LOCAL_INSTANCE` only ever tracked a name
assigned *within the same function body*. Neither covers the single most
common way Python composes dependencies: assign a collaborator once in
`__init__`, call methods on it from every other method.

`SELF_ATTRIBUTE` closes exactly that gap, and only that gap.
`ResolutionContext.self_attribute_types` is populated once per class, from
`__init__` alone, by the same restricted mechanism `LOCAL_INSTANCE` already
uses for a local variable: `self.<attr> = KnownCallable(...)` or
`self.<attr>: T = KnownCallable(...)`, where `KnownCallable` itself resolves
to `IMPORT` or `MODULE_LOCAL` -- never a guess layered on a guess. A
same-file `self._mcube = mcube_client or MCubeClient()` fallback-default
idiom (a `BoolOp`, not a bare `Call`) deliberately still resolves to
nothing, exactly like an unrelated `self.foo.bar()` does -- widening this
to arbitrary attribute-flow analysis (assignments outside `__init__`,
non-`Call` right-hand sides, multi-hop attribute provenance) is future
work, not attempted here, for the same reason `RETURN_VALUE_PROVENANCE`
stayed a disclosed limitation rather than a guess when this project last
faced this choice.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

__all__ = [
    "ImportBinding",
    "ResolutionContext",
    "ResolvedExpr",
    "bindings_for_import",
    "bindings_for_import_from",
    "resolve_expr",
]


@dataclass(frozen=True, slots=True)
class ImportBinding:
    """One name an import statement introduces into a module's namespace."""

    bound_name: str
    target: str
    is_module_import: bool
    """True for `import x` / `import x as y`; False for `from x import y`."""


def bindings_for_import(node: ast.Import) -> list[ImportBinding]:
    """`import a.b.c` binds `a` (not `a.b.c`) unless aliased — this matches
    that CPython binding rule rather than the more convenient wrong guess."""
    bindings = []
    for alias in node.names:
        if alias.asname:
            bindings.append(
                ImportBinding(bound_name=alias.asname, target=alias.name, is_module_import=True)
            )
        else:
            top = alias.name.split(".")[0]
            bindings.append(ImportBinding(bound_name=top, target=top, is_module_import=True))
    return bindings


def _package_of(module_qualified_name: str, level: int) -> str:
    """The package a relative import is anchored to (PEP 328).

    ``level=1`` ("from . import x") means the current module's own package;
    each additional level walks one package further up.
    """
    if level <= 0:
        return module_qualified_name
    parts = module_qualified_name.split(".")[:-1]
    for _ in range(level - 1):
        parts = parts[:-1]
    return ".".join(parts)


def bindings_for_import_from(
    module_qualified_name: str, node: ast.ImportFrom
) -> list[ImportBinding]:
    """Resolve a `from ... import ...` statement relative to the importing
    module's own dotted name. Star imports are skipped, not guessed at — a
    wildcard genuinely does not say what it binds without executing it."""
    if node.level:
        package = _package_of(module_qualified_name, node.level)
        base = f"{package}.{node.module}" if node.module else package
    else:
        base = node.module or ""

    bindings = []
    for alias in node.names:
        if alias.name == "*":
            continue
        bound_name = alias.asname or alias.name
        target = f"{base}.{alias.name}" if base else alias.name
        bindings.append(ImportBinding(bound_name=bound_name, target=target, is_module_import=False))
    return bindings


@dataclass
class ResolutionContext:
    """Everything `resolve_expr` is allowed to look at: purely local, purely
    syntactic. No storage, no other files, no imports of imports."""

    module_qualified_name: str
    imports: dict[str, str] = field(default_factory=dict)
    module_locals: set[str] = field(default_factory=set)
    enclosing_class_qualified_name: str | None = None
    local_instance_types: dict[str, str] = field(default_factory=dict)
    self_attribute_types: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ResolvedExpr:
    text: str
    resolution: str
    """One of SELF, SELF_ATTRIBUTE, LOCAL_INSTANCE, IMPORT, MODULE_LOCAL,
    UNRESOLVED."""
    qualified_name: str | None


def _split_leftmost(expr: ast.expr) -> tuple[str | None, list[str]]:
    """`a.b.c` -> ("a", ["b", "c"]); anything not a dotted Name chain -> (None, [])."""
    suffix: list[str] = []
    node: ast.expr = expr
    while isinstance(node, ast.Attribute):
        suffix.insert(0, node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return node.id, suffix
    return None, []


def resolve_expr(expr: ast.expr, ctx: ResolutionContext) -> ResolvedExpr:
    """Best-effort resolution of a call target, base class, or decorator
    expression, in priority order: an already-known local instance's type,
    `self`/`cls` attribute access, an import binding, then a module-level
    definition. First match wins; nothing beyond that is guessed."""
    text = ast.unparse(expr)
    leftmost, suffix = _split_leftmost(expr)
    if leftmost is None:
        return ResolvedExpr(text=text, resolution="UNRESOLVED", qualified_name=None)

    def _qualify(base: str) -> str:
        return base if not suffix else f"{base}.{'.'.join(suffix)}"

    if leftmost in ctx.local_instance_types:
        return ResolvedExpr(
            text=text,
            resolution="LOCAL_INSTANCE",
            qualified_name=_qualify(ctx.local_instance_types[leftmost]),
        )
    if leftmost in ("self", "cls") and ctx.enclosing_class_qualified_name:
        if len(suffix) == 1:
            return ResolvedExpr(
                text=text,
                resolution="SELF",
                qualified_name=f"{ctx.enclosing_class_qualified_name}.{suffix[0]}",
            )
        if len(suffix) >= 2 and suffix[0] in ctx.self_attribute_types:
            return ResolvedExpr(
                text=text,
                resolution="SELF_ATTRIBUTE",
                qualified_name=f"{ctx.self_attribute_types[suffix[0]]}.{'.'.join(suffix[1:])}",
            )
    if leftmost in ctx.imports:
        return ResolvedExpr(
            text=text, resolution="IMPORT", qualified_name=_qualify(ctx.imports[leftmost])
        )
    if leftmost in ctx.module_locals:
        return ResolvedExpr(
            text=text,
            resolution="MODULE_LOCAL",
            qualified_name=_qualify(f"{ctx.module_qualified_name}.{leftmost}"),
        )
    return ResolvedExpr(text=text, resolution="UNRESOLVED", qualified_name=None)
