"""R6 Step 7 Transition Test: C6 -> C7 Pure File & Symbol Deletion (§13, R6 Protocol).

Tests the incremental transition from C6 to C7:
* Pure file & symbol deletion: `payments/finance/vat.py` deleted (`compute_vat` removed)
* Deleted symbol absent from active entities and relationships (no ghost edges)
* Zero speculative lineage: pure deletion produces no `SUPERSEDES` edge
* Zero ID churn for unrelated entities (`Payment`, `state`, `PaymentService`, `gateway`, etc.)
* Semantic equivalence against frozen C7 oracle manifest
* Agent-facing impact and neighborhood query comparison across current (C7) vs historical (C6)
* 7-stage multi-revision historical query preservation across C6, C5, C4, C3, C2, C1, and C0
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
from hashira.application.graph import get_entity_at_revision
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


def test_r6_step7_c6_to_c7_transition(repo: Path) -> None:
    """Validate Step 7 (C6 -> C7) transition: file & symbol deletion, absence of ghost
    entities/edges, zero unrelated ID churn, impact query distinction, and 7-stage history
    preservation (C0..C6)."""
    revisions = _build_evolution_history(repo)
    c0_rev = revisions[0]
    c1_rev = revisions[1]
    c2_rev = revisions[2]
    c3_rev = revisions[3]
    c4_rev = revisions[4]
    c5_rev = revisions[5]
    c6_rev = revisions[6]
    c7_rev = revisions[7]

    db = MemoryDatabase()
    system = System(name="Checkout-Step7", slug="checkout-step7")
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
        c6_graph = query_at_revision(uow, system_id=system.id, revision=c6_rev)
        c6_entity_ids = {e.qualified_name: e.id for e in c6_graph.entities}
        vat_c6_id = c6_entity_ids["payments.finance.vat.compute_vat"]

    # 8. Advance to C7: Pure File & Symbol Deletion (finance/vat.py deleted)
    _git(repo, "checkout", "-q", c7_rev)
    res_c7 = service.index(repo, system_id=system.id, revision=c7_rev)
    assert res_c7.errors == []

    # Clean oracle for C7 ground truth comparison
    db_c7_clean = MemoryDatabase()
    system_c7_clean = System(name="Checkout-C7-Clean", slug="checkout-c7-clean")
    with db_c7_clean.unit_of_work() as uow_clean:
        uow_clean.systems.save(system_c7_clean)
        uow_clean.commit()
    service_clean = _make_service(db_c7_clean)
    service_clean.index(repo, system_id=system_c7_clean.id, revision=c7_rev)
    with db_c7_clean.unit_of_work() as uow_clean:
        oracle_c7 = extract_oracle_manifest(
            uow_clean, system_id=system_c7_clean.id, revision=c7_rev, stage="C7"
        )

    # =========================================================================
    # Step 7 Invariant Assertions
    # =========================================================================
    with db.unit_of_work() as uow:
        # A. Current state equivalence: Incremental(C7) == Full(C7)
        g7_incremental = query_at_revision(uow, system_id=system.id, revision=c7_rev)
        eq_c7 = compare_semantic_equivalence(g7_incremental, oracle_c7)
        assert eq_c7.equivalent, f"Incremental C7 != Oracle C7: {eq_c7.details}"

        g7_entities = {e.qualified_name: e for e in g7_incremental.entities}

        # B. Deleted entity and relationship absence in active graph
        assert "payments.finance.vat.compute_vat" not in g7_entities
        active_vat = get_entity_at_revision(
            uow, system_id=system.id, entity_id=vat_c6_id, revision=c7_rev
        )
        assert active_vat is None, "Deleted entity must not be active at C7"

        # Ensure no ghost relationships exist touching the deleted entity in C7
        c7_active_rel_endpoints = {
            r.source_entity_id for r in g7_incremental.relationships
        } | {r.target_entity_id for r in g7_incremental.relationships}
        assert vat_c6_id not in c7_active_rel_endpoints, (
            "Ghost relationship found connected to deleted entity at C7"
        )

        # C. Zero speculative lineage: pure deletion produces NO SUPERSEDES relationship
        supersedes_for_vat = uow.graph.get_relationships(
            vat_c6_id, direction="both", types=[RelationshipType.SUPERSEDES]
        )
        # Note: vat superseded calc_tax from C4->C5 (incoming), but must have no successor
        outgoing_supersedes = [
            r for r in supersedes_for_vat if r.source_entity_id == vat_c6_id
        ]
        assert len(outgoing_supersedes) == 0, (
            "Pure deletion MUST NOT manufacture a successor SUPERSEDES relationship"
        )

        # D. Absence of unrelated entity ID churn across C6 -> C7
        for qname, orig_id in c6_entity_ids.items():
            if "vat" in qname:
                continue
            assert qname in g7_entities, f"Preexisting entity {qname} missing in C7"
            assert g7_entities[qname].id == orig_id, (
                f"Entity {qname} ID churned across C6->C7: {orig_id} != {g7_entities[qname].id}"
            )

        assert g7_entities["payments.db_models.Payment"].id == (
            c6_entity_ids["payments.db_models.Payment"]
        )
        assert g7_entities["payments.state"].id == c6_entity_ids["payments.state"]
        assert g7_entities["payments.services.PaymentService"].id == (
            c6_entity_ids["payments.services.PaymentService"]
        )
        assert g7_entities["payments.gateway.PaymentGateway"].id == (
            c6_entity_ids["payments.gateway.PaymentGateway"]
        )
        assert g7_entities["payments.pricing.discount.compute_discount"].id == (
            c6_entity_ids["payments.pricing.discount.compute_discount"]
        )
        assert g7_entities["payments.routers.router:POST /payments/v2/checkout/"].id == (
            c6_entity_ids["payments.routers.router:POST /payments/v2/checkout/"]
        )
        assert g7_entities["payments.routers.dynamic_dispatch"].id == (
            c6_entity_ids["payments.routers.dynamic_dispatch"]
        )

        # E. Agent-facing query comparison: Current (C7) vs Historical (C6)
        # At C6: compute_vat is present and queryable
        vat_at_c6 = get_entity_at_revision(
            uow, system_id=system.id, entity_id=vat_c6_id, revision=c6_rev
        )
        assert vat_at_c6 is not None
        assert vat_at_c6.id == vat_c6_id

        # Impact query on discount at C7 vs C6
        disc_id = g7_entities["payments.pricing.discount.compute_discount"].id
        disc_imp_c7 = reverse_impact(
            uow, system_id=system.id, entity_id=disc_id, revision=c7_rev
        )
        disc_summary_c7 = summarize_impact(disc_imp_c7)
        assert disc_summary_c7 is not None
        # Impact summary at C7 contains valid surviving entities, no deleted vat
        impact_c7_ids = {
            item.entity.id
            for cat in (
                disc_summary_c7.direct_callers,
                disc_summary_c7.direct_callees,
                disc_summary_c7.readers,
                disc_summary_c7.writers,
                disc_summary_c7.framework_boundaries,
                disc_summary_c7.indirect_dependencies,
            )
            for item in cat
        }
        assert vat_c6_id not in impact_c7_ids

        # Querying reverse impact on deleted entity at C7 raises KeyError (not present),
        # but at C6 it succeeds and returns the historical entity
        with pytest.raises(KeyError):
            reverse_impact(uow, system_id=system.id, entity_id=vat_c6_id, revision=c7_rev)

        vat_imp_c6 = reverse_impact(
            uow, system_id=system.id, entity_id=vat_c6_id, revision=c6_rev
        )
        assert vat_imp_c6.start.id == vat_c6_id

        # F. Historical Preservation: Replay of C6 after C7 indexing
        g6_historical = query_at_revision(uow, system_id=system.id, revision=c6_rev)
        eq_c6_hist = compare_semantic_equivalence(g6_historical, oracle_c6)
        assert eq_c6_hist.equivalent, (
            f"Historical C6 replay corrupted after C7 index: {eq_c6_hist.details}"
        )
        c6_hist_names = {e.qualified_name for e in g6_historical.entities}
        assert "payments.finance.vat.compute_vat" in c6_hist_names
        assert "payments.routers.router:POST /payments/v2/checkout/" in c6_hist_names

        # G. Historical Preservation: Replay of C5 after C7 indexing
        g5_historical = query_at_revision(uow, system_id=system.id, revision=c5_rev)
        eq_c5_hist = compare_semantic_equivalence(g5_historical, oracle_c5)
        assert eq_c5_hist.equivalent, (
            f"Historical C5 replay corrupted after C7 index: {eq_c5_hist.details}"
        )

        # H. Historical Preservation: Replay of C4 after C7 indexing
        g4_historical = query_at_revision(uow, system_id=system.id, revision=c4_rev)
        eq_c4_hist = compare_semantic_equivalence(g4_historical, oracle_c4)
        assert eq_c4_hist.equivalent, (
            f"Historical C4 replay corrupted after C7 index: {eq_c4_hist.details}"
        )

        # I. Historical Preservation: Replay of C3 after C7 indexing
        g3_historical = query_at_revision(uow, system_id=system.id, revision=c3_rev)
        eq_c3_hist = compare_semantic_equivalence(g3_historical, oracle_c3)
        assert eq_c3_hist.equivalent, (
            f"Historical C3 replay corrupted after C7 index: {eq_c3_hist.details}"
        )

        # J. Historical Preservation: Replay of C2 after C7 indexing
        g2_historical = query_at_revision(uow, system_id=system.id, revision=c2_rev)
        eq_c2_hist = compare_semantic_equivalence(g2_historical, oracle_c2)
        assert eq_c2_hist.equivalent, (
            f"Historical C2 replay corrupted after C7 index: {eq_c2_hist.details}"
        )

        # K. Historical Preservation: Replay of C1 after C7 indexing
        g1_historical = query_at_revision(uow, system_id=system.id, revision=c1_rev)
        eq_c1_hist = compare_semantic_equivalence(g1_historical, oracle_c1)
        assert eq_c1_hist.equivalent, (
            f"Historical C1 replay corrupted after C7 index: {eq_c1_hist.details}"
        )

        # L. Historical Preservation: Replay of C0 after C7 indexing
        g0_historical = query_at_revision(uow, system_id=system.id, revision=c0_rev)
        eq_c0_hist = compare_semantic_equivalence(g0_historical, oracle_c0)
        assert eq_c0_hist.equivalent, (
            f"Historical C0 replay corrupted after C7 index: {eq_c0_hist.details}"
        )
