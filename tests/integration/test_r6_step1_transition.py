"""R6 Step 1 Transition Test: C0 -> C1 File & Symbol Addition (§13, R6 Protocol).

Tests the minimum incremental transition from C0 to C1:
* File addition (`payments/discount.py`)
* Symbol addition (`payments.discount.compute_discount`)
* Caller/callee edge addition (`PaymentService.process` -> `compute_discount`)
* Preservation of C0 entity identities
* Semantic equivalence against frozen C1 oracle manifest
* Historical query preservation of C0 state against frozen C0 oracle manifest
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
from hashira.core import System
from hashira.storage.memory import MemoryDatabase
from tests.integration.r6_oracle import (
    GraphOracleManifest,
    compare_semantic_equivalence,
)
from tests.integration.test_r6_evolution_baseline import (
    FIXTURE,
    _build_evolution_history,
    _git,
)

ORACLE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "r6_oracle_manifests"


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


def test_r6_step1_c0_to_c1_transition(repo: Path) -> None:
    """Validate Step 1 (C0 -> C1) transition against frozen C0 and C1 oracles."""
    revisions = _build_evolution_history(repo)
    c0_rev = revisions[0]
    c1_rev = revisions[1]

    # Load frozen oracles
    oracle_c0 = GraphOracleManifest.from_dict(
        eval((ORACLE_DIR / "oracle_c0.json").read_text())
    )
    oracle_c1 = GraphOracleManifest.from_dict(
        eval((ORACLE_DIR / "oracle_c1.json").read_text())
    )

    # 1. Start at C0: Initial full index
    _git(repo, "checkout", "-q", c0_rev)
    db = MemoryDatabase()
    system = System(name="Checkout-Step1", slug="checkout-step1")
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()

    service = _make_service(db)
    res_c0 = service.index(repo, system_id=system.id, revision=c0_rev)
    assert res_c0.errors == []

    with db.unit_of_work() as uow:
        g0_initial = query_at_revision(uow, system_id=system.id, revision=c0_rev)
        eq_c0_init = compare_semantic_equivalence(g0_initial, oracle_c0)
        assert eq_c0_init.equivalent, f"C0 initial failed: {eq_c0_init.details}"

        # Capture C0 entity IDs to assert identity preservation across the transition
        c0_entity_ids = {e.qualified_name: e.id for e in g0_initial.entities}

    # 2. Advance repository to C1: Incremental update C0 -> C1
    _git(repo, "checkout", "-q", c1_rev)
    res_c1 = service.index(repo, system_id=system.id, revision=c1_rev)
    assert res_c1.errors == []

    with db.unit_of_work() as uow:
        # A. Current state equivalence: incremental(C1) == full(C1)
        g1_incremental = query_at_revision(uow, system_id=system.id, revision=c1_rev)
        eq_c1 = compare_semantic_equivalence(g1_incremental, oracle_c1)
        assert eq_c1.equivalent, f"Incremental C1 != Oracle C1: {eq_c1.details}"

        # B. Preservation of existing entity IDs (no ID churn for untouched entities)
        g1_entities = {e.qualified_name: e for e in g1_incremental.entities}
        for qname, orig_id in c0_entity_ids.items():
            assert qname in g1_entities, f"Entity {qname} disappeared in C1"
            assert g1_entities[qname].id == orig_id, (
                f"Entity {qname} ID churned: {orig_id} != {g1_entities[qname].id}"
            )

        # C. Verification of new entity and caller/callee relationship
        assert "payments.discount.compute_discount" in g1_entities
        compute_discount_id = g1_entities["payments.discount.compute_discount"].id
        process_id = g1_entities["payments.services.PaymentService.process"].id

        call_rels = uow.graph.get_relationships(process_id, direction="out")
        assert any(
            r.target_entity_id == compute_discount_id and r.type.value == "CALLS"
            for r in call_rels
        ), "Call relationship from PaymentService.process -> compute_discount missing"

        # D. Historical query preservation: query(C0 after incremental C1) == oracle(C0)
        g0_historical = query_at_revision(uow, system_id=system.id, revision=c0_rev)
        eq_c0_hist = compare_semantic_equivalence(g0_historical, oracle_c0)
        assert eq_c0_hist.equivalent, (
            f"Historical C0 replay corrupted after C1 index: {eq_c0_hist.details}"
        )

        # In C0 historical replay, compute_discount must NOT be present
        c0_hist_names = {e.qualified_name for e in g0_historical.entities}
        assert "payments.discount.compute_discount" not in c0_hist_names
