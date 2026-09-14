"""R6 Step 8 Transition Test: C7 -> C8 Semantic No-Op Negative Control (§13, R6 Protocol).

Tests the incremental transition from C7 to C8:
* Negative control: comments / docstrings only change in `payments/services.py`
* Zero semantic graph drift: C7 and C8 graphs must be semantically identical
* Zero entity identity churn, zero relationship churn, zero lineage churn
* Incremental C8 matches frozen full-index C8 oracle manifest
* Historical query preservation across all 8 previous stages (C0..C7)
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
from hashira.application.impact import reverse_impact, summarize_impact
from hashira.core import RelationshipType, System
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


def test_r6_step8_c7_to_c8_transition(repo: Path) -> None:
    """Validate Step 8 (C7 -> C8) negative control transition: docstring/comment change
    produces zero semantic graph drift, exact C8 oracle match, zero ID churn,
    and 8-stage history preservation (C0..C7)."""
    revisions = _build_evolution_history(repo)
    c0_rev = revisions[0]
    c1_rev = revisions[1]
    c2_rev = revisions[2]
    c3_rev = revisions[3]
    c4_rev = revisions[4]
    c5_rev = revisions[5]
    c6_rev = revisions[6]
    c7_rev = revisions[7]
    c8_rev = revisions[8]

    assert c7_rev != c8_rev

    db = MemoryDatabase()
    system = System(name="Checkout-Step8", slug="checkout-step8")
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

    # 6. Advance to C5
    _git(repo, "checkout", "-q", c5_rev)
    service.index(repo, system_id=system.id, revision=c5_rev)
    with db.unit_of_work() as uow:
        oracle_c5 = extract_oracle_manifest(
            uow, system_id=system.id, revision=c5_rev, stage="C5"
        )

    # 7. Advance to C6
    _git(repo, "checkout", "-q", c6_rev)
    service.index(repo, system_id=system.id, revision=c6_rev)
    with db.unit_of_work() as uow:
        oracle_c6 = extract_oracle_manifest(
            uow, system_id=system.id, revision=c6_rev, stage="C6"
        )

    # 8. Advance to C7
    _git(repo, "checkout", "-q", c7_rev)
    service.index(repo, system_id=system.id, revision=c7_rev)
    with db.unit_of_work() as uow:
        oracle_c7 = extract_oracle_manifest(
            uow, system_id=system.id, revision=c7_rev, stage="C7"
        )
        g7_pre = query_at_revision(uow, system_id=system.id, revision=c7_rev)
        c7_entity_ids = {e.qualified_name: e.id for e in g7_pre.entities}

    # 9. Advance to C8: Comments / Docstrings Only (Negative Control)
    _git(repo, "checkout", "-q", c8_rev)
    res_c8 = service.index(repo, system_id=system.id, revision=c8_rev)
    assert res_c8.errors == []

    # Clean oracle for C8 ground truth comparison
    db_c8_clean = MemoryDatabase()
    system_c8_clean = System(name="Checkout-C8-Clean", slug="checkout-c8-clean")
    with db_c8_clean.unit_of_work() as uow_clean:
        uow_clean.systems.save(system_c8_clean)
        uow_clean.commit()
    service_clean = _make_service(db_c8_clean)
    service_clean.index(repo, system_id=system_c8_clean.id, revision=c8_rev)
    with db_c8_clean.unit_of_work() as uow_clean:
        oracle_c8 = extract_oracle_manifest(
            uow_clean, system_id=system_c8_clean.id, revision=c8_rev, stage="C8"
        )

    # =========================================================================
    # Step 8 Invariant Assertions
    # =========================================================================
    with db.unit_of_work() as uow:
        # A. Current state equivalence: Incremental(C8) == Full(C8)
        g8_incremental = query_at_revision(uow, system_id=system.id, revision=c8_rev)
        eq_c8 = compare_semantic_equivalence(g8_incremental, oracle_c8)
        assert eq_c8.equivalent, f"Incremental C8 != Oracle C8: {eq_c8.details}"

        # B. Negative Control: C7 and C8 must be semantically identical
        g7_post = query_at_revision(uow, system_id=system.id, revision=c7_rev)
        eq_c7_c8 = compare_semantic_equivalence(g8_incremental, oracle_c7)
        assert eq_c7_c8.equivalent, (
            f"Negative control violated: C8 differs semantically from C7: {eq_c7_c8.details}"
        )

        g8_entities = {e.qualified_name: e for e in g8_incremental.entities}
        g7_entities = {e.qualified_name: e for e in g7_post.entities}

        # Exact match of entity qualified names and counts
        assert set(g8_entities.keys()) == set(g7_entities.keys())
        assert len(g8_entities) == len(g7_entities)

        # C. Zero ID churn: every single entity in C8 retains its exact C7 ID
        for qname, orig_id in c7_entity_ids.items():
            assert qname in g8_entities, f"Entity {qname} missing in C8"
            assert g8_entities[qname].id == orig_id, (
                f"Entity {qname} ID churned across C7->C8: {orig_id} != {g8_entities[qname].id}"
            )

        # Specifically check core entities
        assert g8_entities["payments.db_models.Payment"].id == (
            c7_entity_ids["payments.db_models.Payment"]
        )
        assert g8_entities["payments.state"].id == c7_entity_ids["payments.state"]
        assert g8_entities["payments.services.PaymentService"].id == (
            c7_entity_ids["payments.services.PaymentService"]
        )
        assert g8_entities["payments.services.PaymentService.process"].id == (
            c7_entity_ids["payments.services.PaymentService.process"]
        )
        assert g8_entities["payments.gateway.PaymentGateway"].id == (
            c7_entity_ids["payments.gateway.PaymentGateway"]
        )
        assert g8_entities["payments.pricing.discount.compute_discount"].id == (
            c7_entity_ids["payments.pricing.discount.compute_discount"]
        )
        assert g8_entities["payments.routers.router:POST /payments/v2/checkout/"].id == (
            c7_entity_ids["payments.routers.router:POST /payments/v2/checkout/"]
        )
        assert g8_entities["payments.routers.dynamic_dispatch"].id == (
            c7_entity_ids["payments.routers.dynamic_dispatch"]
        )

        # D. Zero Lineage / SUPERSEDES churn created by comment change
        service_entity_id = g8_entities["payments.services.PaymentService"].id
        supersedes_for_service = uow.graph.get_relationships(
            service_entity_id, direction="both", types=[RelationshipType.SUPERSEDES]
        )
        assert len(supersedes_for_service) == 0, (
            "Comment changes MUST NOT manufacture SUPERSEDES relationships"
        )

        # E. Agent-facing queries: Impact and Neighborhood at C8 identical to C7
        svc_imp_c8 = reverse_impact(
            uow, system_id=system.id, entity_id=service_entity_id, revision=c8_rev
        )
        svc_imp_c7 = reverse_impact(
            uow, system_id=system.id, entity_id=service_entity_id, revision=c7_rev
        )
        summary_c8 = summarize_impact(svc_imp_c8)
        summary_c7 = summarize_impact(svc_imp_c7)

        c8_caller_ids = {item.entity.id for item in summary_c8.direct_callers}
        c7_caller_ids = {item.entity.id for item in summary_c7.direct_callers}
        assert c8_caller_ids == c7_caller_ids

        c8_callee_ids = {item.entity.id for item in summary_c8.direct_callees}
        c7_callee_ids = {item.entity.id for item in summary_c7.direct_callees}
        assert c8_callee_ids == c7_callee_ids

        # F. Multi-stage Historical Preservation across all 8 previous stages (C0..C7)
        # Replay C7
        eq_c7_hist = compare_semantic_equivalence(g7_post, oracle_c7)
        assert eq_c7_hist.equivalent, (
            f"Historical C7 replay corrupted after C8 index: {eq_c7_hist.details}"
        )

        # Replay C6
        g6_historical = query_at_revision(uow, system_id=system.id, revision=c6_rev)
        eq_c6_hist = compare_semantic_equivalence(g6_historical, oracle_c6)
        assert eq_c6_hist.equivalent, (
            f"Historical C6 replay corrupted after C8 index: {eq_c6_hist.details}"
        )
        c6_hist_names = {e.qualified_name for e in g6_historical.entities}
        assert "payments.finance.vat.compute_vat" in c6_hist_names

        # Replay C5
        g5_historical = query_at_revision(uow, system_id=system.id, revision=c5_rev)
        eq_c5_hist = compare_semantic_equivalence(g5_historical, oracle_c5)
        assert eq_c5_hist.equivalent, (
            f"Historical C5 replay corrupted after C8 index: {eq_c5_hist.details}"
        )

        # Replay C4
        g4_historical = query_at_revision(uow, system_id=system.id, revision=c4_rev)
        eq_c4_hist = compare_semantic_equivalence(g4_historical, oracle_c4)
        assert eq_c4_hist.equivalent, (
            f"Historical C4 replay corrupted after C8 index: {eq_c4_hist.details}"
        )

        # Replay C3
        g3_historical = query_at_revision(uow, system_id=system.id, revision=c3_rev)
        eq_c3_hist = compare_semantic_equivalence(g3_historical, oracle_c3)
        assert eq_c3_hist.equivalent, (
            f"Historical C3 replay corrupted after C8 index: {eq_c3_hist.details}"
        )

        # Replay C2
        g2_historical = query_at_revision(uow, system_id=system.id, revision=c2_rev)
        eq_c2_hist = compare_semantic_equivalence(g2_historical, oracle_c2)
        assert eq_c2_hist.equivalent, (
            f"Historical C2 replay corrupted after C8 index: {eq_c2_hist.details}"
        )

        # Replay C1
        g1_historical = query_at_revision(uow, system_id=system.id, revision=c1_rev)
        eq_c1_hist = compare_semantic_equivalence(g1_historical, oracle_c1)
        assert eq_c1_hist.equivalent, (
            f"Historical C1 replay corrupted after C8 index: {eq_c1_hist.details}"
        )

        # Replay C0
        g0_historical = query_at_revision(uow, system_id=system.id, revision=c0_rev)
        eq_c0_hist = compare_semantic_equivalence(g0_historical, oracle_c0)
        assert eq_c0_hist.equivalent, (
            f"Historical C0 replay corrupted after C8 index: {eq_c0_hist.details}"
        )
