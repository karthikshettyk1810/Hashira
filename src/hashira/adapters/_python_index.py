"""A shared index over Python's own observations, and a small parsed-tree
cache -- the plumbing every framework enricher needs before it can do its
own targeted parsing, factored out once a second one (`adapters.fastapi`)
needed exactly the same thing `adapters.django` already built.

Not a public API (the leading underscore is deliberate, matching `_dedup.py`
and `_identity_claims.py`) -- this is infrastructure for consuming
`python.*` observations, not a framework concept. Nothing here is Django- or
FastAPI-specific: it is built entirely from `Observation.kind` values the
Python adapter itself defines (`adapters/python/extractor.py`), and reused
by both enrichers, which each still do their own, independent, targeted
parsing for whatever their framework actually needs.
"""

from __future__ import annotations

import ast
from pathlib import Path

from ..ports.adapters import ExtractionResult
from .python.resolve import ResolutionContext

__all__ = ["PythonIndex", "PythonTreeCache", "find_class_node"]


class PythonIndex:
    """Everything a framework enricher needs from Python's own observations,
    indexed once. Built entirely from `base.observations` -- no re-parsing
    here; that is each enricher's own, separate job."""

    def __init__(self) -> None:
        self.imports: dict[str, dict[str, str]] = {}
        self.module_locals: dict[str, set[str]] = {}
        self.inheritance: dict[str, list[str]] = {}
        self.symbol_file: dict[str, str] = {}
        self.module_file: dict[str, str] = {}
        self.class_module: dict[str, str] = {}

    @classmethod
    def build(cls, base: ExtractionResult) -> PythonIndex:
        index = cls()
        for obs in base.observations:
            if obs.kind == "python.import":
                module_qn = str(obs.payload["importer_qualified_name"])
                index.imports.setdefault(module_qn, {})[str(obs.payload["bound_name"])] = str(
                    obs.payload["target"]
                )
            elif obs.kind == "python.module":
                qn = str(obs.payload["qualified_name"])
                index.module_file[qn] = str(obs.payload["file"])
            elif obs.kind == "python.symbol":
                qn = str(obs.payload["qualified_name"])
                index.symbol_file[qn] = str(obs.payload["file"])
                if obs.payload["kind"] == "class":
                    index.module_locals.setdefault(
                        str(obs.payload["module_qualified_name"]), set()
                    ).add(str(obs.payload["name"]))
                    index.class_module[qn] = str(obs.payload["module_qualified_name"])
                elif obs.payload["parent_kind"] == "module":
                    index.module_locals.setdefault(
                        str(obs.payload["module_qualified_name"]), set()
                    ).add(str(obs.payload["name"]))
            elif obs.kind == "python.inheritance":
                resolved = obs.payload.get("resolved_qualified_name")
                if resolved:
                    index.inheritance.setdefault(
                        str(obs.payload["class_qualified_name"]), []
                    ).append(str(resolved))
        return index

    def context_for(self, module_qn: str) -> ResolutionContext:
        return ResolutionContext(
            module_qualified_name=module_qn,
            imports=self.imports.get(module_qn, {}),
            module_locals=self.module_locals.get(module_qn, set()),
        )


class PythonTreeCache:
    """Parses each file at most once per `enrich()` call, shared across
    however many targeted passes a framework enricher needs to make over
    the same source tree."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._cache: dict[str, ast.Module | None] = {}

    def get(self, file_rel: str) -> ast.Module | None:
        if file_rel not in self._cache:
            try:
                self._cache[file_rel] = ast.parse(
                    (self._root / file_rel).read_text(encoding="utf-8")
                )
            except (SyntaxError, UnicodeDecodeError, OSError):
                self._cache[file_rel] = None
        return self._cache[file_rel]


def find_class_node(tree: ast.Module, simple_name: str) -> ast.ClassDef | None:
    """The first class definition in `tree` with this simple (unqualified)
    name -- used once a framework enricher already knows a class's
    qualified name (from `PythonIndex`) and needs the AST node itself to
    parse the class body for something Python's own extractor does not
    track (Django's model fields, SQLAlchemy's columns, ...)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == simple_name:
            return node
    return None
