"""R6 Step 4 Transition Test: C3 -> C4 File Move with Unchanged Symbols (§13, R6 Protocol).

Tests the incremental transition from C3 to C4:
* File move: `payments/discount.py` -> `payments/pricing/discount.py`
* Unchanged symbol inside moved file: `compute_discount`
* Import update in `payments/services.py`: `from .pricing.discount import compute_discount`
* Import/call relationships rewired to the new location
* No duplicate active entities in current graph
* Zero ID churn for unrelated entities (`PaymentService`, `Payment`, `state`, `gateway`, routes)
* Semantic equivalence against frozen C4 oracle manifest
* Multi-revision historical query preservation across C3, C2, C1, and C0
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
from hashira.core import EntityStatus, RelationshipType, System
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


def test_r6_step4_c3_to_c4_transition(repo: Path) -> None:
    """Validate Step 4 (C3 -> C4) transition: file move, unchanged symbols,
    relationship rewiring, absence of duplicate active entities, zero unrelated ID churn,
    and 4-stage historical preservation (C0..C3)."""
    revisions = _build_evolution_history(repo)
    c0_rev = revisions[0]
    c1_rev = revisions[1]
    c2_rev = revisions[2]
    c3_rev = revisions[3]
    c4_rev = revisions[4]

    db = MemoryDatabase()
    system = System(name="Checkout-Step4", slug="checkout-step4")
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

    # 4. Advance to C3
    _git(repo, "checkout", "-q", c3_rev)
    service.index(repo, system_id=system.id, revision=c3_rev)
    with db.unit_of_work() as uow:
        oracle_c3 = extract_oracle_manifest(
            uow, system_id=system.id, revision=c3_rev, stage="C3"
        )
        c3_graph = query_at_revision(uow, system_id=system.id, revision=c3_rev)
        c3_entity_ids = {e.qualified_name: e.id for e in c3_graph.entities}

    # 5. Advance to C4: File Move payments/discount.py -> payments/pricing/discount.py
    _git(repo, "checkout", "-q", c4_rev)
    res_c4 = service.index(repo, system_id=system.id, revision=c4_rev)
    assert res_c4.errors == []

    # Clean oracle for C4 ground truth comparison
    db_c4_clean = MemoryDatabase()
    system_c4_clean = System(name="Checkout-C4-Clean", slug="checkout-c4-clean")
    with db_c4_clean.unit_of_work() as uow_clean:
        uow_clean.systems.save(system_c4_clean)
        uow_clean.commit()
    service_clean = _make_service(db_c4_clean)
    service_clean.index(repo, system_id=system_c4_clean.id, revision=c4_rev)
    with db_c4_clean.unit_of_work() as uow_clean:
        oracle_c4 = extract_oracle_manifest(
            uow_clean, system_id=system_c4_clean.id, revision=c4_rev, stage="C4"
        )

    # =========================================================================
    # Step 4 Invariant Assertions
    # =========================================================================
    with db.unit_of_work() as uow:
        # A. Current state equivalence: Incremental(C4) == Full(C4)
        g4_incremental = query_at_revision(uow, system_id=system.id, revision=c4_rev)
        eq_c4 = compare_semantic_equivalence(g4_incremental, oracle_c4)
        assert eq_c4.equivalent, f"Incremental C4 != Oracle C4: {eq_c4.details}"

        g4_entities = {e.qualified_name: e for e in g4_incremental.entities}

        # B. File move assertions: new qualified name active, old path absent
        assert "payments.pricing.discount.compute_discount" in g4_entities
        assert "payments.discount.compute_discount" not in g4_entities
        discount_new = g4_entities["payments.pricing.discount.compute_discount"]
        assert discount_new.status is EntityStatus.ACTIVE

        # Verify call edge rewiring from PaymentService.process -> pricing.discount.compute_discount
        process_id = g4_entities["payments.services.PaymentService.process"].id
        calls_rels = uow.graph.get_relationships(
            process_id, direction="out", types=[RelationshipType.CALLS]
        )
        assert any(
            r.target_entity_id == discount_new.id and r.is_current
            for r in calls_rels
        ), "Call edge from PaymentService.process -> pricing.discount.compute_discount missing"

        # C. Absence of unrelated entity ID churn across C3 -> C4
        # Preexisting entities (other than the moved discount module/symbol) must retain exact IDs
        for qname, orig_id in c3_entity_ids.items():
            if "discount" in qname:
                continue
            assert qname in g4_entities, f"Preexisting entity {qname} missing in C4"
            assert g4_entities[qname].id == orig_id, (
                f"Entity {qname} ID churned across C3->C4: {orig_id} != {g4_entities[qname].id}"
            )

        assert g4_entities["payments.db_models.Payment"].id == (
            c3_entity_ids["payments.db_models.Payment"]
        )
        assert g4_entities["payments.state"].id == c3_entity_ids["payments.state"]
        assert g4_entities["payments.services.PaymentService"].id == (
            c3_entity_ids["payments.services.PaymentService"]
        )
        assert g4_entities["payments.gateway.PaymentGateway"].id == (
            c3_entity_ids["payments.gateway.PaymentGateway"]
        )

        # D. Historical Preservation: Replay of C3 after C4 indexing
        g3_historical = query_at_revision(uow, system_id=system.id, revision=c3_rev)
        eq_c3_hist = compare_semantic_equivalence(g3_historical, oracle_c3)
        assert eq_c3_hist.equivalent, (
            f"Historical C3 replay corrupted after C4 index: {eq_c3_hist.details}"
        )
        c3_hist_names = {e.qualified_name for e in g3_historical.entities}
        assert "payments.discount.compute_discount" in c3_hist_names
        assert "payments.pricing.discount.compute_discount" not in c3_hist_names

        # E. Historical Preservation: Replay of C2 after C4 indexing
        g2_historical = query_at_revision(uow, system_id=system.id, revision=c2_rev)
        eq_c2_hist = compare_semantic_equivalence(g2_historical, oracle_c2)
        assert eq_c2_hist.equivalent, (
            f"Historical C2 replay corrupted after C4 index: {eq_c2_hist.details}"
        )
        c2_hist_names = {e.qualified_name for e in g2_historical.entities}
        assert "payments.discount.compute_discount" in c2_hist_names
        assert "payments.gateway.PaymentGateway" not in c2_hist_names

        # F. Historical Preservation: Replay of C1 after C4 indexing
        g1_historical = query_at_revision(uow, system_id=system.id, revision=c1_rev)
        eq_c1_hist = compare_semantic_equivalence(g1_historical, oracle_c1)
        assert eq_c1_hist.equivalent, (
            f"Historical C1 replay corrupted after C4 index: {eq_c1_hist.details}"
        )
        c1_hist_names = {e.qualified_name for e in g1_historical.entities}
        assert "payments.status" in c1_hist_names
        assert "payments.state" not in c1_hist_names

        # G. Historical Preservation: Replay of C0 after C4 indexing
        g0_historical = query_at_revision(uow, system_id=system.id, revision=c0_rev)
        eq_c0_hist = compare_semantic_equivalence(g0_historical, oracle_c0)
        assert eq_c0_hist.equivalent, (
            f"Historical C0 replay corrupted after C4 index: {eq_c0_hist.details}"
        )
        c0_hist_names = {e.qualified_name for e in g0_historical.entities}
        assert "payments.discount.compute_discount" not in c0_hist_names
        assert "payments.pricing.discount.compute_discount" not in c0_hist_names
