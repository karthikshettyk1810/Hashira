"""File discovery: find real source, skip junk, respect src-layout."""

from __future__ import annotations

from pathlib import Path

from hashira.adapters.python.discovery import discover_python_files, import_root_for


def _touch(root: Path, relpath: str) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


def test_discovers_python_files_recursively(tmp_path: Path) -> None:
    _touch(tmp_path, "src/shop/checkout.py")
    _touch(tmp_path, "src/shop/payments.py")
    _touch(tmp_path, "tests/test_checkout.py")
    found = {p.relative_to(tmp_path).as_posix() for p in discover_python_files(tmp_path)}
    assert found == {"src/shop/checkout.py", "src/shop/payments.py", "tests/test_checkout.py"}


def test_ignores_venvs_and_caches(tmp_path: Path) -> None:
    _touch(tmp_path, "src/shop/checkout.py")
    _touch(tmp_path, ".venv/lib/site-packages/whatever.py")
    _touch(tmp_path, "src/shop/__pycache__/checkout.cpython-312.pyc.py")  # pretend
    _touch(tmp_path, "build/lib/shop/checkout.py")
    found = {p.relative_to(tmp_path).as_posix() for p in discover_python_files(tmp_path)}
    assert found == {"src/shop/checkout.py"}


def test_ignores_hidden_directories(tmp_path: Path) -> None:
    _touch(tmp_path, "src/shop/checkout.py")
    _touch(tmp_path, ".git/hooks/pre-commit.py")
    found = {p.relative_to(tmp_path).as_posix() for p in discover_python_files(tmp_path)}
    assert found == {"src/shop/checkout.py"}


def test_ignores_egg_info(tmp_path: Path) -> None:
    _touch(tmp_path, "src/shop/checkout.py")
    _touch(tmp_path, "hashira.egg-info/PKG-INFO.py")  # contrived, proves the suffix check
    found = {p.relative_to(tmp_path).as_posix() for p in discover_python_files(tmp_path)}
    assert found == {"src/shop/checkout.py"}


def test_discovery_order_is_deterministic(tmp_path: Path) -> None:
    _touch(tmp_path, "src/z_module.py")
    _touch(tmp_path, "src/a_module.py")
    found = list(discover_python_files(tmp_path))
    assert found == sorted(found)


def test_import_root_is_src_when_file_lives_under_it(tmp_path: Path) -> None:
    file = _touch(tmp_path, "src/shop/checkout.py")
    assert import_root_for(file, tmp_path) == tmp_path / "src"


def test_import_root_is_project_root_when_no_src_layout(tmp_path: Path) -> None:
    file = _touch(tmp_path, "tests/test_checkout.py")
    assert import_root_for(file, tmp_path) == tmp_path


def test_import_root_is_project_root_for_a_flat_layout(tmp_path: Path) -> None:
    file = _touch(tmp_path, "shop/checkout.py")
    assert import_root_for(file, tmp_path) == tmp_path
