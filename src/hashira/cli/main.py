"""`hashira mcp`: run the MCP read surface (`hashira.mcp.build_server`)
over stdio against one already-indexed system in a SQLite database.

Deliberately the only subcommand today -- `hashira index`/`hashira serve`
and any richer CLI surface are a later phase's concern (see
`hashira/cli/__init__.py`'s own placeholder docstring); this exists so the
`[project.scripts] hashira = "hashira.cli.main:main"` entry point already
declared in `pyproject.toml` is not dead, and so MCP clients that expect to
launch a server via a command (Claude Desktop's config, for instance) have
one to point at.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from typing import cast

from ..ports.repositories import UnitOfWork
from ..storage.sqlite import SqliteDatabase


def _run_mcp(args: argparse.Namespace) -> int:
    db = SqliteDatabase(args.db)
    with db.unit_of_work() as uow:
        system = uow.systems.get_by_slug(args.system)
    if system is None:
        print(f"error: no system with slug {args.system!r} in {args.db}", file=sys.stderr)
        return 1

    try:
        from ..mcp import build_server
    except ModuleNotFoundError as exc:
        # `exc.name` is the first missing component of whatever dotted
        # import failed -- "mcp" for a bare `import mcp`, but "mcp.server"
        # (or deeper) when the top-level package resolves yet a submodule
        # doesn't (e.g. an incompatible partial install). Either shape means
        # the same thing to a user: the optional `mcp` dependency isn't
        # usable, so both are treated as the same, actionable error rather
        # than a raw traceback on a new user's first real command.
        if exc.name != "mcp" and not (exc.name or "").startswith("mcp."):
            raise
        print(
            "error: the 'mcp' package is required to run this command.\n"
            '       install it with: pip install "hashira[mcp]"',
            file=sys.stderr,
        )
        return 1

    # `SqliteDatabase.unit_of_work` returns the concrete `SqliteUnitOfWork`,
    # not the `UnitOfWork` protocol itself -- mypy's strict (invariant)
    # matching of a Protocol's mutable attributes means no concrete backend
    # is a *structural* subtype of `UnitOfWork`, even though every method
    # this module and `build_server` ever call on it is satisfied. This is
    # the first `src/hashira` call site to actually bind a concrete
    # backend's `unit_of_work` to a `Callable[[], UnitOfWork]`-typed
    # parameter (every other caller lives in `tests/`, outside mypy's
    # `files` scope) -- the cast documents that gap rather than papering
    # over a real behavioral mismatch.
    uow_factory = cast("Callable[[], UnitOfWork]", db.unit_of_work)
    server = build_server(uow_factory, system_id=system.id, name=f"hashira:{args.system}")
    server.run(transport="stdio")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hashira")
    subparsers = parser.add_subparsers(dest="command", required=True)

    mcp_parser = subparsers.add_parser("mcp", help="run the MCP read surface over stdio")
    mcp_parser.add_argument("--db", required=True, help="path to the SQLite database file")
    mcp_parser.add_argument("--system", required=True, help="system slug to expose")
    mcp_parser.set_defaults(func=_run_mcp)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
