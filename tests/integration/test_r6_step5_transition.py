"""R6 Step 5 Transition Test: C4 -> C5 Simultaneous File + Symbol Rename.

Tests the incremental transition from C4 to C5:
* Combined move + rename: `tax.py:calc_tax` -> `finance/vat.py:compute_vat`
* Active graph contains exactly one active `compute_vat` entity and zero active `calc_tax`
* Caller/callee relationships correctly resolve to new location and symbol
* Zero ID churn for unrelated entities (`Payment`, `state`, `PaymentService`, `gateway`, `discount`)
* Semantic equivalence against frozen C5 oracle manifest
* Multi-revision historical query preservation across C4, C3, C2, C1, and C0
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


def test_r6_step5_c4_to_c5_transition(repo: Path) -> None:
    """Validate Step 5 (C4 -> C5) transition: simultaneous file + symbol rename,
    identity separation, absence of unrelated ID churn, and 5-stage history preservation."""
    revisions = _build_evolution_history(repo)
    c0_rev = revisions[0]
    c1_rev = revisions[1]
    c2_rev = revisions[2]
    c3_rev = revisions[3]
    c4_rev = revisions[4]
    c5_rev = revisions[5]

    db = MemoryDatabase()
    system = System(name="Checkout-Step5", slug="checkout-step5")
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

    # 5. Advance to C4
    _git(repo, "checkout", "-q", c4_rev)
    service.index(repo, system_id=system.id, revision=c4_rev)
    with db.unit_of_work() as uow:
        oracle_c4 = extract_oracle_manifest(
            uow, system_id=system.id, revision=c4_rev, stage="C4"
        )
        c4_graph = query_at_revision(uow, system_id=system.id, revision=c4_rev)
        c4_entity_ids = {e.qualified_name: e.id for e in c4_graph.entities}

    # 6. Advance to C5: Move and rename tax.py:calc_tax -> finance/vat.py:compute_vat
    _git(repo, "checkout", "-q", c5_rev)
    res_c5 = service.index(repo, system_id=system.id, revision=c5_rev)
    assert res_c5.errors == []

    # Clean oracle for C5 ground truth comparison
    db_c5_clean = MemoryDatabase()
    system_c5_clean = System(name="Checkout-C5-Clean", slug="checkout-c5-clean")
    with db_c5_clean.unit_of_work() as uow_clean:
        uow_clean.systems.save(system_c5_clean)
        uow_clean.commit()
    service_clean = _make_service(db_c5_clean)
    service_clean.index(repo, system_id=system_c5_clean.id, revision=c5_rev)
    with db_c5_clean.unit_of_work() as uow_clean:
        oracle_c5 = extract_oracle_manifest(
            uow_clean, system_id=system_c5_clean.id, revision=c5_rev, stage="C5"
        )

    # =========================================================================
    # Step 5 Invariant Assertions
    # =========================================================================
    with db.unit_of_work() as uow:
        # A. Current state equivalence: Incremental(C5) == Full(C5)
        g5_incremental = query_at_revision(uow, system_id=system.id, revision=c5_rev)
        eq_c5 = compare_semantic_equivalence(g5_incremental, oracle_c5)
        assert eq_c5.equivalent, f"Incremental C5 != Oracle C5: {eq_c5.details}"

        g5_entities = {e.qualified_name: e for e in g5_incremental.entities}

        # B. Combined move & rename entity presence
        assert "payments.finance.vat.compute_vat" in g5_entities
        assert "payments.tax.calc_tax" not in g5_entities
        vat_entity = g5_entities["payments.finance.vat.compute_vat"]
        assert vat_entity.status is EntityStatus.ACTIVE

        # C. Absence of unrelated entity ID churn across C4 -> C5
        for qname, orig_id in c4_entity_ids.items():
            if "tax" in qname or "vat" in qname:
                continue
            assert qname in g5_entities, f"Preexisting entity {qname} missing in C5"
            assert g5_entities[qname].id == orig_id, (
                f"Entity {qname} ID churned across C4->C5: {orig_id} != {g5_entities[qname].id}"
            )

        assert g5_entities["payments.db_models.Payment"].id == (
            c4_entity_ids["payments.db_models.Payment"]
        )
        assert g5_entities["payments.state"].id == c4_entity_ids["payments.state"]
        assert g5_entities["payments.services.PaymentService"].id == (
            c4_entity_ids["payments.services.PaymentService"]
        )
        assert g5_entities["payments.gateway.PaymentGateway"].id == (
            c4_entity_ids["payments.gateway.PaymentGateway"]
        )
        assert g5_entities["payments.pricing.discount.compute_discount"].id == (
            c4_entity_ids["payments.pricing.discount.compute_discount"]
        )

        # D. Historical Preservation: Replay of C4 after C5 indexing
        g4_historical = query_at_revision(uow, system_id=system.id, revision=c4_rev)
        eq_c4_hist = compare_semantic_equivalence(g4_historical, oracle_c4)
        assert eq_c4_hist.equivalent, (
            f"Historical C4 replay corrupted after C5 index: {eq_c4_hist.details}"
        )
        c4_hist_names = {e.qualified_name for e in g4_historical.entities}
        assert "payments.finance.vat.compute_vat" not in c4_hist_names

        # E. Historical Preservation: Replay of C3 after C5 indexing
        g3_historical = query_at_revision(uow, system_id=system.id, revision=c3_rev)
        eq_c3_hist = compare_semantic_equivalence(g3_historical, oracle_c3)
        assert eq_c3_hist.equivalent, (
            f"Historical C3 replay corrupted after C5 index: {eq_c3_hist.details}"
        )

        # F. Historical Preservation: Replay of C2 after C5 indexing
        g2_historical = query_at_revision(uow, system_id=system.id, revision=c2_rev)
        eq_c2_hist = compare_semantic_equivalence(g2_historical, oracle_c2)
        assert eq_c2_hist.equivalent, (
            f"Historical C2 replay corrupted after C5 index: {eq_c2_hist.details}"
        )

        # G. Historical Preservation: Replay of C1 after C5 indexing
        g1_historical = query_at_revision(uow, system_id=system.id, revision=c1_rev)
        eq_c1_hist = compare_semantic_equivalence(g1_historical, oracle_c1)
        assert eq_c1_hist.equivalent, (
            f"Historical C1 replay corrupted after C5 index: {eq_c1_hist.details}"
        )
        c1_hist_names = {e.qualified_name for e in g1_historical.entities}
        assert "payments.status" in c1_hist_names
        assert "payments.state" not in c1_hist_names

        # H. Historical Preservation: Replay of C0 after C5 indexing
        g0_historical = query_at_revision(uow, system_id=system.id, revision=c0_rev)
        eq_c0_hist = compare_semantic_equivalence(g0_historical, oracle_c0)
        assert eq_c0_hist.equivalent, (
            f"Historical C0 replay corrupted after C5 index: {eq_c0_hist.details}"
        )
        c0_hist_names = {e.qualified_name for e in g0_historical.entities}
        assert "payments.discount.compute_discount" not in c0_hist_names
        assert "payments.finance.vat.compute_vat" not in c0_hist_names
