"""R6 Step 3 Transition Test: C2 -> C3 Constructor Dependency & Topology Rewiring.

Tests the incremental transition from C2 to C3:
* New dependency introduction: `payments/gateway.py:PaymentGateway`
* Constructor dependency addition: `PaymentService.__init__`
* Field usage rewiring: `PaymentService.process` writes/reads `Payment.state`
* Relationship reconciliation: old edges closed at C3, preserved historically at C2/C1/C0
* Zero ID churn for unrelated entities (`PaymentService`, `process`, `Payment`, `Payment.state`)
* Semantic equivalence against frozen C3 oracle manifest
* Historical query preservation of C2, C1, and C0 states against frozen oracles
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from hashira.adapters._compose import compose_normalizers
from hashira.adapters.fastapi import FastAPIAdapter
from hashira.adapters.fastapi.normalizer import enrich_normalized_run as enrich_fastapi
from hashira.adapters.git import GitAdapter
from hashira.adapters.python import PythonAdapter
from hashira.adapters.sqlalchemy import SQLAlchemyAdapter
from hashira.adapters.sqlalchemy.normalizer import enrich_normalized_run as enrich_sqlalchemy
from hashira.application import IndexingService
from hashira.application.history import query_at_revision
from hashira.application.impact import reverse_impact
from hashira.core import EntityStatus, System
from hashira.storage.memory import MemoryDatabase
from tests.integration.r6_oracle import (
    compare_semantic_equivalence,
    extract_oracle_manifest,
)
from tests.integration.test_r6_evolution_baseline import (
    FIXTURE,
    _build_evolution_history,
    _git,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    shutil.copytree(FIXTURE, root)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    return root


def _make_service(db: MemoryDatabase) -> IndexingService:
    normalize = compose_normalizers(enrich_fastapi, enrich_sqlalchemy)
    return IndexingService(
        db.unit_of_work,
        [PythonAdapter()],
        normalize,
        framework_adapters=[FastAPIAdapter()],
        data_adapters=[SQLAlchemyAdapter()],
        history_adapters=[GitAdapter()],
    )


def test_r6_step3_c2_to_c3_transition(repo: Path) -> None:
    """Validate Step 3 (C2 -> C3) transition: constructor dependency introduction,
    call graph rewiring, relationship reconciliation, absence of unrelated ID churn,
    and multi-revision historical preservation across C0, C1, and C2."""
    revisions = _build_evolution_history(repo)
    c0_rev = revisions[0]
    c1_rev = revisions[1]
    c2_rev = revisions[2]
    c3_rev = revisions[3]

    db = MemoryDatabase()
    system = System(name="Checkout-Step3", slug="checkout-step3")
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()

    service = _make_service(db)

    # 1. Index C0
    _git(repo, "checkout", "-q", c0_rev)
    service.index(repo, system_id=system.id, revision=c0_rev)
    with db.unit_of_work() as uow:
        oracle_c0 = extract_oracle_manifest(
            uow, system_id=system.id, revision=c0_rev, stage="C0"
        )

    # 2. Advance to C1
    _git(repo, "checkout", "-q", c1_rev)
    service.index(repo, system_id=system.id, revision=c1_rev)
    with db.unit_of_work() as uow:
        oracle_c1 = extract_oracle_manifest(
            uow, system_id=system.id, revision=c1_rev, stage="C1"
        )

    # 3. Advance to C2
    _git(repo, "checkout", "-q", c2_rev)
    service.index(repo, system_id=system.id, revision=c2_rev)
    with db.unit_of_work() as uow:
        oracle_c2 = extract_oracle_manifest(
            uow, system_id=system.id, revision=c2_rev, stage="C2"
        )
        c2_graph = query_at_revision(uow, system_id=system.id, revision=c2_rev)
        c2_entity_ids = {e.qualified_name: e.id for e in c2_graph.entities}

    # 4. Advance to C3: Constructor dependency + state column usage catch-up
    _git(repo, "checkout", "-q", c3_rev)
    res_c3 = service.index(repo, system_id=system.id, revision=c3_rev)
    assert res_c3.errors == []

    # Clean oracle for C3 ground truth comparison
    db_c3_clean = MemoryDatabase()
    system_c3_clean = System(name="Checkout-C3-Clean", slug="checkout-c3-clean")
    with db_c3_clean.unit_of_work() as uow_clean:
        uow_clean.systems.save(system_c3_clean)
        uow_clean.commit()
    service_clean = _make_service(db_c3_clean)
    service_clean.index(repo, system_id=system_c3_clean.id, revision=c3_rev)
    with db_c3_clean.unit_of_work() as uow_clean:
        oracle_c3 = extract_oracle_manifest(
            uow_clean, system_id=system_c3_clean.id, revision=c3_rev, stage="C3"
        )

    # =========================================================================
    # Step 3 Invariant Assertions
    # =========================================================================
    with db.unit_of_work() as uow:
        # A. Current state equivalence: Incremental(C3) == Full(C3)
        g3_incremental = query_at_revision(uow, system_id=system.id, revision=c3_rev)
        eq_c3 = compare_semantic_equivalence(g3_incremental, oracle_c3)
        assert eq_c3.equivalent, f"Incremental C3 != Oracle C3: {eq_c3.details}"

        g3_entities = {e.qualified_name: e for e in g3_incremental.entities}

        # B. Verify new dependency entity and constructor entity
        assert "payments.gateway.PaymentGateway" in g3_entities
        assert "payments.services.PaymentService.__init__" in g3_entities
        gw_entity = g3_entities["payments.gateway.PaymentGateway"]
        assert gw_entity.status is EntityStatus.ACTIVE

        # C. Absence of unrelated entity ID churn across C2 -> C3
        # Existing entities in C2 MUST maintain their exact C2 EntityIDs
        for qname, orig_id in c2_entity_ids.items():
            assert qname in g3_entities, f"Preexisting entity {qname} missing in C3"
            assert g3_entities[qname].id == orig_id, (
                f"Entity {qname} ID churned across C2->C3: {orig_id} != {g3_entities[qname].id}"
            )

        # Specifically verify PaymentService, Payment, and Payment.state are rock-solid stable
        assert g3_entities["payments.services.PaymentService"].id == (
            c2_entity_ids["payments.services.PaymentService"]
        )
        assert g3_entities["payments.services.PaymentService.process"].id == (
            c2_entity_ids["payments.services.PaymentService.process"]
        )
        assert g3_entities["payments.db_models.Payment"].id == (
            c2_entity_ids["payments.db_models.Payment"]
        )
        assert g3_entities["payments.state"].id == c2_entity_ids["payments.state"]

        # D. Relationship reconciliation and Impact rewiring
        state_entity = g3_entities["payments.state"]
        impact_c3 = reverse_impact(
            uow, system_id=system.id, entity_id=state_entity.id, revision=c3_rev
        )
        affected_names = {e.qualified_name for e in impact_c3.affected_entities}
        assert "payments.services.PaymentService.process" in affected_names

        # E. Historical Preservation: Replay of C2 after C3 indexing
        g2_historical = query_at_revision(uow, system_id=system.id, revision=c2_rev)
        eq_c2_hist = compare_semantic_equivalence(g2_historical, oracle_c2)
        assert eq_c2_hist.equivalent, (
            f"Historical C2 replay corrupted after C3 index: {eq_c2_hist.details}"
        )
        c2_hist_names = {e.qualified_name for e in g2_historical.entities}
        assert "payments.gateway.PaymentGateway" not in c2_hist_names
        assert "payments.services.PaymentService.__init__" not in c2_hist_names

        # F. Historical Preservation: Replay of C1 after C3 indexing
        g1_historical = query_at_revision(uow, system_id=system.id, revision=c1_rev)
        eq_c1_hist = compare_semantic_equivalence(g1_historical, oracle_c1)
        assert eq_c1_hist.equivalent, (
            f"Historical C1 replay corrupted after C3 index: {eq_c1_hist.details}"
        )
        c1_hist_names = {e.qualified_name for e in g1_historical.entities}
        assert "payments.status" in c1_hist_names
        assert "payments.state" not in c1_hist_names
        assert "payments.gateway.PaymentGateway" not in c1_hist_names

        # G. Historical Preservation: Replay of C0 after C3 indexing
        g0_historical = query_at_revision(uow, system_id=system.id, revision=c0_rev)
        eq_c0_hist = compare_semantic_equivalence(g0_historical, oracle_c0)
        assert eq_c0_hist.equivalent, (
            f"Historical C0 replay corrupted after C3 index: {eq_c0_hist.details}"
        )
        c0_hist_names = {e.qualified_name for e in g0_historical.entities}
        assert "payments.discount.compute_discount" not in c0_hist_names
        assert "payments.gateway.PaymentGateway" not in c0_hist_names
