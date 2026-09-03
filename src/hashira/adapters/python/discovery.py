"""Which files this adapter should look at, and what module each one is.

Kept deliberately simple: v0.1 recognizes exactly one src-layout convention
(a top-level ``src/`` directory) rather than a general multi-root resolver —
see `module_qualified_name` in `extractor.py` for why getting this wrong
silently produces wrong dotted names rather than an error.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

__all__ = ["DEFAULT_EXCLUDED_DIRS", "discover_python_files", "import_root_for"]

#: Directories never worth indexing: virtual envs, VCS metadata, caches,
#: build output. A repository is not obligated to avoid these names, so this
#: is a heuristic, not a guarantee — an explicit exclude list belongs to the
#: application layer once one exists.
DEFAULT_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".git",
        ".hg",
        ".svn",
        "build",
        "dist",
        "node_modules",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".eggs",
    }
)


def _is_excluded(relative_dir_parts: tuple[str, ...], excluded_dirs: frozenset[str]) -> bool:
    return any(
        part in excluded_dirs
        or part.endswith(".egg-info")
        or (part.startswith(".") and part not in (".", ""))
        for part in relative_dir_parts
    )


def discover_python_files(
    project_root: Path, *, excluded_dirs: frozenset[str] = DEFAULT_EXCLUDED_DIRS
) -> Iterator[Path]:
    """Every ``*.py`` file under ``project_root``, skipping junk directories.

    Order is deterministic (lexicographic) so two runs over the same tree
    produce observations in the same order — useful for reviewing a diff of
    what changed, and required for the golden-fixture style of testing this
    project already uses for the IR schema.
    """
    for path in sorted(project_root.rglob("*.py")):
        rel_parts = path.relative_to(project_root).parts[:-1]
        if _is_excluded(rel_parts, excluded_dirs):
            continue
        yield path


def import_root_for(file: Path, project_root: Path) -> Path:
    """The directory a file's dotted module name should be computed relative
    to: the nearest ``src/`` directly under ``project_root`` on the file's own
    path, else ``project_root`` itself.

    This means ``<root>/src/shop/checkout.py`` resolves to module
    ``shop.checkout`` (the src-layout convention: ``src/`` sits on
    ``sys.path``, not the repository root), while ``<root>/tests/test_x.py``
    — outside ``src/`` — resolves to ``tests.test_x`` relative to the project
    root, matching how it would actually import.
    """
    src_root = project_root / "src"
    try:
        file.relative_to(src_root)
    except ValueError:
        return project_root
    return src_root
