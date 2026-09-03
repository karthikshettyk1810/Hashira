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
from .resolve import ResolutionContext, bindings_for_import, bindings_for_import_from, resolve_expr

__all__ = ["ExtractedFile", "extract_file", "module_qualified_name"]


def module_qualified_name(file: Path, import_root: Path) -> str:
    """The dotted module name a file would have on ``sys.path`` rooted at
    ``import_root`` — e.g. ``src/shop/checkout.py`` under ``src/`` becomes
    ``shop.checkout``. Works for namespace packages too: nothing here
    requires an ``__init__.py`` to exist."""
    rel = file.relative_to(import_root)
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
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
    system_id: SystemID,
    revision: str | None,
    now: datetime | None = None,
) -> ExtractedFile:
    """Parse one file and return everything this file, on its own, can say."""
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
        import_root=import_root,
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


def _module_level_imports(module_qn: str, module: ast.Module) -> dict[str, str]:
    """Import bindings visible at module scope, found anywhere outside a
    function/class body (so a module-level `if`/`try` guarding an import
    still counts, matching how these are almost always used in practice)."""
    imports: dict[str, str] = {}

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import):
                for binding in bindings_for_import(child):
                    imports[binding.bound_name] = binding.target
            elif isinstance(child, ast.ImportFrom):
                for binding in bindings_for_import_from(module_qn, child):
                    imports[binding.bound_name] = binding.target
            elif not isinstance(child, _SCOPE_NODES):
                walk(child)

    walk(module)
    return imports


def _local_instance_types(
    func: ast.FunctionDef | ast.AsyncFunctionDef, ctx: ResolutionContext
) -> dict[str, str]:
    """`name = ClassName(...)` assignments anywhere in the function's own
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
        if target is not None and isinstance(value, ast.Call):
            resolved = resolve_expr(value.func, ctx)
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


class _Walker:
    def __init__(
        self,
        *,
        module_qn: str,
        file: Path,
        import_root: Path,
        system_id: SystemID,
        revision: str | None,
        now: datetime,
    ) -> None:
        self.module_qn = module_qn
        self.file = file
        self.rel_file = file.relative_to(import_root).as_posix()
        self.system_id = system_id
        self.revision = revision
        self.now = now
        self.observations: list[Observation] = []
        self.evidence: list[Evidence] = []

    def run(self, tree: ast.Module) -> None:
        base_ctx = ResolutionContext(
            module_qualified_name=self.module_qn,
            imports=_module_level_imports(self.module_qn, tree),
            module_locals=_module_level_locals(tree),
        )
        self._emit(
            kind="python.module",
            payload={"qualified_name": self.module_qn, "file": self.rel_file},
            summary=f"module {self.module_qn}",
            line=1,
            col=0,
        )
        for binding_kind, node in self._top_level_imports(tree):
            self._emit_import(binding_kind, node)
        self._walk_body(
            tree.body,
            ctx=base_ctx,
            parent_qualified_name=self.module_qn,
            parent_kind="module",
            caller_qualified_name=self.module_qn,
        )

    def _top_level_imports(
        self, module: ast.Module
    ) -> list[tuple[str, ast.Import | ast.ImportFrom]]:
        found: list[tuple[str, ast.Import | ast.ImportFrom]] = []

        def walk(node: ast.AST) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.Import):
                    found.append(("import", child))
                elif isinstance(child, ast.ImportFrom):
                    found.append(("import_from", child))
                elif not isinstance(child, _SCOPE_NODES):
                    walk(child)

        walk(module)
        return found

    def _emit_import(self, kind: str, node: ast.Import | ast.ImportFrom) -> None:
        bindings = (
            bindings_for_import(node)
            if isinstance(node, ast.Import)
            else bindings_for_import_from(self.module_qn, node)
        )
        raw = ast.unparse(node)
        for binding in bindings:
            self._emit(
                kind="python.import",
                payload={
                    "bound_name": binding.bound_name,
                    "target": binding.target,
                    "is_module_import": binding.is_module_import,
                    "raw": raw,
                    "importer_qualified_name": self.module_qn,
                },
                summary=f"{self.module_qn} imports {binding.target} as {binding.bound_name}",
                line=node.lineno,
                col=node.col_offset,
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
        self._emit(
            kind="python.symbol",
            payload={
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
            },
            summary=f"{symbol_kind} {qualified_name}",
            line=node.lineno,
            col=node.col_offset,
        )
        local_types = _local_instance_types(node, ctx)
        func_ctx = dataclasses.replace(ctx, local_instance_types=local_types)
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
