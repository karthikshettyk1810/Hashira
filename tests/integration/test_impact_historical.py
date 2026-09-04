"""The Impact Analysis milestone's own brutal integration test: FastAPI +
SQLAlchemy + Python + Git, all at once, asking revision-scoped impact
questions instead of just proving the graph connects.

Real Git history over the `fastapi_checkout` fixture (which already wires
FastAPI's route/handler to a genuine SQLAlchemy `Payment.status` column --
see `test_fastapi_sqlalchemy_together.py`):

* **A** -- the fixture as it stands: `POST /checkout/` -> `checkout()` ->
  `PaymentService.process` -> writes/reads `payments.status`, exercised
  directly by both the route and a unit test.
* **B** -- `payments/db_models.py` moves to `payments/models.py` (a pure
  Git rename, not a content edit) -- the `Payment` *class*'s qualified name
  changes (`payments.db_models.Payment` -> `payments.models.Payment`), so
  it resolves as `SUPERSEDES` with Git-backed lineage, exactly like
  `test_git_identity.py`'s already-proven module-rename scenario. The
  `payments.status` *column* entity, by contrast, is unaffected at the
  identity level: its qualified name is `table.field` (`"payments.status"`),
  which the file move never touches, so it resolves `MATCHED` -- same
  entity id before and after, no lineage-following needed for it at all.
  (A bare attribute-level rename -- `status` becoming a differently-named
  column with no corresponding file move -- has no Git-backed identity
  signal to attach today, the same documented limitation
  `identity/git_evidence.py` already states for a Python symbol renamed in
  the same commit as its file; that is a future identity-resolution
  milestone, not this one.)
* **C** -- `checkout()` stops calling `PaymentService` entirely -- a real
  structural break, not a rename.

Then:

* `reverse_impact(payments.status, revision=A)` must reach the service,
  the handler, the route, and the test.
* Asking about the *old* `Payment` class id at revision B must resolve,
  via `SUPERSEDES` lineage, to the entity at its new location -- and a
  `reverse_impact` query using that old id must still reach the same
  impacted set, proving the query survives the rename rather than silently
  returning nothing.
* `forward_impact(checkout, revision=C)` must no longer include
  `PaymentService.process` -- reflecting C's actual state, not A's -- while
  the same query at B (before the break) still does.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from hashira.adapters._compose import compose_normalizers
from hashira.adapters.fastapi import FastAPIAdapter
from hashira.adapters.fastapi.normalizer import enrich_normalized_run as enrich_fastapi
from hashira.adapters.git import GitAdapter, GitRepository
from hashira.adapters.python import PythonAdapter
from hashira.adapters.sqlalchemy import SQLAlchemyAdapter
from hashira.adapters.sqlalchemy.normalizer import enrich_normalized_run as enrich_sqlalchemy
from hashira.application import IndexingService
from hashira.application.history import query_at_revision
from hashira.application.impact import forward_impact, resolve_identity, reverse_impact
from hashira.core import EntityStatus, System
from hashira.storage.memory import MemoryDatabase

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fastapi_checkout"


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
    system = System(name="Checkout", slug="checkout-impact-history")
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()
    return system


def _service(db: MemoryDatabase) -> IndexingService:
    normalize = compose_normalizers(enrich_fastapi, enrich_sqlalchemy)
    return IndexingService(
        db.unit_of_work,
        [PythonAdapter()],
        normalize,
        framework_adapters=[FastAPIAdapter()],
        data_adapters=[SQLAlchemyAdapter()],
        history_adapters=[GitAdapter()],
    )


def test_impact_analysis_across_a_rename_and_a_structural_break(
    repo: Path, db: MemoryDatabase, system: System
) -> None:
    service = _service(db)

    # --- A: the full, working chain -----------------------------------
    sha_a = _commit(repo, "A: checkout flow with a real SQLAlchemy model")
    result_a = service.index(repo, system_id=system.id, revision=sha_a)
    assert result_a.errors == []

    with db.unit_of_work() as uow:
        graph_a = query_at_revision(uow, system_id=system.id, revision=sha_a)
    status_column = graph_a.entity("payments.status")
    payment_class_at_a = graph_a.entity("payments.db_models.Payment")
    assert status_column is not None
    assert payment_class_at_a is not None

    with db.unit_of_work() as uow:
        impact_at_a = reverse_impact(
            uow, system_id=system.id, entity_id=status_column.id, revision=sha_a
        )
    affected_at_a = {e.qualified_name for e in impact_at_a.affected_entities}
    assert affected_at_a == {
        "payments.services.PaymentService.process",
        "payments.services.PaymentService.mark_refunded",
        "payments.routers.checkout",
        "payments.routers.router:POST /payments/checkout/",
        "payments.tests.test_checkout.TestCheckout.test_process_marks_payment_captured",
    }
    # Every hop actually carries its own relationship, unmodified -- the
    # "explainable path" promise, not just a bag of names.
    for path in impact_at_a.paths:
        for hop in path.hops:
            assert hop.relationship.evidence_ids
            assert hop.relationship.confidence is not None

    # --- B: payments/db_models.py -> payments/models.py, a pure rename -
    _git(repo, "mv", "payments/db_models.py", "payments/models.py")
    services_py = repo / "payments" / "services.py"
    services_py.write_text(services_py.read_text().replace(".db_models", ".models"))
    sha_b = _commit(repo, "B: reorganize db_models.py into models.py")
    result_b = service.index(repo, system_id=system.id, revision=sha_b)
    assert result_b.errors == []

    with db.unit_of_work() as uow:
        graph_b = query_at_revision(uow, system_id=system.id, revision=sha_b)
    # The column's identity is untouched by the move (its qualified name is
    # `table.field`, not path-based) -- same entity, still ACTIVE, no
    # lineage-following needed.
    status_column_at_b = graph_b.entity("payments.status")
    assert status_column_at_b is not None
    assert status_column_at_b.id == status_column.id
    assert status_column_at_b.status is EntityStatus.ACTIVE

    # The *class*'s qualified name did change with the file -- it resolves
    # as SUPERSEDES, with Git-backed lineage, not MATCHED.
    payment_class_at_b = graph_b.entity("payments.models.Payment")
    assert payment_class_at_b is not None
    assert payment_class_at_b.id != payment_class_at_a.id

    with db.unit_of_work() as uow:
        resolved = resolve_identity(graph_b, payment_class_at_a.id)
        assert resolved is not None
        assert resolved.id == payment_class_at_b.id

        # Asking reverse_impact about the *old* class id at revision B must
        # still work -- it transparently follows the lineage rather than
        # returning nothing because that id is no longer "present".
        impact_via_old_id = reverse_impact(
            uow, system_id=system.id, entity_id=payment_class_at_a.id, revision=sha_b
        )
    assert impact_via_old_id.resolved_from == payment_class_at_a.id
    assert impact_via_old_id.start.id == payment_class_at_b.id
    assert "payments.services.PaymentService.process" in {
        e.qualified_name for e in impact_via_old_id.affected_entities
    }

    # --- C: checkout() stops calling the service ------------------------
    routers_py = repo / "payments" / "routers.py"
    routers_py.write_text(
        routers_py.read_text().replace(
            "    service = PaymentService()\n"
            "    result = service.process(body.amount)\n"
            "    return PaymentResponse(status=result)\n",
            '    return PaymentResponse(status="not_implemented")\n',
        )
    )
    sha_c = _commit(repo, "C: checkout no longer calls the payment service")
    result_c = service.index(repo, system_id=system.id, revision=sha_c)
    assert result_c.errors == []

    with db.unit_of_work() as uow:
        checkout_handler = query_at_revision(uow, system_id=system.id, revision=sha_c).entity(
            "payments.routers.checkout"
        )
        assert checkout_handler is not None

        forward_at_b = forward_impact(
            uow, system_id=system.id, entity_id=checkout_handler.id, revision=sha_b
        )
        forward_at_c = forward_impact(
            uow, system_id=system.id, entity_id=checkout_handler.id, revision=sha_c
        )

    process_qn = "payments.services.PaymentService.process"
    assert process_qn in {e.qualified_name for e in forward_at_b.affected_entities}
    assert process_qn not in {e.qualified_name for e in forward_at_c.affected_entities}

    # And "current" (no revision) means C, not A -- the break was never
    # fixed.
    with db.unit_of_work() as uow:
        forward_current = forward_impact(uow, system_id=system.id, entity_id=checkout_handler.id)
    assert process_qn not in {e.qualified_name for e in forward_current.affected_entities}
