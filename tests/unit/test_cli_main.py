"""`hashira.cli.main`: the two-command CLI (`hashira index`, `hashira mcp`)
that makes `[project.scripts] hashira` in `pyproject.toml` actually
runnable without writing a custom Python script, wired to
`application.indexing.IndexingService` and `hashira.mcp.build_server`
respectively. `hashira mcp`'s tests never call `MCPServer.run` for real
(it would block on stdio) -- they stop at "the right server got built for
the right system" and "an unknown system fails cleanly", exercising
`_run_mcp` itself rather than the process boundary. `hashira index`'s
tests run the real `IndexingService` against a real, temporary Git
repository -- there is nothing left to fake once the CLI layer is this
thin."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.server.mcpserver import MCPServer

from hashira.application.graph import get_relationships
from hashira.cli.main import _run_index, _run_mcp, _slugify, build_parser, main
from hashira.core import System
from hashira.core.enums import RelationshipType
from hashira.storage.sqlite import SqliteDatabase


def _args(db_path: Path, system_slug: str) -> argparse.Namespace:
    return build_parser().parse_args(["mcp", "--db", str(db_path), "--system", system_slug])


def _index_args(argv: list[str]) -> argparse.Namespace:
    return build_parser().parse_args(["index", *argv])


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A real, temporary Git repository with one plain-Python module --
    deliberately outside any Hashira source path, exactly like a real
    colleague's project would be."""
    root = tmp_path / "myproject"
    root.mkdir()
    (root / "app").mkdir()
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "payments.py").write_text(
        "class PaymentService:\n    def charge(self, amount):\n        return amount > 0\n"
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial commit")
    return root


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


# --- hashira index --------------------------------------------------------


def test_build_parser_parses_index_subcommand(project: Path) -> None:
    args = _index_args([str(project), "--db", "custom.db", "--system", "myslug"])
    assert args.command == "index"
    assert args.project_root == str(project)
    assert args.db == "custom.db"
    assert args.system == "myslug"
    assert args.func is _run_index


def test_build_parser_index_defaults_to_current_directory() -> None:
    args = _index_args([])
    assert args.project_root == "."
    assert args.db is None
    assert args.system is None


def test_slugify_is_deterministic_and_url_safe() -> None:
    assert _slugify("My Project") == "my-project"
    assert _slugify("My_Project.v2") == "my-project-v2"
    assert _slugify("already-a-slug") == "already-a-slug"


def test_run_index_indexes_a_real_project_into_the_default_db(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = _run_index(_index_args([str(project)]))

    assert exit_code == 0
    db_path = project / ".hashira" / "hashira.db"
    assert db_path.is_file()

    out = capsys.readouterr().out
    assert "entities upserted:" in out
    assert str(db_path) in out

    db = SqliteDatabase(str(db_path))
    with db.unit_of_work() as uow:
        system = uow.systems.get_by_slug("myproject")
        assert system is not None
        entities = uow.graph.find_entities(system.id, limit=100)
        assert any(e.qualified_name == "app.payments.PaymentService" for e in entities)


@pytest.fixture
def django_project(tmp_path: Path) -> Path:
    """A minimal, real Django-shaped project (`models.Model`, a service
    reading/writing a model field) -- catches a real regression: `hashira
    index`'s own normalizer composition silently dropped every
    Django-specific entity and relationship even though `DjangoAdapter` was
    correctly wired into extraction; see `_run_index`'s own
    `compose_normalizers` call."""
    root = tmp_path / "djproject"
    (root / "payments").mkdir(parents=True)
    (root / "payments" / "__init__.py").write_text("")
    (root / "payments" / "models.py").write_text(
        "from django.db import models\n\n\n"
        "class Payment(models.Model):\n"
        "    status = models.CharField(max_length=20)\n"
    )
    (root / "payments" / "services.py").write_text(
        "from .models import Payment\n\n\n"
        "def process():\n"
        "    payment = Payment()\n"
        '    payment.status = "captured"\n'
        "    return payment.status\n"
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial commit")
    return root


def test_run_index_produces_django_specific_entities_and_relationships(
    django_project: Path,
) -> None:
    """The regression: `DjangoAdapter` was wired into `framework_adapters`
    (so its own `enrich()` extraction ran, producing real `django.*`
    observations) but never into `compose_normalizers` -- so every one of
    those observations was silently discarded before becoming a real
    `Entity`/`Relationship`. A model tagged `django_kind` and a real
    `WRITES` edge are the minimal, real signal that Django's own normalizer
    actually ran as part of `hashira index`, not just as part of the
    lower-level `IndexingService` API other tests already exercise
    directly."""
    exit_code = _run_index(_index_args([str(django_project)]))
    assert exit_code == 0

    db_path = django_project / ".hashira" / "hashira.db"
    db = SqliteDatabase(str(db_path))
    with db.unit_of_work() as uow:
        system = uow.systems.get_by_slug("djproject")
        assert system is not None
        entities = {e.qualified_name: e for e in uow.graph.find_entities(system.id, limit=100)}

        model = entities["payments.models.Payment"]
        assert model.metadata.get("django_kind") == "model"

        process = entities["payments.services.process"]
        writes = get_relationships(
            uow,
            system_id=system.id,
            entity_id=process.id,
            direction="out",
            types=[RelationshipType.WRITES],
        )
        assert len(writes) == 1


def test_run_index_produces_fastapi_and_sqlalchemy_entities_and_relationships(
    tmp_path: Path,
) -> None:
    """The seam audit's own instruction: one test exercising the actual
    shipped `hashira index` composition for the two remaining
    observation-producing adapters the Django regression's own fix did not
    touch. Reuses the real, already-load-bearing `fastapi_checkout` fixture
    (`tests/integration/test_fastapi_sqlalchemy_together.py`'s own fixture)
    rather than a fresh inline one, run through `_run_index` itself -- if a
    future change ever drops `enrich_fastapi`/`enrich_sqlalchemy` from
    `compose_normalizers` the way `enrich_django` was dropped, this fails
    the same way the Django regression test now would."""
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "fastapi_checkout"
    project = tmp_path / "checkout"
    shutil.copytree(fixture, project)
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "test@example.com")
    _git(project, "config", "user.name", "Test")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "initial commit")

    exit_code = _run_index(_index_args([str(project)]))
    assert exit_code == 0

    db_path = project / ".hashira" / "hashira.db"
    db = SqliteDatabase(str(db_path))
    with db.unit_of_work() as uow:
        system = uow.systems.get_by_slug("checkout")
        assert system is not None
        entities = {e.qualified_name: e for e in uow.graph.find_entities(system.id, limit=200)}

        model = entities["payments.db_models.Payment"]
        assert model.metadata.get("sqlalchemy_kind") == "model"

        handler = entities["payments.routers.checkout"]
        assert handler.metadata.get("fastapi_kind") == "route_handler"


def test_run_index_reports_a_clean_error_for_a_missing_project_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "does-not-exist"
    exit_code = _run_index(_index_args([str(missing)]))
    assert exit_code == 1
    assert "does not exist" in capsys.readouterr().err


def test_run_index_reports_a_clean_error_when_the_root_is_a_file(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a_file = project / "app" / "payments.py"
    exit_code = _run_index(_index_args([str(a_file)]))
    assert exit_code == 1
    assert "is not a directory" in capsys.readouterr().err


def test_run_index_respects_an_explicit_db_path(project: Path, tmp_path: Path) -> None:
    custom_db = tmp_path / "elsewhere" / "custom.db"
    exit_code = _run_index(_index_args([str(project), "--db", str(custom_db)]))
    assert exit_code == 0
    assert custom_db.is_file()
    assert not (project / ".hashira").exists()


def test_run_index_respects_an_explicit_system_slug(project: Path) -> None:
    exit_code = _run_index(_index_args([str(project), "--system", "custom-slug"]))
    assert exit_code == 0

    db = SqliteDatabase(str(project / ".hashira" / "hashira.db"))
    with db.unit_of_work() as uow:
        assert uow.systems.get_by_slug("custom-slug") is not None
        assert uow.systems.get_by_slug("myproject") is None


def test_run_index_derives_the_slug_from_the_directory_name(project: Path) -> None:
    exit_code = _run_index(_index_args([str(project)]))
    assert exit_code == 0

    db = SqliteDatabase(str(project / ".hashira" / "hashira.db"))
    with db.unit_of_work() as uow:
        assert uow.systems.get_by_slug(_slugify(project.name)) is not None


def test_run_index_is_idempotent_on_repeat_runs(project: Path) -> None:
    """Re-indexing the same project must reuse the same system (and
    therefore the same entity identities across revisions), not mint a
    second, colliding one -- this is what `IndexingService`'s own
    revision-to-revision identity resolution depends on."""
    first = _run_index(_index_args([str(project)]))
    second = _run_index(_index_args([str(project)]))
    assert first == 0
    assert second == 0

    db = SqliteDatabase(str(project / ".hashira" / "hashira.db"))
    with db.unit_of_work() as uow:
        systems = [s for s in [uow.systems.get_by_slug("myproject")] if s is not None]
    assert len(systems) == 1


def test_run_index_works_without_a_git_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "nogit"
    root.mkdir()
    (root / "mod.py").write_text("def f():\n    pass\n")

    exit_code = _run_index(_index_args([str(root)]))

    assert exit_code == 0
    assert "no Git repository detected" in capsys.readouterr().err

    db = SqliteDatabase(str(root / ".hashira" / "hashira.db"))
    with db.unit_of_work() as uow:
        system = uow.systems.get_by_slug("nogit")
        assert system is not None
        snapshot = uow.snapshots.latest_complete(system.id)
        assert snapshot is not None
        # `IndexingService` itself substitutes "unknown" for a `None`
        # revision (application/indexing.py) -- this asserts *that* no real
        # revision was fabricated, not that `_run_index` invented its own
        # sentinel.
        assert snapshot.revision == "unknown"


def test_run_index_warns_when_the_git_root_differs_from_the_indexed_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The monorepo/import-root caveat, made visible rather than silently
    guessed at (docs/ADAPTERS.md's own entry on this)."""
    mono_root = tmp_path / "monorepo"
    backend = mono_root / "backend"
    (backend / "app").mkdir(parents=True)
    (backend / "app" / "mod.py").write_text("def f():\n    pass\n")
    (mono_root / "frontend").mkdir()
    _git(mono_root, "init", "-q")
    _git(mono_root, "config", "user.email", "test@example.com")
    _git(mono_root, "config", "user.name", "Test")
    _git(mono_root, "add", "-A")
    _git(mono_root, "commit", "-q", "-m", "monorepo init")

    exit_code = _run_index(_index_args([str(backend), "--system", "backend"]))

    assert exit_code == 0
    err = capsys.readouterr().err
    assert "Git repository root" in err
    assert str(mono_root.resolve()) in err
    assert str(backend.resolve()) in err


def test_run_index_reports_a_parse_error_without_crashing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Malformed input (here, a syntax error) must come back as a reported,
    actionable `IndexingResult.errors` entry -- never an unhandled
    exception, and never silently dropped either."""
    root = tmp_path / "broken"
    root.mkdir()
    (root / "bad.py").write_text("def f(:\n    pass\n")  # invalid syntax

    exit_code = _run_index(_index_args([str(root)]))

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "errors: 1" in err
    assert "bad.py" in err


def test_run_index_handles_an_empty_project_without_crashing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "empty"
    root.mkdir()

    exit_code = _run_index(_index_args([str(root)]))

    assert exit_code == 0
    assert "entities upserted:       0" in capsys.readouterr().out


def test_indexed_project_is_immediately_queryable_over_mcp(project: Path) -> None:
    """The full, real user journey in one test: `hashira index` followed
    by `hashira mcp`, exercised the same way a real MCP client would --
    not just "a snapshot got written."""
    exit_code = _run_index(_index_args([str(project)]))
    assert exit_code == 0

    from hashira.mcp import build_server

    db = SqliteDatabase(str(project / ".hashira" / "hashira.db"))
    with db.unit_of_work() as uow:
        system = uow.systems.get_by_slug("myproject")
    assert system is not None

    server = build_server(db.unit_of_work, system_id=system.id, name="hashira:myproject")

    async def scenario() -> dict[str, object]:
        async with (
            InMemoryTransport(server) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool("search_entities", {"query": "PaymentService"})
            assert result.structured_content is not None
            return result.structured_content

    import asyncio

    payload = asyncio.run(scenario())
    names = {r["qualified_name"] for r in payload["results"]}  # type: ignore[union-attr]
    assert "app.payments.PaymentService" in names
