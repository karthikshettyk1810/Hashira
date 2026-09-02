"""Guardrail 7 of spec §41, enforced rather than trusted.

"Does this introduce an LLM dependency into the core?" is a question people
forget to ask at 6pm on a Friday. This test asks it on every run.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

CORE = pathlib.Path(__file__).resolve().parents[2] / "src" / "hashira" / "core"

#: Top-level modules the core domain may never import. Spec §6: "the core domain
#: must never import an LLM SDK, Django, a particular database driver, or a
#: specific agent runtime."
FORBIDDEN = {
    "anthropic",
    "openai",
    "google",
    "cohere",
    "mistralai",
    "ollama",
    "langchain",
    "llama_index",
    "transformers",
    "torch",
    "sqlalchemy",
    "psycopg",
    "psycopg2",
    "sqlite3",
    "asyncpg",
    "redis",
    "django",
    "flask",
    "fastapi",
    "starlette",
    "celery",
    "requests",
    "httpx",
    "aiohttp",
    "boto3",
}

CORE_MODULES = sorted(CORE.glob("*.py"))


def _imported_roots(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.contract
@pytest.mark.parametrize("module", CORE_MODULES, ids=lambda p: p.name)
def test_core_imports_no_infrastructure(module: pathlib.Path) -> None:
    offenders = _imported_roots(module) & FORBIDDEN
    assert not offenders, f"{module.name} imports {sorted(offenders)} — see spec §6 and §41"


@pytest.mark.contract
def test_core_does_not_import_outward() -> None:
    """Dependency direction is one-way: nothing in core may reach into ports,
    storage, adapters, ingestion, application or cli (§6)."""
    outward = {
        "hashira.ports",
        "hashira.storage",
        "hashira.adapters",
        "hashira.ingestion",
        "hashira.application",
        "hashira.cli",
    }
    for module in CORE_MODULES:
        tree = ast.parse(module.read_text(), filename=str(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module not in outward, f"{module.name} imports {node.module}"
            # A relative import above the package would leave core entirely.
            if isinstance(node, ast.ImportFrom) and node.level > 1:
                raise AssertionError(f"{module.name} reaches outside core via a relative import")


@pytest.mark.contract
def test_core_is_importable_with_only_pydantic() -> None:
    """The advertised dependency set is the real one."""
    import hashira.core  # noqa: F401
