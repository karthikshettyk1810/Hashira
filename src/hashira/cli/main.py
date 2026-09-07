"""`hashira index`/`hashira mcp`: the two-command CLI a colleague who has
never seen this source tree actually needs -- build a graph from a real
project, then serve it over MCP.

Both are thin wiring, on purpose (see each function's own docstring for
why): every line of actual indexing logic lives in `application.indexing`
and the adapters it composes, and every line of actual query logic lives
in `hashira.mcp`. This module's whole job is argument parsing, assembling
the concrete objects those layers need, and turning their results into
either a clean stdout report or an actionable stderr message -- never a
raw traceback as a new user's first real command.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

from ..core import System
from ..ports.adapters import HistoryAdapter
from ..ports.repositories import UnitOfWork
from ..storage.sqlite import SqliteDatabase


def _slugify(name: str) -> str:
    """A stable, deterministic system slug from a directory name -- the
    same input always produces the same output, which is what makes
    re-indexing the same project idempotent (`_run_index` looks an
    existing system up by this slug before minting a new one)."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug


def _default_db_path(project_root: Path) -> Path:
    return project_root / ".hashira" / "hashira.db"


def _run_index(args: argparse.Namespace) -> int:
    """Index one project root into a Hashira database -- the composition
    root for `IndexingService` and every adapter this package ships, not a
    reimplementation of anything `application.indexing`/the adapters
    already do. Every adapter is always offered the chance to look; each
    one already only produces entities for constructs it actually
    recognizes (a plain-Python project simply gets nothing from
    `DjangoAdapter`/`FastAPIAdapter`/`SQLAlchemyAdapter`, at negligible
    cost), so there is no separate "which frameworks does this project
    use" selection system to build or keep in sync."""
    project_root = Path(args.project_root).resolve()
    if not project_root.exists():
        print(f"error: {project_root} does not exist", file=sys.stderr)
        return 1
    if not project_root.is_dir():
        print(f"error: {project_root} is not a directory", file=sys.stderr)
        return 1

    db_path = Path(args.db).resolve() if args.db else _default_db_path(project_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    slug = args.system or _slugify(project_root.name)
    if not slug:
        print(
            f"error: could not derive a system slug from {project_root.name!r} "
            "-- pass --system explicitly",
            file=sys.stderr,
        )
        return 1

    from ..adapters.git.repository import GitRepository, is_git_repository
    from ..adapters.git.runner import GitCommandError, run_git

    revision: str | None = None
    history_adapters: list[HistoryAdapter] = []
    if is_git_repository(project_root):
        from ..adapters.git import GitAdapter

        try:
            revision = GitRepository(project_root).current_revision()
        except GitCommandError:
            print(
                "note: a Git repository was found but has no commits yet -- "
                "indexing without revision history.",
                file=sys.stderr,
            )
        else:
            history_adapters.append(GitAdapter())
            try:
                toplevel = Path(
                    run_git(["rev-parse", "--show-toplevel"], cwd=project_root).strip()
                ).resolve()
            except GitCommandError:
                toplevel = None
            if toplevel is not None and toplevel != project_root:
                print(
                    f"note: the Git repository root ({toplevel}) differs from the "
                    f"indexed root ({project_root}). If {project_root} is not this "
                    "project's actual Python import root, cross-file resolution "
                    "(imports, calls, field reads/writes) may be incomplete -- see "
                    "docs/ADAPTERS.md's 'analysis root vs. repository root' entry.",
                    file=sys.stderr,
                )
    else:
        print(
            "note: no Git repository detected at the indexed root -- "
            "indexing without revision history.",
            file=sys.stderr,
        )

    db = SqliteDatabase(str(db_path))
    with db.unit_of_work() as uow:
        system = uow.systems.get_by_slug(slug)
        if system is None:
            system = System(name=project_root.name, slug=slug)
            uow.systems.save(system)
            uow.commit()

    from ..adapters._compose import compose_normalizers
    from ..adapters.django import DjangoAdapter
    from ..adapters.fastapi import FastAPIAdapter
    from ..adapters.fastapi.normalizer import enrich_normalized_run as enrich_fastapi
    from ..adapters.python import PythonAdapter
    from ..adapters.sqlalchemy import SQLAlchemyAdapter
    from ..adapters.sqlalchemy.normalizer import enrich_normalized_run as enrich_sqlalchemy
    from ..application import IndexingService
    from ..application.indexing import Normalizer

    # Two of the same "first src/hashira-scoped call site to bind a
    # structurally-compatible-but-nominally-distinct concrete type"
    # situation `_run_mcp`'s own `uow_factory` cast already documents:
    # `SqliteDatabase.unit_of_work` returns the concrete `SqliteUnitOfWork`,
    # not the `UnitOfWork` Protocol, and `compose_normalizers`'s
    # `ComposedNormalizer` returns `adapters.python.normalizer.NormalizedRun`,
    # not `application.indexing`'s own structurally-identical `_NormalizedRun`
    # (duplicated there on purpose, per §18, so `application` never imports
    # upward into `adapters`). Both casts document a real behavioral match
    # mypy's strict, invariant Protocol matching can't see -- not a
    # workaround for an actual mismatch.
    uow_factory = cast("Callable[[], UnitOfWork]", db.unit_of_work)
    normalize = cast("Normalizer", compose_normalizers(enrich_fastapi, enrich_sqlalchemy))

    service = IndexingService(
        uow_factory,
        [PythonAdapter()],
        normalize,
        history_adapters=history_adapters,
        framework_adapters=[DjangoAdapter(), FastAPIAdapter()],
        data_adapters=[SQLAlchemyAdapter()],
    )

    print(f"Indexing {project_root} as system {slug!r} (revision: {revision or 'none'})...")
    result = service.index(project_root, system_id=system.id, revision=revision)

    print(f"  files processed:         {result.files_processed}")
    print(f"  entities upserted:       {result.entities_upserted}")
    print(f"  relationships upserted:  {result.relationships_upserted}")
    if result.ambiguous_count:
        print(f"  ambiguous identities:    {result.ambiguous_count}")
    if result.unresolved_observation_count:
        print(f"  unresolved observations: {result.unresolved_observation_count}")

    if result.errors:
        print(f"  errors: {len(result.errors)}", file=sys.stderr)
        for error in result.errors:
            print(f"    - {error}", file=sys.stderr)

    print(f"\nDatabase: {db_path}")
    print(f"Next:     hashira mcp --db {db_path} --system {slug}")

    return 1 if result.errors else 0


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

    index_parser = subparsers.add_parser("index", help="index a project into a Hashira database")
    index_parser.add_argument(
        "project_root",
        nargs="?",
        default=".",
        help="path to the project to index (default: current directory)",
    )
    index_parser.add_argument(
        "--db",
        help="path to the SQLite database file (default: <project_root>/.hashira/hashira.db)",
    )
    index_parser.add_argument(
        "--system", help="system slug (default: derived from the project directory name)"
    )
    index_parser.set_defaults(func=_run_index)

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
