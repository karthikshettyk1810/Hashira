"""Python + Git together: the scenarios a bare Python adapter cannot resolve
on its own (renames, moves) now have real Git evidence to work with — and
the scenarios where that evidence still shouldn't be trusted stay untrusted.

Each test drives a real `git` repository (init, write, commit, mv, commit),
exactly like a developer would, then indexes it twice through the full
pipeline. No mocking of Git's output here — see `test_git_repository.py` and
`test_git_evidence.py` for the lower-level unit coverage this builds on.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from hashira.adapters.git import GitAdapter, GitRepository
from hashira.adapters.python import PythonAdapter, normalize
from hashira.application import IndexingService
from hashira.core import EntityStatus, RelationshipType, System
from hashira.storage.memory import MemoryDatabase

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "python_basic"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    shutil.copytree(FIXTURE, root)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def db() -> MemoryDatabase:
    return MemoryDatabase()


@pytest.fixture
def system(db: MemoryDatabase) -> System:
    system = System(name="Shop", slug="shop")
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()
    return system


def _service(db: MemoryDatabase) -> IndexingService:
    return IndexingService(db.unit_of_work, [PythonAdapter()], normalize, [GitAdapter()])


def _entities_by_qn(db: MemoryDatabase, system_id: str) -> dict:  # type: ignore[type-arg]
    with db.unit_of_work() as uow:
        all_entities = uow.graph.find_entities(system_id, limit=10_000)
    by_qn: dict[str, list] = {}  # type: ignore[type-arg]
    for e in all_entities:
        by_qn.setdefault(e.qualified_name, []).append(e)
    return by_qn


def _lineage(db: MemoryDatabase, system_id: str) -> set[tuple[str, str]]:
    with db.unit_of_work() as uow:
        entities = uow.graph.find_entities(system_id, limit=10_000)
        pairs = set()
        for e in entities:
            for rel in uow.graph.get_relationships(
                e.id, direction="out", types=[RelationshipType.SUPERSEDES]
            ):
                target = next((x for x in entities if x.id == rel.target_entity_id), None)
                if target is not None:
                    pairs.add((e.qualified_name, target.qualified_name))
    return pairs


# --- simple rename: identity is preserved via Git evidence -----------------


def test_simple_rename_produces_supersedes_lineage_not_an_orphan(
    repo: Path, db: MemoryDatabase, system: System
) -> None:
    """The scenario the whole Git adapter was built to close: without it,
    this was a disconnected NEW with the old entity orphaned forever."""
    git_repo = GitRepository(repo)
    service = _service(db)
    service.index(repo, system_id=system.id, revision=git_repo.current_revision())

    _git(repo, "mv", "src/shop/payments.py", "src/shop/billing.py")
    checkout = repo / "src" / "shop" / "checkout.py"
    checkout.write_text(
        checkout.read_text().replace("from .payments import", "from .billing import")
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "rename payments to billing")
    service.index(repo, system_id=system.id, revision=git_repo.current_revision())

    by_qn = _entities_by_qn(db, system.id)
    assert by_qn["shop.billing.PaymentService"][0].status is EntityStatus.ACTIVE
    assert by_qn["shop.payments.PaymentService"][0].status is EntityStatus.SUPERSEDED
    assert by_qn["shop.billing.PaymentService.process"][0].status is EntityStatus.ACTIVE
    assert by_qn["shop.payments.PaymentService.process"][0].status is EntityStatus.SUPERSEDED

    lineage = _lineage(db, system.id)
    assert ("shop.billing.PaymentService", "shop.payments.PaymentService") in lineage
    assert (
        "shop.billing.PaymentService.process",
        "shop.payments.PaymentService.process",
    ) in lineage
    assert ("shop.billing", "shop.payments") in lineage  # the module itself, too

    # And the graph stays connected: calls into the renamed class now point
    # at the surviving (new) entity, not the superseded one.
    with db.unit_of_work() as uow:
        entities = uow.graph.find_entities(system.id, limit=10_000)
        checkout_method = next(
            e for e in entities if e.qualified_name == "shop.checkout.CheckoutService.checkout"
        )
        current_calls = {
            next(t.qualified_name for t in entities if t.id == rel.target_entity_id)
            for rel in uow.graph.get_relationships(
                checkout_method.id, direction="out", types=[RelationshipType.CALLS]
            )
            if rel.is_current
        }
    assert "shop.billing.PaymentService.process" in current_calls
    assert "shop.payments.PaymentService.process" not in current_calls


def test_file_and_class_rename_with_unchanged_methods_produces_class_lineage(
    repo: Path, db: MemoryDatabase, system: System
) -> None:
    git_repo = GitRepository(repo)
    source = (
        "class ScanStore:\n"
        "    def record_vulnerability(self, vulnerability):\n"
        "        self.vulnerability = vulnerability\n\n"
        "    def set_final_scan_result(self, result):\n"
        "        self.final_result = result\n\n"
        "    def get_report_data(self):\n"
        '        return {"vulnerability": self.vulnerability}\n\n'
        "    def get_total_stats(self):\n"
        '        return {"count": 1}\n'
    )
    old_file = repo / "src" / "strix" / "telemetry" / "scan_store.py"
    old_file.parent.mkdir(parents=True)
    old_file.write_text(source)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "define ScanStore")

    service = _service(db)
    service.index(repo, system_id=system.id, revision=git_repo.current_revision())

    (repo / "src" / "strix" / "report").mkdir(parents=True)
    _git(
        repo,
        "mv",
        "src/strix/telemetry/scan_store.py",
        "src/strix/report/state.py",
    )
    new_file = repo / "src" / "strix" / "report" / "state.py"
    new_file.write_text(source.replace("ScanStore", "ReportState"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "rename ScanStore to ReportState")
    service.index(repo, system_id=system.id, revision=git_repo.current_revision())

    by_qn = _entities_by_qn(db, system.id)
    assert by_qn["strix.report.state.ReportState"][0].status is EntityStatus.ACTIVE
    assert by_qn["strix.telemetry.scan_store.ScanStore"][0].status is EntityStatus.SUPERSEDED
    assert (
        "strix.report.state.ReportState",
        "strix.telemetry.scan_store.ScanStore",
    ) in _lineage(db, system.id)


def test_a_later_unrelated_reindex_does_not_close_the_lineage_edge(
    repo: Path, db: MemoryDatabase, system: System
) -> None:
    """A real bug found while building the Identity Resolution v0.2
    milestone: `_reconcile_relationships` fetched *every* outgoing edge for
    reconciliation, including `SUPERSEDES` -- which this run's structural
    observations never contain, since it is a one-time historical fact, not
    a recurring one. Without an explicit exclusion, a `SUPERSEDES` edge
    read as "no longer observed" on the very next, otherwise unrelated
    re-index and got closed (`valid_until_revision` set), silently erasing
    lineage that `application.history`/`application.impact` both depend on
    being permanent. Caught here by doing exactly what no earlier Git-rename
    test did: index a third time, on an unrelated change, after the rename
    that created the lineage in the first place."""
    git_repo = GitRepository(repo)
    service = _service(db)
    service.index(repo, system_id=system.id, revision=git_repo.current_revision())

    _git(repo, "mv", "src/shop/payments.py", "src/shop/billing.py")
    checkout = repo / "src" / "shop" / "checkout.py"
    checkout.write_text(
        checkout.read_text().replace("from .payments import", "from .billing import")
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "rename payments to billing")
    service.index(repo, system_id=system.id, revision=git_repo.current_revision())

    # A third, unrelated change and re-index -- nothing here touches
    # billing.py/payments.py at all.
    readme = repo / "README.md"
    readme.write_text("unrelated change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "unrelated change")
    service.index(repo, system_id=system.id, revision=git_repo.current_revision())

    with db.unit_of_work() as uow:
        entities = uow.graph.find_entities(system.id, limit=10_000)
        by_qn = {e.qualified_name: e for e in entities}
        new_module = by_qn["shop.billing"]
        lineage = [
            rel
            for rel in uow.graph.get_relationships(
                new_module.id, direction="out", types=[RelationshipType.SUPERSEDES]
            )
        ]
    assert len(lineage) == 1
    assert lineage[0].is_current
    assert lineage[0].valid_until_revision is None


# --- module move: a directory move, not just a same-directory rename -----


def test_module_move_into_a_subdirectory_preserves_lineage(
    repo: Path, db: MemoryDatabase, system: System
) -> None:
    git_repo = GitRepository(repo)
    service = _service(db)
    service.index(repo, system_id=system.id, revision=git_repo.current_revision())

    (repo / "src" / "shop" / "services").mkdir()
    _git(repo, "mv", "src/shop/payments.py", "src/shop/services/payments.py")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "move payments into services/")
    service.index(repo, system_id=system.id, revision=git_repo.current_revision())

    by_qn = _entities_by_qn(db, system.id)
    assert by_qn["shop.services.payments.PaymentService"][0].status is EntityStatus.ACTIVE
    assert by_qn["shop.payments.PaymentService"][0].status is EntityStatus.SUPERSEDED

    lineage = _lineage(db, system.id)
    assert (
        "shop.services.payments.PaymentService",
        "shop.payments.PaymentService",
    ) in lineage


# --- rename + modest content edit: still linked, at lower confidence -----


def test_rename_with_a_modest_edit_is_still_linked(
    repo: Path, db: MemoryDatabase, system: System
) -> None:
    """A rename plus a genuinely small edit -- one line changed out of the
    whole file -- should still clear the similarity threshold, and at LIKELY
    (not CERTAIN) confidence, since the content isn't byte-identical."""
    git_repo = GitRepository(repo)
    service = _service(db)
    service.index(repo, system_id=system.id, revision=git_repo.current_revision())

    _git(repo, "mv", "src/shop/payments.py", "src/shop/billing.py")
    billing = repo / "src" / "shop" / "billing.py"
    billing.write_text(
        billing.read_text().replace(
            "return is_positive(amount)", "return bool(is_positive(amount))"
        )
    )
    checkout = repo / "src" / "shop" / "checkout.py"
    checkout.write_text(
        checkout.read_text().replace("from .payments import", "from .billing import")
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "rename payments to billing, tweak one line")
    service.index(repo, system_id=system.id, revision=git_repo.current_revision())

    by_qn = _entities_by_qn(db, system.id)
    assert by_qn["shop.billing"][0].status is EntityStatus.ACTIVE
    assert by_qn["shop.payments"][0].status is EntityStatus.SUPERSEDED
    assert by_qn["shop.billing.PaymentService"][0].status is EntityStatus.ACTIVE
    assert by_qn["shop.payments.PaymentService"][0].status is EntityStatus.SUPERSEDED

    lineage = _lineage(db, system.id)
    assert ("shop.billing.PaymentService", "shop.payments.PaymentService") in lineage

    with db.unit_of_work() as uow:
        entities = uow.graph.find_entities(system.id, limit=10_000)
        new_module = next(e for e in entities if e.qualified_name == "shop.billing")
        superseded_rel = next(
            rel
            for rel in uow.graph.get_relationships(
                new_module.id, direction="out", types=[RelationshipType.SUPERSEDES]
            )
        )
    # Git reports this specific edit at 90% similarity -- clears this
    # project's threshold but well below the 98% near-exact bar, so the
    # lineage should be LIKELY, not CERTAIN (identity/git_evidence.py).
    assert superseded_rel.confidence.value == "LIKELY"


