"""`hashira.cli.main`: the `hashira mcp --db ... --system ...` entry point
that makes `[project.scripts] hashira` in `pyproject.toml` actually
runnable, wired to `hashira.mcp.build_server`. Never calls
`MCPServer.run` for real here (it would block on stdio) -- these tests stop
at "the right server got built for the right system" and "an unknown
system fails cleanly", exercising `_run_mcp` itself rather than the
process boundary."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from hashira.cli.main import _run_mcp, build_parser, main
from hashira.core import System
from hashira.storage.sqlite import SqliteDatabase


def _args(db_path: Path, system_slug: str) -> argparse.Namespace:
    return build_parser().parse_args(["mcp", "--db", str(db_path), "--system", system_slug])


def test_build_parser_parses_mcp_subcommand(tmp_path: Path) -> None:
    args = _args(tmp_path / "hashira.db", "checkout")
    assert args.command == "mcp"
    assert args.db == str(tmp_path / "hashira.db")
    assert args.system == "checkout"
    assert args.func is _run_mcp


def test_run_mcp_returns_error_for_unknown_system(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "hashira.db"
    SqliteDatabase(str(db_path))  # creates the schema, no systems saved

    exit_code = _run_mcp(_args(db_path, "nonexistent"))

    assert exit_code == 1
    assert "nonexistent" in capsys.readouterr().err


def test_run_mcp_builds_and_runs_the_server_for_a_known_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "hashira.db"
    db = SqliteDatabase(str(db_path))
    with db.unit_of_work() as uow:
        uow.systems.save(System(name="Checkout", slug="checkout"))
        uow.commit()

    captured: dict[str, object] = {}

    def fake_run(self: MCPServer, transport: str = "stdio") -> None:
        captured["transport"] = transport
        captured["server"] = self

    monkeypatch.setattr(MCPServer, "run", fake_run)

    exit_code = _run_mcp(_args(db_path, "checkout"))

    assert exit_code == 0
    assert captured["transport"] == "stdio"
    assert isinstance(captured["server"], MCPServer)


def test_main_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit):
        main([])


def test_run_mcp_reports_a_clean_error_when_the_mcp_extra_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`pip install hashira` (no extra) leaves the `mcp` package absent --
    `hashira mcp` must say so and point at the fix, not surface a raw
    `ModuleNotFoundError` traceback as a new user's first real command."""
    db_path = tmp_path / "hashira.db"
    db = SqliteDatabase(str(db_path))
    with db.unit_of_work() as uow:
        uow.systems.save(System(name="Checkout", slug="checkout"))
        uow.commit()

    # Force the deferred `from ..mcp import build_server` to raise
    # ModuleNotFoundError("mcp") exactly as it would with the extra absent,
    # without needing a second, mcp-less test environment.
    for name in [n for n in sys.modules if n == "mcp" or n.startswith("mcp.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    for name in [n for n in sys.modules if n == "hashira.mcp" or n.startswith("hashira.mcp.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "mcp", None)  # type: ignore[call-overload]

    exit_code = _run_mcp(_args(db_path, "checkout"))

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "mcp" in err
    assert 'pip install "hashira[mcp]"' in err
