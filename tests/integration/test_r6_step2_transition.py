"""R6 Step 2 Transition Test: C1 -> C2 ORM Field Rename & Declaration Lineage.

Tests the incremental transition from C1 to C2:
* In-place column rename in `payments/db_models.py`: `Payment.status` -> `Payment.state`
* Generation of `DECLARATION_LINEAGE` identity claim
* Creation of `SUPERSEDES` relationship (`Payment.state` -> `Payment.status`) with `CERTAIN`
* Deactivation/superseding of `Payment.status` in active graph
* Zero ID churn for unrelated entities (`Payment`, `PaymentService`, `compute_discount`, routes)
* Semantic equivalence against frozen C2 oracle manifest
* Historical query preservation of C0 and C1 states against frozen C0/C1 oracles
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
from hashira.core import Confidence, EntityStatus, IdentityClaimKind, RelationshipType, System
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


def test_r6_step2_c1_to_c2_transition(repo: Path) -> None:
    """Validate Step 2 (C1 -> C2) transition: ORM field rename, lineage,
    absence of unrelated entity ID churn, and multi-revision historical preservation."""
    revisions = _build_evolution_history(repo)
    c0_rev = revisions[0]
    c1_rev = revisions[1]
    c2_rev = revisions[2]

    # 1. Clean Full Index at C0
    _git(repo, "checkout", "-q", c0_rev)
    db = MemoryDatabase()
    system = System(name="Checkout-Step2", slug="checkout-step2")
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()

    service = _make_service(db)
    service.index(repo, system_id=system.id, revision=c0_rev)

    with db.unit_of_work() as uow:
        oracle_c0 = extract_oracle_manifest(
            uow, system_id=system.id, revision=c0_rev, stage="C0"
        )

    # 2. Advance to C1 (Step 1 baseline)
    _git(repo, "checkout", "-q", c1_rev)
    service.index(repo, system_id=system.id, revision=c1_rev)

    with db.unit_of_work() as uow:
        oracle_c1 = extract_oracle_manifest(
            uow, system_id=system.id, revision=c1_rev, stage="C1"
        )
        c1_graph = query_at_revision(uow, system_id=system.id, revision=c1_rev)
        c1_entity_ids = {e.qualified_name: e.id for e in c1_graph.entities}

    # 3. Advance to C2: ORM field rename status -> state
    _git(repo, "checkout", "-q", c2_rev)
    res_c2 = service.index(repo, system_id=system.id, revision=c2_rev)
    assert res_c2.errors == []

    # Also extract fresh clean oracle for C2 in an isolated DB for exact semantic ground truth
    _git(repo, "checkout", "-q", c2_rev)
    db_c2_clean = MemoryDatabase()
    system_c2_clean = System(name="Checkout-C2-Clean", slug="checkout-c2-clean")
    with db_c2_clean.unit_of_work() as uow_clean:
        uow_clean.systems.save(system_c2_clean)
        uow_clean.commit()
    service_clean = _make_service(db_c2_clean)
    service_clean.index(repo, system_id=system_c2_clean.id, revision=c2_rev)
    with db_c2_clean.unit_of_work() as uow_clean:
        oracle_c2 = extract_oracle_manifest(
            uow_clean, system_id=system_c2_clean.id, revision=c2_rev, stage="C2"
        )

    # =========================================================================
    # Step 2 Verification Assertions on the Evolving Graph
    # =========================================================================
    with db.unit_of_work() as uow:
        # A. Current state equivalence: Incremental(C2) == Full(C2)
        g2_incremental = query_at_revision(uow, system_id=system.id, revision=c2_rev)
        eq_c2 = compare_semantic_equivalence(g2_incremental, oracle_c2)
        assert eq_c2.equivalent, f"Incremental C2 != Oracle C2: {eq_c2.details}"

        g2_entities = {e.qualified_name: e for e in g2_incremental.entities}

        # B. Field identity & lineage assertions
        assert "payments.state" in g2_entities
        assert "payments.status" not in g2_entities  # Deactivated in active graph

        state_entity = g2_entities["payments.state"]
        assert state_entity.status is EntityStatus.ACTIVE

        # Verify DECLARATION_LINEAGE claim
        assert any(
            claim.kind is IdentityClaimKind.DECLARATION_LINEAGE
            for claim in state_entity.identity_claims
        ), "DECLARATION_LINEAGE claim missing on Payment.state"

        # Verify SUPERSEDES relationship
        status_orig_id = c1_entity_ids["payments.status"]
        lineage_rels = uow.graph.get_relationships(
            state_entity.id, direction="out", types=[RelationshipType.SUPERSEDES]
        )
        assert len(lineage_rels) == 1, "Expected exactly 1 SUPERSEDES relationship"
        supersedes_rel = lineage_rels[0]
        assert supersedes_rel.target_entity_id == status_orig_id
        assert supersedes_rel.confidence is Confidence.CERTAIN
        assert supersedes_rel.valid_from_revision == c2_rev

        # C. Absence of unrelated entity ID churn
        # Entities present in C1 (other than payments.status) MUST retain their exact C1 EntityIDs
        for qname, orig_id in c1_entity_ids.items():
            if qname == "payments.status":
                continue
            assert qname in g2_entities, f"Unrelated entity {qname} missing in C2"
            assert g2_entities[qname].id == orig_id, (
                f"Unrelated entity {qname} ID churned: {orig_id} != {g2_entities[qname].id}"
            )

        # Specifically verify model, service, discount helper, and router IDs are stable
        assert g2_entities["payments.db_models.Payment"].id == (
            c1_entity_ids["payments.db_models.Payment"]
        )
        assert g2_entities["payments.services.PaymentService"].id == (
            c1_entity_ids["payments.services.PaymentService"]
        )
        assert g2_entities["payments.discount.compute_discount"].id == (
            c1_entity_ids["payments.discount.compute_discount"]
        )

        # D. Historical Preservation: Replay of C1 after C2 indexing
        g1_historical = query_at_revision(uow, system_id=system.id, revision=c1_rev)
        eq_c1_hist = compare_semantic_equivalence(g1_historical, oracle_c1)
        assert eq_c1_hist.equivalent, (
            f"Historical C1 replay corrupted after C2 index: {eq_c1_hist.details}"
        )
        c1_hist_names = {e.qualified_name for e in g1_historical.entities}
        assert "payments.status" in c1_hist_names
        assert "payments.state" not in c1_hist_names

        # E. Historical Preservation: Replay of C0 after C2 indexing
        g0_historical = query_at_revision(uow, system_id=system.id, revision=c0_rev)
        eq_c0_hist = compare_semantic_equivalence(g0_historical, oracle_c0)
        assert eq_c0_hist.equivalent, (
            f"Historical C0 replay corrupted after C2 index: {eq_c0_hist.details}"
        )
        c0_hist_names = {e.qualified_name for e in g0_historical.entities}
        assert "payments.status" in c0_hist_names
        assert "payments.state" not in c0_hist_names
        assert "payments.discount.compute_discount" not in c0_hist_names