# --- the adversarial cases: refuse to guess -------------------------------


def test_rename_plus_symbol_rename_links_the_module_not_the_symbol(
    repo: Path, db: MemoryDatabase, system: System
) -> None:
    """ "The really interesting one": the file moved *and* the class's name
    changed in the same commit. Path and name are the only two things this
    adapter can pair on, and both changed at once -- the module still links
    (file path alone is enough there), but the class itself must not."""
    git_repo = GitRepository(repo)
    service = _service(db)
    service.index(repo, system_id=system.id, revision=git_repo.current_revision())

    _git(repo, "mv", "src/shop/payments.py", "src/shop/billing.py")
    billing = repo / "src" / "shop" / "billing.py"
    billing.write_text(billing.read_text().replace("PaymentService", "PaymentProcessor"))
    checkout = repo / "src" / "shop" / "checkout.py"
    checkout.write_text(
        checkout.read_text()
        .replace("from .payments import PaymentService", "from .billing import PaymentProcessor")
        .replace("PaymentService()", "PaymentProcessor()")
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "rename file and class together")
    service.index(repo, system_id=system.id, revision=git_repo.current_revision())

    by_qn = _entities_by_qn(db, system.id)
    assert by_qn["shop.billing"][0].status is EntityStatus.ACTIVE
    assert by_qn["shop.payments"][0].status is EntityStatus.SUPERSEDED
    # The renamed class itself: no lineage, orphaned old entity, exactly the
    # pre-Git behavior -- because nothing here justifies connecting them.
    assert by_qn["shop.billing.PaymentProcessor"][0].status is EntityStatus.ACTIVE
    assert (
        by_qn["shop.payments.PaymentService"][0].status is EntityStatus.ACTIVE
    )  # orphaned, not superseded

    lineage = _lineage(db, system.id)
    assert ("shop.billing", "shop.payments") in lineage
    assert ("shop.billing.PaymentProcessor", "shop.payments.PaymentService") not in lineage


