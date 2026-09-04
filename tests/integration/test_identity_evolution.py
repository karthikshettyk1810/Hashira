"""Identity Resolution v0.2's own brutal integration test: a symbol renamed
*within* an unchanged file -- the gap `test_impact_historical.py` explicitly
declined to fake, closed here with declaration-level lineage evidence
instead of name/content similarity.

Real Git history over the `fastapi_checkout` fixture, in three deliberately
separate steps (not collapsed into one atomic rename, because the gap
between them is itself the interesting thing to query):

* **A** -- `Payment.status` exists; `PaymentService.process` writes and
  reads it; the route and a test both reach it, exactly as
  `test_impact_historical.py` already proved.
* **B** -- *only* `payments/db_models.py` changes: the column becomes
  `state`. `payments/services.py` is not touched yet -- it still assigns
  `payment.status`, which is no longer a column this system knows the
  `Payment` model to have, so that assignment is honestly *not* tracked as
  a field access at all (§12: an absent fact, not a guessed one). The old
  `payments.status` entity resolves `SUPERSEDES` via `DECLARATION_LINEAGE`
  evidence (`identity/declaration_evidence.py`) -- Git never reported this
  file as renamed, so `GIT_RENAME` had nothing to detect; this is the
  mechanism built specifically for a declaration renamed within an
  unchanged file.
* **C** -- `payments/services.py` catches up: every `.status` becomes
  `.state`. Only *now* does `PaymentService.process` actually write/read
  `payments.state` again.

This proves the two questions this milestone was built to keep separate:
"did the declaration's identity survive the rename" (yes, from B onward,
via evidence) and "does anything actually use the new declaration yet"
(no, until C) -- collapsing them into one commit would have hidden exactly
the distinction the milestone's own design discussion asked to preserve.
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
from hashira.application.impact import reverse_impact
from hashira.core import Confidence, EntityStatus, IdentityClaimKind, RelationshipType, System
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
    system = System(name="Checkout", slug="checkout-identity-evolution")
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


def test_declaration_lineage_survives_a_rename_the_usage_catches_up_to_later(
    repo: Path, db: MemoryDatabase, system: System
) -> None:
    service = _service(db)

    # --- A: Payment.status exists and is genuinely used --------------
    sha_a = _commit(repo, "A: checkout flow writes Payment.status")
    result_a = service.index(repo, system_id=system.id, revision=sha_a)
    assert result_a.errors == []

    with db.unit_of_work() as uow:
        graph_a = query_at_revision(uow, system_id=system.id, revision=sha_a)
    status_at_a = graph_a.entity("payments.status")
    assert status_at_a is not None
    assert graph_a.entity("payments.state") is None

    with db.unit_of_work() as uow:
        impact_at_a = reverse_impact(
            uow, system_id=system.id, entity_id=status_at_a.id, revision=sha_a
        )
    assert {e.qualified_name for e in impact_at_a.affected_entities} == {
        "payments.services.PaymentService.process",
        "payments.services.PaymentService.mark_refunded",
        "payments.routers.checkout",
        "payments.routers.router:POST /payments/checkout/",
        "payments.tests.test_checkout.TestCheckout.test_process_marks_payment_captured",
    }

    # --- B: only the model renames -- services.py has not caught up --
    db_models_py = repo / "payments" / "db_models.py"
    db_models_py.write_text(db_models_py.read_text().replace("status", "state"))
    sha_b = _commit(repo, "B: rename the status column to state")
    result_b = service.index(repo, system_id=system.id, revision=sha_b)
    assert result_b.errors == []

    with db.unit_of_work() as uow:
        graph_b = query_at_revision(uow, system_id=system.id, revision=sha_b)
    status_at_b = graph_b.entity("payments.status")
    state_at_b = graph_b.entity("payments.state")
    assert status_at_b is None  # superseded, no longer "present"
    assert state_at_b is not None
    assert state_at_b.status is EntityStatus.ACTIVE

    # The lineage itself: evidence-backed, not an assumption. A
    # DECLARATION_LINEAGE claim (not GIT_RENAME -- the file never moved)
    # sits on both sides, and the resolver recorded genuine SUPERSEDES
    # lineage, not a silent merge.
    assert any(c.kind is IdentityClaimKind.DECLARATION_LINEAGE for c in state_at_b.identity_claims)
    with db.unit_of_work() as uow:
        lineage = uow.graph.get_relationships(
            state_at_b.id, direction="out", types=[RelationshipType.SUPERSEDES]
        )
    assert any(rel.target_entity_id == status_at_a.id for rel in lineage)
    superseding_rel = next(rel for rel in lineage if rel.target_entity_id == status_at_a.id)
    assert superseding_rel.confidence is Confidence.CERTAIN  # shape matched exactly

    # Querying the OLD id at B must resolve, via lineage, to the new one --
    # "what happened to the field I was looking at" answered with evidence,
    # not "that entity doesn't exist anymore".
    with db.unit_of_work() as uow:
        impact_via_old_id_at_b = reverse_impact(
            uow, system_id=system.id, entity_id=status_at_a.id, revision=sha_b
        )
    assert impact_via_old_id_at_b.resolved_from == status_at_a.id
    assert impact_via_old_id_at_b.start.id == state_at_b.id
    # But nothing actually reaches it yet -- services.py still writes the
    # old name, which the model no longer declares, so that assignment is
    # honestly untracked rather than guessed to still mean the same thing.
    assert impact_via_old_id_at_b.affected_entities == ()

    # --- C: services.py finally catches up ----------------------------
    services_py = repo / "payments" / "services.py"
    services_py.write_text(services_py.read_text().replace("status", "state"))
    sha_c = _commit(repo, "C: update the service to use the renamed column")
    result_c = service.index(repo, system_id=system.id, revision=sha_c)
    assert result_c.errors == []

    with db.unit_of_work() as uow:
        graph_c = query_at_revision(uow, system_id=system.id, revision=sha_c)
    state_at_c = graph_c.entity("payments.state")
    assert state_at_c is not None
    assert graph_c.entity("payments.status") is None

    with db.unit_of_work() as uow:
        impact_at_c = reverse_impact(
            uow, system_id=system.id, entity_id=state_at_c.id, revision=sha_c
        )
    assert {e.qualified_name for e in impact_at_c.affected_entities} == {
        "payments.services.PaymentService.process",
        "payments.services.PaymentService.mark_refunded",
        "payments.routers.checkout",
        "payments.routers.router:POST /payments/checkout/",
        "payments.tests.test_checkout.TestCheckout.test_process_marks_payment_captured",
    }

    # And asking about the *original* A-era id, all the way from C, still
    # resolves through the same lineage to today's real impact set.
    with db.unit_of_work() as uow:
        impact_via_original_id_at_c = reverse_impact(
            uow, system_id=system.id, entity_id=status_at_a.id, revision=sha_c
        )
    assert impact_via_original_id_at_c.resolved_from == status_at_a.id
    assert impact_via_original_id_at_c.start.id == state_at_c.id
    assert {e.qualified_name for e in impact_via_original_id_at_c.affected_entities} == {
        e.qualified_name for e in impact_at_c.affected_entities
    }


def test_a_type_change_alongside_the_rename_is_not_auto_linked(
    repo: Path, db: MemoryDatabase, system: System
) -> None:
    """The adversarial case: `status` (a `String`) disappears and `state`
    (an `Integer`) appears in the same table, in the same commit. Name and
    position both point at a correspondence, but the type family does not
    -- per the milestone's own instruction ("the system should not
    automatically conclude 'same entity because the name changed'"), no
    `DECLARATION_LINEAGE` claim is proposed at all. The old entity is left
    exactly where every other evidence-free case already leaves it:
    orphaned, `ACTIVE`, unlinked -- not merged, not silently dropped."""
    service = _service(db)

    sha_a = _commit(repo, "A: checkout flow writes Payment.status")
    service.index(repo, system_id=system.id, revision=sha_a)

    db_models_py = repo / "payments" / "db_models.py"
    db_models_py.write_text(
        db_models_py.read_text()
        .replace(
            "from sqlalchemy import Column, Integer, String",
            "from sqlalchemy import Column, Integer",
        )
        .replace("status = Column(String(20))", "state = Column(Integer)")
    )
    sha_b = _commit(repo, "B: rename status to state AND change its type")
    result_b = service.index(repo, system_id=system.id, revision=sha_b)
    assert result_b.errors == []

    with db.unit_of_work() as uow:
        graph_b = query_at_revision(uow, system_id=system.id, revision=sha_b)
    state_at_b = graph_b.entity("payments.state")
    assert state_at_b is not None
    assert not any(
        c.kind is IdentityClaimKind.DECLARATION_LINEAGE for c in state_at_b.identity_claims
    )

    with db.unit_of_work() as uow:
        # The old entity is not "present" in the current/at-B view (§13's
        # current-state filter excludes nothing here since it was never
        # superseded -- it is simply absent from B's source, same as any
        # entity with no candidate this run) -- but it still exists,
        # orphaned, exactly where the adversarial Git-rename tests already
        # proved this discipline holds.
        all_entities = uow.graph.find_entities(system.id, limit=10_000)
    status_entity = next(e for e in all_entities if e.qualified_name == "payments.status")
    assert status_entity.status is EntityStatus.ACTIVE  # orphaned, not superseded
    assert not any(
        c.kind is IdentityClaimKind.DECLARATION_LINEAGE for c in status_entity.identity_claims
    )
