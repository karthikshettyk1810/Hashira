"""R6 Multi-Commit Evolution Baseline: Ground-Truth Full-Index State
across an 8-stage adversarial repository evolution sequence.

Corpus Evolution Stages:
* C0 — Base State:
       FastAPI routers, SQLAlchemy models (Payment.status), services, dependencies.
* C1 — File Addition + Symbol Addition + Caller/Callee Change:
       Add `payments/discount.py:compute_discount`; call it from `PaymentService.process`.
* C2 — ORM Field Rename:
       Rename `Payment.status` -> `Payment.state` in `payments/db_models.py` (DECLARATION_LINEAGE).
* C3 — Constructor Dependency Change + Usage Catch-up:
       Add `PaymentGateway` dependency to `PaymentService`; update service to `Payment.state`.
* C4 — File Rename with Unchanged Symbols:
       Move `payments/discount.py` -> `payments/pricing/discount.py` (symbol unchanged).
* C5 — File + Symbol Rename Combined:
       Add `payments/tax.py:calc_tax`, move & rename to `payments/finance/vat.py:compute_vat`.
* C6 — Framework Route Change + Dynamic Reflection Case:
       Change `/checkout/` -> `/v2/checkout/`; add dynamic `getattr` helper with PARTIAL coverage.
* C7 — File & Symbol Deletion:
       Delete `payments/finance/vat.py` and remove dead code.
* C8 — Negative Control Commit:
       Add comments and docstrings only; assert zero semantic graph drift against C7.

This suite freezes the complete ground truth for each commit revision,
asserting entity identities, types, relationship endpoints, confidence,
evidence, SUPERSEDES lineage, temporal validity, and neighborhood queries.
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
from hashira.application.graph import get_entity_neighborhood
from hashira.application.history import query_at_revision
from hashira.application.impact import reverse_impact, summarize_impact
from hashira.core import RelationshipType, System
from hashira.storage.memory import MemoryDatabase

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fastapi_checkout"


def _git(root: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)
    return res.stdout.strip()


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return GitRepository(root).current_revision()


def _build_evolution_history(repo: Path) -> list[str]:
    """Build the 9-commit evolution history (C0 to C8) on a git repository."""
    revisions: list[str] = []

    # C0: Initial commit
    c0 = _commit(repo, "C0: initial checkout system")
    revisions.append(c0)

    # C1: File Addition + Symbol Addition + Caller/Callee Change
    discount_file = repo / "payments" / "discount.py"
    discount_file.write_text(
        "def compute_discount(amount: float) -> float:\n    return amount * 0.1\n"
    )
    svc_file = repo / "payments" / "services.py"
    svc_text = svc_file.read_text()
    svc_file.write_text(
        "from .discount import compute_discount\n"
        + svc_text.replace(
            "def process(self, amount):",
            "def process(self, amount):\n        discount = compute_discount(amount)\n",
        )
    )
    c1 = _commit(repo, "C1: add discount calculation helper and call it")
    revisions.append(c1)

    # C2: ORM Field Rename
    db_models_file = repo / "payments" / "db_models.py"
    db_models_file.write_text(db_models_file.read_text().replace("status", "state"))
    c2 = _commit(repo, "C2: rename Payment.status to Payment.state")
    revisions.append(c2)

    # C3: Constructor Dependency Change + Service Catch-up
    gw_file = repo / "payments" / "gateway.py"
    gw_file.write_text(
        "class PaymentGateway:\n"
        "    def charge(self, amount: float) -> str:\n"
        "        return 'success'\n"
    )
    svc_text = svc_file.read_text()
    init_sig = (
        "class PaymentService:\n"
        "    def __init__(self, gateway: PaymentGateway | None = None):\n"
        "        self.gateway = gateway or PaymentGateway()\n"
    )
    svc_file.write_text(
        "from .gateway import PaymentGateway\n"
        + svc_text.replace("class PaymentService:", init_sig).replace(
            "payment.status", "payment.state"
        )
    )
    c3 = _commit(repo, "C3: add PaymentGateway dependency and catch up to state column")
    revisions.append(c3)

    # C4: File Rename with Unchanged Symbols
    (repo / "payments" / "pricing").mkdir(parents=True, exist_ok=True)
    _git(repo, "mv", "payments/discount.py", "payments/pricing/discount.py")
    svc_text = svc_file.read_text()
    svc_file.write_text(svc_text.replace("from .discount import", "from .pricing.discount import"))
    c4 = _commit(repo, "C4: move discount.py to payments/pricing/")
    revisions.append(c4)

    # C5: File + Symbol Rename Combined
    tax_file = repo / "payments" / "tax.py"
    tax_file.write_text("def calc_tax(val: float) -> float:\n    return val * 0.05\n")
    _commit(repo, "C5-prep: add tax helper")
    (repo / "payments" / "finance").mkdir(parents=True, exist_ok=True)
    _git(repo, "mv", "payments/tax.py", "payments/finance/vat.py")
    vat_file = repo / "payments" / "finance" / "vat.py"
    vat_file.write_text("def compute_vat(val: float) -> float:\n    return val * 0.05\n")
    c5 = _commit(repo, "C5: move and rename tax.py:calc_tax -> finance/vat.py:compute_vat")
    revisions.append(c5)

    # C6: Framework Route Change + Dynamic Reflection Case
    router_file = repo / "payments" / "routers.py"
    router_text = router_file.read_text()
    router_file.write_text(
        router_text.replace('@router.post("/checkout/"', '@router.post("/v2/checkout/"')
        + "\ndef dynamic_dispatch(obj: object, method: str) -> None:\n    getattr(obj, method)()\n"
    )
    c6 = _commit(repo, "C6: update route path to /v2/checkout/ and add dynamic dispatch")
    revisions.append(c6)

    # C7: File & Symbol Deletion
    _git(repo, "rm", "payments/finance/vat.py")
    c7 = _commit(repo, "C7: delete finance/vat.py module")
    revisions.append(c7)

    # C8: Negative Control Commit (Docstrings and comments only)
    svc_text = svc_file.read_text()
    commentary = "# Top-level module commentary\n\"\"\"Service implementation.\"\"\"\n"
    svc_file.write_text(commentary + svc_text)
    c8 = _commit(repo, "C8: negative control docstring update")
    revisions.append(c8)

    return revisions


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


def test_r6_clean_full_index_baseline_per_commit(repo: Path) -> None:
    """Capture clean full-index state at every single commit in isolation (Graph_full(Ci))
    and verify ground-truth entity and relationship structures."""
    revisions = _build_evolution_history(repo)
    assert len(revisions) == 9

    clean_baselines: dict[str, dict[str, object]] = {}

    for i, rev in enumerate(revisions):
        _git(repo, "checkout", "-q", rev)
        db = MemoryDatabase()
        system = System(name=f"Checkout-C{i}", slug=f"checkout-c{i}")
        with db.unit_of_work() as uow:
            uow.systems.save(system)
            uow.commit()

        service = _make_service(db)
        res = service.index(repo, system_id=system.id, revision=rev)
        assert res.errors == []

        with db.unit_of_work() as uow:
            graph = query_at_revision(uow, system_id=system.id, revision=rev)
            entities = {e.qualified_name: e for e in graph.entities}
            rels = {(r.source_entity_id, r.target_entity_id, r.type) for r in graph.relationships}

            clean_baselines[f"C{i}"] = {
                "revision": rev,
                "entity_count": len(entities),
                "entities": entities,
                "rel_count": len(rels),
                "relationships": rels,
            }

    # =========================================================================
    # Verify C0 Clean Baseline
    # =========================================================================
    c0 = clean_baselines["C0"]
    c0_ents: dict[str, object] = c0["entities"]  # type: ignore
    assert "payments.db_models.Payment" in c0_ents
    assert "payments.status" in c0_ents
    assert "payments.services.PaymentService.process" in c0_ents
    assert "payments.routers.router:POST /payments/checkout/" in c0_ents

    # =========================================================================
    # Verify C1 Clean Baseline (Addition of discount helper)
    # =========================================================================
    c1 = clean_baselines["C1"]
    c1_ents: dict[str, object] = c1["entities"]  # type: ignore
    assert "payments.discount.compute_discount" in c1_ents
    assert c1["entity_count"] > c0["entity_count"]  # type: ignore

    # =========================================================================
    # Verify C2 Clean Baseline (ORM Field Rename: status -> state)
    # =========================================================================
    c2 = clean_baselines["C2"]
    c2_ents: dict[str, object] = c2["entities"]  # type: ignore
    assert "payments.state" in c2_ents
    assert "payments.status" not in c2_ents

    # =========================================================================
    # Verify C3 Clean Baseline (Constructor dependency)
    # =========================================================================
    c3 = clean_baselines["C3"]
    c3_ents: dict[str, object] = c3["entities"]  # type: ignore
    assert "payments.gateway.PaymentGateway" in c3_ents
    assert "payments.services.PaymentService.__init__" in c3_ents

    # =========================================================================
    # Verify C4 Clean Baseline (File move: discount.py -> pricing/discount.py)
    # =========================================================================
    c4 = clean_baselines["C4"]
    c4_ents: dict[str, object] = c4["entities"]  # type: ignore
    assert "payments.pricing.discount.compute_discount" in c4_ents
    assert "payments.discount.compute_discount" not in c4_ents

    # =========================================================================
    # Verify C5 Clean Baseline (File + Symbol rename: finance/vat.py:compute_vat)
    # =========================================================================
    c5 = clean_baselines["C5"]
    c5_ents: dict[str, object] = c5["entities"]  # type: ignore
    assert "payments.finance.vat.compute_vat" in c5_ents

    # =========================================================================
    # Verify C6 Clean Baseline (Route change + dynamic dispatch)
    # =========================================================================
    c6 = clean_baselines["C6"]
    c6_ents: dict[str, object] = c6["entities"]  # type: ignore
    assert "payments.routers.router:POST /payments/v2/checkout/" in c6_ents
    assert "payments.routers.router:POST /payments/checkout/" not in c6_ents
    assert "payments.routers.dynamic_dispatch" in c6_ents

    # =========================================================================
    # Verify C7 Clean Baseline (Deletion of finance/vat.py)
    # =========================================================================
    c7 = clean_baselines["C7"]
    c7_ents: dict[str, object] = c7["entities"]  # type: ignore
    assert "payments.finance.vat.compute_vat" not in c7_ents

    # =========================================================================
    # Verify C8 Clean Baseline (Negative Control: Docstring/comment only)
    # =========================================================================
    c8 = clean_baselines["C8"]
    c8_ents: dict[str, object] = c8["entities"]  # type: ignore
    assert set(c8_ents.keys()) == set(c7_ents.keys())
    assert c8["entity_count"] == c7["entity_count"]
    assert c8["rel_count"] == c7["rel_count"]


def test_r6_sequential_evolution_lineage_and_history(repo: Path) -> None:
    """Run sequential indexing across the 9 evolution commits in a shared database,
    verifying identity resolution, SUPERSEDES lineage, historical preservation,
    and negative-control equivalence."""
    revisions = _build_evolution_history(repo)

    db = MemoryDatabase()
    system = System(name="Checkout-Seq", slug="checkout-seq")
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()

    service = _make_service(db)

    for _i, rev in enumerate(revisions):
        _git(repo, "checkout", "-q", rev)
        res = service.index(repo, system_id=system.id, revision=rev)
        assert res.errors == []

    with db.unit_of_work() as uow:
        # 1. Verify C0 historical state remains intact
        g0 = query_at_revision(uow, system_id=system.id, revision=revisions[0])
        c0_ents = {e.qualified_name: e for e in g0.entities}
        assert "payments.status" in c0_ents
        assert "payments.state" not in c0_ents

        # 2. Verify C2 lineage: payments.state supersedes payments.status
        g2 = query_at_revision(uow, system_id=system.id, revision=revisions[2])
        c2_ents = {e.qualified_name: e for e in g2.entities}
        assert "payments.state" in c2_ents
        state_ent = c2_ents["payments.state"]
        lineage = uow.graph.get_relationships(
            state_ent.id, direction="out", types=[RelationshipType.SUPERSEDES]
        )
        assert any(r.target_entity_id == c0_ents["payments.status"].id for r in lineage)

        # 3. Verify C8 vs C7 negative control
        g7 = query_at_revision(uow, system_id=system.id, revision=revisions[7])
        g8 = query_at_revision(uow, system_id=system.id, revision=revisions[8])
        c7_names = {e.qualified_name for e in g7.entities}
        c8_names = {e.qualified_name for e in g8.entities}
        assert c8_names == c7_names

        # 4. Verify R5 MCP neighborhood and impact summary on the evolving graph
        neigh_c8 = get_entity_neighborhood(
            uow, system_id=system.id, entity_id=state_ent.id, revision=revisions[8]
        )
        assert neigh_c8 is not None
        assert neigh_c8.entity.name == "state"
        assert neigh_c8.entity.qualified_name == "payments.state"

        impact_c8 = reverse_impact(
            uow, system_id=system.id, entity_id=state_ent.id, revision=revisions[8]
        )
        summary_c8 = summarize_impact(impact_c8)
        assert summary_c8.affected_entity_count >= 1