def test_replacement_disguised_as_a_rename_is_not_linked(
    repo: Path, db: MemoryDatabase, system: System
) -> None:
    """Git mv plus a total content rewrite. Better to say NEW than to
    confidently connect two files that share nothing but a git-mv command."""
    git_repo = GitRepository(repo)
    service = _service(db)
    service.index(repo, system_id=system.id, revision=git_repo.current_revision())

    _git(repo, "mv", "src/shop/payments.py", "src/shop/billing.py")
    billing = repo / "src" / "shop" / "billing.py"
    billing.write_text("class UnrelatedThing:\n    def do_something_else(self):\n        pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "total rewrite disguised as a rename")
    service.index(repo, system_id=system.id, revision=git_repo.current_revision())

    by_qn = _entities_by_qn(db, system.id)
    assert by_qn["shop.payments.PaymentService"][0].status is EntityStatus.REMOVED
    assert "shop.billing.UnrelatedThing" in by_qn
    assert by_qn["shop.billing.UnrelatedThing"][0].status is EntityStatus.ACTIVE

    lineage = _lineage(db, system.id)
    assert not any(source.startswith("shop.billing") for source, _ in lineage)


def test_low_similarity_rename_below_threshold_is_not_linked_even_if_git_detects_it(
    repo: Path, db: MemoryDatabase, system: System
) -> None:
    """A direct test of the threshold itself (identity/git_evidence.py):
    construct a rename Git *does* classify as a rename (content still
    shares some structure) but below this project's stricter 90% bar."""
    git_repo = GitRepository(repo)
    service = _service(db)
    service.index(repo, system_id=system.id, revision=git_repo.current_revision())

    payments = repo / "src" / "shop" / "payments.py"
    original = payments.read_text()
    _git(repo, "mv", "src/shop/payments.py", "src/shop/billing.py")
    billing = repo / "src" / "shop" / "billing.py"
    # Keep only a small fragment of the original -- similarity should land
    # somewhere below 90% but Git may still (or may not) call it a rename;
    # either way, our threshold must not let it link.
    billing.write_text(
        original.split("class PaymentService")[0] + "class TotallyDifferent:\n    pass\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "mostly rewritten, kept as a rename by git mv")
    service.index(repo, system_id=system.id, revision=git_repo.current_revision())

    lineage = _lineage(db, system.id)
    assert not any("billing" in source for source, _ in lineage)
