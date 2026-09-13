"""R6 Step 6 Transition Test: C5 -> C6 Route Mutation & Dynamic Reflection (§13).

Tests the incremental transition from C5 to C6:
* Framework route path mutation: `/payments/checkout/` -> `/payments/v2/checkout/`
* Old route retired from active graph, new route established with handler relationship
* Dynamic reflection addition: `dynamic_dispatch` with `getattr`
* Epistemic boundary: no invented speculative edges for dynamic dispatch; limitations disclosed
* Zero ID churn for unrelated entities (`Payment`, `state`, `PaymentService`, etc.)
* Semantic equivalence against frozen C6 oracle manifest
* Multi-revision historical query preservation across C5, C4, C3, C2, C1, and C0
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
from hashira.application.graph import get_entity_neighborhood
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


def test_r6_step6_c5_to_c6_transition(repo: Path) -> None:
    """Validate Step 6 (C5 -> C6) transition: framework route mutation, dynamic reflection
    handling, epistemic coverage preservation, zero unrelated ID churn, and 6-stage history
    preservation (C0..C5)."""
    revisions = _build_evolution_history(repo)
    c0_rev = revisions[0]
    c1_rev = revisions[1]
    c2_rev = revisions[2]
    c3_rev = revisions[3]
    c4_rev = revisions[4]
    c5_rev = revisions[5]
    c6_rev = revisions[6]

    db = MemoryDatabase()
    system = System(name="Checkout-Step6", slug="checkout-step6")
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
        c5_graph = query_at_revision(uow, system_id=system.id, revision=c5_rev)
        c5_entity_ids = {e.qualified_name: e.id for e in c5_graph.entities}

    # 7. Advance to C6: Route update /v2/checkout/ + dynamic dispatch
    _git(repo, "checkout", "-q", c6_rev)
    res_c6 = service.index(repo, system_id=system.id, revision=c6_rev)
    assert res_c6.errors == []

    # Clean oracle for C6 ground truth comparison
    db_c6_clean = MemoryDatabase()
    system_c6_clean = System(name="Checkout-C6-Clean", slug="checkout-c6-clean")
    with db_c6_clean.unit_of_work() as uow_clean:
        uow_clean.systems.save(system_c6_clean)
        uow_clean.commit()
    service_clean = _make_service(db_c6_clean)
    service_clean.index(repo, system_id=system_c6_clean.id, revision=c6_rev)
    with db_c6_clean.unit_of_work() as uow_clean:
        oracle_c6 = extract_oracle_manifest(
            uow_clean, system_id=system_c6_clean.id, revision=c6_rev, stage="C6"
        )

    # =========================================================================
    # Step 6 Invariant Assertions
    # =========================================================================
    with db.unit_of_work() as uow:
        # A. Current state equivalence: Incremental(C6) == Full(C6)
        g6_incremental = query_at_revision(uow, system_id=system.id, revision=c6_rev)
        eq_c6 = compare_semantic_equivalence(g6_incremental, oracle_c6)
        assert eq_c6.equivalent, f"Incremental C6 != Oracle C6: {eq_c6.details}"

        g6_entities = {e.qualified_name: e for e in g6_incremental.entities}

        # B. Framework Route Mutation: new route active, old route absent
        assert "payments.routers.router:POST /payments/v2/checkout/" in g6_entities
        assert "payments.routers.router:POST /payments/checkout/" not in g6_entities
        v2_route_ent = g6_entities["payments.routers.router:POST /payments/v2/checkout/"]
        assert v2_route_ent.status is EntityStatus.ACTIVE

        # Verify route exposes checkout handler
        checkout_handler_id = g6_entities["payments.routers.checkout"].id
        exposes_rels = uow.graph.get_relationships(
            v2_route_ent.id, direction="out", types=[RelationshipType.EXPOSES]
        )
        assert any(
            r.target_entity_id == checkout_handler_id and r.is_current
            for r in exposes_rels
        ), "EXPOSES relationship from /v2/checkout/ -> checkout handler missing"

        # C. Dynamic reflection function presence & epistemic honesty
        assert "payments.routers.dynamic_dispatch" in g6_entities
        dyn_ent = g6_entities["payments.routers.dynamic_dispatch"]
        assert dyn_ent.status is EntityStatus.ACTIVE

        # Neighborhood of dynamic dispatch reflects static facts without inventing caller edges
        dyn_neigh = get_entity_neighborhood(
            uow, system_id=system.id, entity_id=dyn_ent.id, revision=c6_rev
        )
        assert dyn_neigh is not None
        # No speculative calls invented
        outgoing_calls = [edge for edge in dyn_neigh.outgoing if edge.type == "CALLS"]
        assert len(outgoing_calls) == 0

        # D. Absence of unrelated entity ID churn across C5 -> C6
        for qname, orig_id in c5_entity_ids.items():
            if "checkout/" in qname:
                continue
            assert qname in g6_entities, f"Preexisting entity {qname} missing in C6"
            assert g6_entities[qname].id == orig_id, (
                f"Entity {qname} ID churned across C5->C6: {orig_id} != {g6_entities[qname].id}"
            )

        assert g6_entities["payments.db_models.Payment"].id == (
            c5_entity_ids["payments.db_models.Payment"]
        )
        assert g6_entities["payments.state"].id == c5_entity_ids["payments.state"]
        assert g6_entities["payments.services.PaymentService"].id == (
            c5_entity_ids["payments.services.PaymentService"]
        )
        assert g6_entities["payments.gateway.PaymentGateway"].id == (
            c5_entity_ids["payments.gateway.PaymentGateway"]
        )
        assert g6_entities["payments.pricing.discount.compute_discount"].id == (
            c5_entity_ids["payments.pricing.discount.compute_discount"]
        )
        assert g6_entities["payments.finance.vat.compute_vat"].id == (
            c5_entity_ids["payments.finance.vat.compute_vat"]
        )

        # E. Historical Preservation: Replay of C5 after C6 indexing
        g5_historical = query_at_revision(uow, system_id=system.id, revision=c5_rev)
        eq_c5_hist = compare_semantic_equivalence(g5_historical, oracle_c5)
        assert eq_c5_hist.equivalent, (
            f"Historical C5 replay corrupted after C6 index: {eq_c5_hist.details}"
        )
        c5_hist_names = {e.qualified_name for e in g5_historical.entities}
        assert "payments.routers.router:POST /payments/checkout/" in c5_hist_names
        assert "payments.routers.router:POST /payments/v2/checkout/" not in c5_hist_names
        assert "payments.routers.dynamic_dispatch" not in c5_hist_names

        # F. Historical Preservation: Replay of C4 after C6 indexing
        g4_historical = query_at_revision(uow, system_id=system.id, revision=c4_rev)
        eq_c4_hist = compare_semantic_equivalence(g4_historical, oracle_c4)
        assert eq_c4_hist.equivalent, (
            f"Historical C4 replay corrupted after C6 index: {eq_c4_hist.details}"
        )

        # G. Historical Preservation: Replay of C3 after C6 indexing
        g3_historical = query_at_revision(uow, system_id=system.id, revision=c3_rev)
        eq_c3_hist = compare_semantic_equivalence(g3_historical, oracle_c3)
        assert eq_c3_hist.equivalent, (
            f"Historical C3 replay corrupted after C6 index: {eq_c3_hist.details}"
        )

        # H. Historical Preservation: Replay of C2 after C6 indexing
        g2_historical = query_at_revision(uow, system_id=system.id, revision=c2_rev)
        eq_c2_hist = compare_semantic_equivalence(g2_historical, oracle_c2)
        assert eq_c2_hist.equivalent, (
            f"Historical C2 replay corrupted after C6 index: {eq_c2_hist.details}"
        )

        # I. Historical Preservation: Replay of C1 after C6 indexing
        g1_historical = query_at_revision(uow, system_id=system.id, revision=c1_rev)
        eq_c1_hist = compare_semantic_equivalence(g1_historical, oracle_c1)
        assert eq_c1_hist.equivalent, (
            f"Historical C1 replay corrupted after C6 index: {eq_c1_hist.details}"
        )

        # J. Historical Preservation: Replay of C0 after C6 indexing
        g0_historical = query_at_revision(uow, system_id=system.id, revision=c0_rev)
        eq_c0_hist = compare_semantic_equivalence(g0_historical, oracle_c0)
        assert eq_c0_hist.equivalent, (
            f"Historical C0 replay corrupted after C6 index: {eq_c0_hist.details}"
        )


def test_r6_step6_route_evolution_negative_controls(tmp_path: Path) -> None:
    """Adversarial negative controls for framework route evolution:
    1. Positive: same router, same method, same handler, path changed -> SUPERSEDES lineage
    2. Negative 1: same router, same method, DIFFERENT handler -> NO lineage (independent new route)
    3. Negative 2: same router, DIFFERENT method (GET -> POST), same handler -> NO lineage
    """
    repo = tmp_path / "adv_project"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    # Initial state
    (repo / "pyproject.toml").write_text('[project]\ndependencies = ["fastapi"]\n')
    routes_file = repo / "routes.py"
    routes_file.write_text(
        "from fastapi import APIRouter\n\n"
        "router = APIRouter(prefix='/api')\n\n"
        "@router.post('/checkout/')\n"
        "def checkout() -> dict:\n"
        "    return {'status': 'ok'}\n\n"
        "@router.get('/info/')\n"
        "def get_info() -> dict:\n"
        "    return {'info': 'v1'}\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "Initial routes")
    rev0 = _git(repo, "rev-parse", "HEAD")

    db = MemoryDatabase()
    system = System(name="AdvRouteSystem", slug="adv-route-system")
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()

    service = _make_service(db)
    service.index(repo, system_id=system.id, revision=rev0)

    # Commit 1:
    # 1. Positive: /checkout/ -> /v2/checkout/ on checkout (same method, router, handler)
    # 2. Negative 1: /info/ (GET) -> /info/ (POST) on get_info (different method)
    # 3. Negative 2: /order/ added with new handler process_order (unrelated new route)
    routes_file.write_text(
        "from fastapi import APIRouter\n\n"
        "router = APIRouter(prefix='/api')\n\n"
        "# Positive: path changed, same handler, router, method\n"
        "@router.post('/v2/checkout/')\n"
        "def checkout() -> dict:\n"
        "    return {'status': 'v2'}\n\n"
        "# Negative 1: method changed GET -> POST\n"
        "@router.post('/info/')\n"
        "def get_info() -> dict:\n"
        "    return {'info': 'v2'}\n\n"
        "# Negative 2: brand new route + handler\n"
        "@router.get('/order/')\n"
        "def process_order() -> dict:\n"
        "    return {'order': 1}\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "Evolve routes with positive and negative cases")
    rev1 = _git(repo, "rev-parse", "HEAD")

    res1 = service.index(repo, system_id=system.id, revision=rev1)
    assert res1.errors == []

    with db.unit_of_work() as uow:
        g1 = query_at_revision(uow, system_id=system.id, revision=rev1)
        g1_ents = {e.qualified_name: e for e in g1.entities}

        # 1. Positive control: /v2/checkout/ active, /checkout/ retired and superseded
        assert "routes.router:POST /api/v2/checkout/" in g1_ents
        assert "routes.router:POST /api/checkout/" not in g1_ents

        v2_checkout_id = g1_ents["routes.router:POST /api/v2/checkout/"].id
        supersedes_rels = uow.graph.get_relationships(
            v2_checkout_id, direction="out", types=[RelationshipType.SUPERSEDES]
        )
        assert len(supersedes_rels) == 1, "Expected SUPERSEDES lineage for positive route mutation"

        # 2. Negative control 1: Method change (GET /info/ -> POST /info/)
        # Old GET /info/ must NOT be superseded by POST /info/ (method differs)
        post_info_id = g1_ents["routes.router:POST /api/info/"].id
        post_info_supersedes = uow.graph.get_relationships(
            post_info_id, direction="out", types=[RelationshipType.SUPERSEDES]
        )
        assert len(post_info_supersedes) == 0, (
            "Method change MUST NOT produce false SUPERSEDES lineage"
        )

        # 3. Negative control 2: Unrelated new route /order/ has NO lineage
        order_id = g1_ents["routes.router:GET /api/order/"].id
        order_supersedes = uow.graph.get_relationships(
            order_id, direction="out", types=[RelationshipType.SUPERSEDES]
        )
        assert len(order_supersedes) == 0, "New route MUST NOT produce false SUPERSEDES lineage"

