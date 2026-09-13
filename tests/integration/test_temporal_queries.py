"""The temporal-queries milestone's brutal integration test: can Hashira say
what the system looked like at a particular point in its history, using
evidence-backed identity rather than today's source tree? (see the design
discussion this was built from, and `application/history.py`.)

Two scenarios, each driving a real `git` repository through several commits
and indexing after every one -- exactly like `test_git_identity.py`, just
asking `application.history.query_at_revision` instead of the plain,
current-only ports:

* `test_query_at_an_old_revision_shows_the_old_chain_not_todays_state` --
  the impact chain for `Payment.status` exists at commit A, gets broken by a
  structural edit at commit C, and stays broken today (D). Querying at A
  must show the old chain; querying at C, or with no revision at all, must
  not -- the whole point is that this is ancestry-aware, not "restrict
  today's graph to files that still exist."
* `test_identity_lineage_survives_a_rename_across_revisions` -- the same
  rename scenario `test_git_identity.py` already proves resolves as
  SUPERSEDES-with-lineage, but now asked at each revision along the way: the
  pre-rename entity is visible before the rename and gone after, the
  post-rename entity is the reverse, and a *different*, disguised rename in
  the same history still refuses to link -- proving the revision-query layer
  does not accidentally launder a bad merge into looking legitimate just
  because time has passed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from hashira.adapters.django import DjangoAdapter
from hashira.adapters.django import normalize as django_normalize
from hashira.adapters.git import GitAdapter, GitRepository
from hashira.adapters.python import PythonAdapter
from hashira.application import IndexingService
from hashira.application.history import HistoricalGraph, query_at_revision
from hashira.core import EntityStatus, IdentityClaimKind, RelationshipType, System
from hashira.storage.memory import MemoryDatabase

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "django_basic"

_STRUCTURAL = {RelationshipType.CONTAINS, RelationshipType.DEFINES}


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return GitRepository(root).current_revision()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    shutil.copytree(FIXTURE, root)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    return root


@pytest.fixture
def db() -> MemoryDatabase:
    return MemoryDatabase()


@pytest.fixture
def system(db: MemoryDatabase) -> System:
    system = System(name="Checkout", slug="checkout")
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()
    return system


def _service(db: MemoryDatabase) -> IndexingService:
    return IndexingService(
        db.unit_of_work,
        [PythonAdapter()],
        django_normalize,
        history_adapters=[GitAdapter()],
        framework_adapters=[DjangoAdapter()],
    )


def _reverse_impact(hist: HistoricalGraph, start_qn: str) -> set[str]:
    by_qn = {e.qualified_name: e for e in hist.entities}
    by_id = {e.id: e for e in hist.entities}
    start = by_qn[start_qn]
    seen = {start.id}
    frontier = [start.id]
    while frontier:
        next_frontier: list[str] = []
        for eid in frontier:
            for rel in hist.relationships:
                if rel.type in _STRUCTURAL:
                    continue
                if rel.target_entity_id == eid and rel.source_entity_id not in seen:
                    seen.add(rel.source_entity_id)
                    next_frontier.append(rel.source_entity_id)
        frontier = next_frontier
    return {by_id[i].qualified_name for i in seen if i != start.id}


# --- scenario 1: the impact chain at an old revision vs. today -------------


def test_query_at_an_old_revision_shows_the_old_chain_not_todays_state(
    repo: Path, db: MemoryDatabase, system: System
) -> None:
    service = _service(db)

    # A: the fixture as it stands -- full route -> view -> service -> field chain.
    sha_a = _commit(repo, "A: initial checkout flow")
    service.index(repo, system_id=system.id, revision=sha_a)

    # B: a content-only tweak to the service -- no structural change, just
    # proves ancestry still reaches back to A's facts through an
    # intermediate revision.
    services_py = repo / "payments" / "services.py"
    services_py.write_text(services_py.read_text().replace('"captured"', '"confirmed"'))
    sha_b = _commit(repo, "B: rename the captured status to confirmed")
    service.index(repo, system_id=system.id, revision=sha_b)

    # C: the view stops calling the service at all -- a real structural
    # break, not a rename Git could offer evidence about.
    views_py = repo / "payments" / "views.py"
    views_py.write_text(
        "from django.http import JsonResponse\n"
        "from django.views import View\n\n\n"
        "class CheckoutView(View):\n"
        "    def post(self, request):\n"
        '        return JsonResponse({"status": "not_implemented"})\n'
    )
    sha_c = _commit(repo, "C: view no longer calls the payment service")
    service.index(repo, system_id=system.id, revision=sha_c)

    # D: the route itself changes -- a later fact that must not leak
    # backwards into queries at A/B/C.
    urls_py = repo / "payments" / "urls.py"
    urls_py.write_text(urls_py.read_text().replace('"checkout/"', '"checkout/v2/"'))
    sha_d = _commit(repo, "D: route moves to checkout/v2/")
    service.index(repo, system_id=system.id, revision=sha_d)

    with db.unit_of_work() as uow:
        hist_a = query_at_revision(uow, system_id=system.id, revision=sha_a)
        hist_b = query_at_revision(uow, system_id=system.id, revision=sha_b)
        hist_c = query_at_revision(uow, system_id=system.id, revision=sha_c)
        hist_current = query_at_revision(uow, system_id=system.id, revision=None)

    # At A and B, the full chain from the design discussion is intact.
    full_chain = {
        "payments.services.PaymentService.process",
        "payments.views.CheckoutView.post",
        "payments.tests.test_checkout.TestCheckout.test_process_marks_payment_captured",
    }
    assert _reverse_impact(hist_a, "payments.models.Payment.status") == full_chain
    assert _reverse_impact(hist_b, "payments.models.Payment.status") == full_chain

    # At C -- and today, since the view was never fixed -- the view drops
    # out of the impact set. This must be true from the *ancestry-filtered*
    # historical query, not merely from asking storage for "current" state.
    broken_chain = full_chain - {"payments.views.CheckoutView.post"}
    assert _reverse_impact(hist_c, "payments.models.Payment.status") == broken_chain
    assert _reverse_impact(hist_current, "payments.models.Payment.status") == broken_chain

    # The route change at D must not be visible looking backwards.
    assert hist_a.entity("payments.urls:checkout/v2/") is None
    assert hist_c.entity("payments.urls:checkout/v2/") is None
    assert hist_current.entity("payments.urls:checkout/v2/") is not None
    # ...and the pre-D route is exactly what A/B/C see.
    assert hist_a.entity("payments.urls:checkout/") is not None
    assert hist_c.entity("payments.urls:checkout/") is not None

    # Plain, no-revision querying means "current," matching an ordinary
    # (non-historical) query of the same system -- not "as of A."
    with db.unit_of_work() as uow:
        live_entities = {e.qualified_name for e in uow.graph.find_entities(system.id, limit=10_000)}
    assert {e.qualified_name for e in hist_current.entities} <= live_entities
    assert sha_d  # the run that produced "today" -- referenced for clarity


# --- scenario 2: identity lineage across a rename, plus an adversarial case -


def test_identity_lineage_survives_a_rename_across_revisions(
    repo: Path, db: MemoryDatabase, system: System
) -> None:
    service = _service(db)

    sha_a = _commit(repo, "A: initial checkout flow")
    service.index(repo, system_id=system.id, revision=sha_a)

    # B: payments/services.py -> billing/services.py, a genuine cross-package
    # move. Kept as a *pure* rename (Git reports it at 100% similarity) by
    # touching only the other files that imported it in this commit --
    # editing the moved file's own content here would dilute its similarity
    # score below this project's stricter rename threshold
    # (`identity/git_evidence.py::DEFAULT_MIN_SIMILARITY`) and the point of
    # this test is that the rename evidence *is* trusted.
    (repo / "billing").mkdir()
    (repo / "billing" / "__init__.py").write_text("")
    _git(repo, "mv", "payments/services.py", "billing/services.py")
    (repo / "payments" / "views.py").write_text(
        (repo / "payments" / "views.py")
        .read_text()
        .replace(
            "from .services import PaymentService", "from billing.services import PaymentService"
        )
    )
    (repo / "payments" / "tests" / "test_checkout.py").write_text(
        (repo / "payments" / "tests" / "test_checkout.py")
        .read_text()
        .replace(
            "from payments.services import PaymentService",
            "from billing.services import PaymentService",
        )
    )
    sha_b = _commit(repo, "B: move services.py into a new billing package")
    service.index(repo, system_id=system.id, revision=sha_b)

    # C: a trivial follow-up in the new location -- fixes the now-relative
    # import that broke when the file moved packages, and proves the entity
    # stays put (MATCHED, not another SUPERSEDES) once it has landed.
    billing_services = repo / "billing" / "services.py"
    billing_services.write_text(
        "# billing domain\n"
        + billing_services.read_text().replace(
            "from .models import Payment", "from payments.models import Payment"
        )
    )
    sha_c = _commit(repo, "C: fix the cross-package import, add a comment")
    service.index(repo, system_id=system.id, revision=sha_c)

    # D: an unrelated, *disguised* rename -- git mv plus a total rewrite of
    # payments/models.py. This must NOT be linked, at any revision, exactly
    # like test_git_identity.py's equivalent case with no revision involved.
    _git(repo, "mv", "payments/models.py", "payments/ledger.py")
    (repo / "payments" / "ledger.py").write_text(
        "class UnrelatedThing:\n    def do_something_else(self):\n        pass\n"
    )
    sha_d = _commit(repo, "D: disguised replacement of models.py")
    service.index(repo, system_id=system.id, revision=sha_d)

    with db.unit_of_work() as uow:
        hist_a = query_at_revision(uow, system_id=system.id, revision=sha_a)
        hist_b = query_at_revision(uow, system_id=system.id, revision=sha_b)
        hist_c = query_at_revision(uow, system_id=system.id, revision=sha_c)
        hist_d = query_at_revision(uow, system_id=system.id, revision=sha_d)
        hist_current = query_at_revision(uow, system_id=system.id, revision=None)

    old_qn = "payments.services.PaymentService.process"
    new_qn = "billing.services.PaymentService.process"

    # Before the rename: only the old entity exists.
    assert hist_a.entity(old_qn) is not None
    assert hist_a.entity(new_qn) is None

    # At and after the rename: only the new one is live, at every later
    # revision including a run three commits on and "today."
    for hist in (hist_b, hist_c, hist_d, hist_current):
        assert hist.entity(old_qn) is None, hist.revision
        new_entity = hist.entity(new_qn)
        assert new_entity is not None, hist.revision
        assert new_entity.status is EntityStatus.ACTIVE

    # The lineage is real, evidence-backed identity, not a coincidence of
    # matching names: a GIT_RENAME claim is on record, and a SUPERSEDES edge
    # connects the surviving entity back to the one it replaced.
    new_entity = hist_b.entity(new_qn)
    assert new_entity is not None
    assert any(c.kind is IdentityClaimKind.GIT_RENAME for c in new_entity.identity_claims)
    lineage_targets = {
        rel.target_entity_id
        for rel in hist_b.relationships
        if rel.type is RelationshipType.SUPERSEDES and rel.source_entity_id == new_entity.id
    }
    old_entity_at_a = hist_a.entity(old_qn)
    assert old_entity_at_a is not None
    assert old_entity_at_a.id in lineage_targets

    # Adversarial case, now checked *through the revision-scoped query*: the
    # disguised rename of models.py must never produce a lineage edge, at D
    # or afterwards -- the old Payment entity was removed at D, not superseded.
    payment_qn = "payments.models.Payment"
    assert hist_a.entity(payment_qn) is not None
    assert hist_b.entity(payment_qn) is not None
    assert hist_c.entity(payment_qn) is not None
    payment_a_id = hist_a.entity(payment_qn).id  # type: ignore
    for hist in (hist_d, hist_current):
        assert hist.entity(payment_qn) is None, hist.revision
        assert not any(
            rel.type is RelationshipType.SUPERSEDES and rel.target_entity_id == payment_a_id
            for rel in hist.relationships
        )
    # And the entity introduced by the disguised rewrite must not be
    # visible looking backwards, same as any other later fact.
    assert hist_a.entity("payments.ledger.UnrelatedThing") is None
    assert hist_d.entity("payments.ledger.UnrelatedThing") is not None
