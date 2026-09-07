"""`PythonIndex` (`adapters/_python_index.py`): shared plumbing every
framework enricher (Django/FastAPI/SQLAlchemy) builds its own resolution
context from. Small on purpose, but a real regression lived here: once the
Python adapter started emitting a `python.import` observation for a
function-local import too (R1, `docs/ROADMAP.md`), this index's own
`imports` dict -- which represents *a module's own* import namespace, a
contract `context_for` and every current consumer already assume -- began
silently conflating two unrelated functions' same-named local imports.
"""

from __future__ import annotations

from pathlib import Path

from hashira.adapters._python_index import PythonIndex
from hashira.adapters.python import PythonAdapter
from hashira.adapters.python.discovery import discover_python_files
from hashira.core.ids import IDPrefix, new_id


def _index_for(tmp_path: Path, source: str) -> PythonIndex:
    (tmp_path / "app.py").write_text(source)
    system_id = new_id(IDPrefix.SYSTEM)
    files = list(discover_python_files(tmp_path))
    base = PythonAdapter().extract(tmp_path, files, system_id=system_id, revision="rev1")
    return PythonIndex.build(base)


def test_module_level_import_is_indexed(tmp_path: Path) -> None:
    index = _index_for(tmp_path, "from lib import Thing\n")
    assert index.imports["app"] == {"Thing": "lib.Thing"}


def test_function_local_imports_do_not_leak_into_the_module_index(tmp_path: Path) -> None:
    """The regression: `func_a` and `func_b` each locally import a
    different `Thing` -- neither should appear in `app`'s own module-scope
    import namespace at all, and critically, neither should silently
    overwrite the other."""
    source = (
        "def func_a():\n"
        "    from module_x import Thing\n"
        "    return Thing()\n\n\n"
        "def func_b():\n"
        "    from module_y import Thing\n"
        "    return Thing()\n"
    )
    index = _index_for(tmp_path, source)
    assert index.imports.get("app", {}) == {}


def test_module_level_import_survives_alongside_unrelated_function_local_ones(
    tmp_path: Path,
) -> None:
    """A genuine module-level import must still be indexed correctly even
    when the same file also has function-local imports of a same-named
    binding -- the filter must remove only the function-local rows, not
    the whole module's import set."""
    source = (
        "from module_z import Thing as ModuleLevelThing\n\n\n"
        "def func_a():\n"
        "    from module_x import Thing\n"
        "    return Thing()\n"
    )
    index = _index_for(tmp_path, source)
    assert index.imports["app"] == {"ModuleLevelThing": "module_z.Thing"}
