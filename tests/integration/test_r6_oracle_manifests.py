"""R6 Oracle Manifest Tests: Extract and verify machine-comparable ground truth
for stages C0..C8 across the adversarial evolution sequence.
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
    extract_oracle_manifest,
    save_oracle_manifests,
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


def test_extract_and_validate_all_r6_oracle_manifests(repo: Path) -> None:
    """Extract machine-comparable oracle manifests for C0..C8, save them,
    and verify self-equivalence under semantic comparison."""
    revisions = _build_evolution_history(repo)
    assert len(revisions) == 9

    manifests: dict[str, GraphOracleManifest] = {}

    for i, rev in enumerate(revisions):
        stage_name = f"C{i}"
        _git(repo, "checkout", "-q", rev)

        db = MemoryDatabase()
        system = System(name=f"Oracle-{stage_name}", slug=f"oracle-c{i}")
        with db.unit_of_work() as uow:
            uow.systems.save(system)
            uow.commit()

        service = _make_service(db)
        res = service.index(repo, system_id=system.id, revision=rev)
        assert res.errors == []

        with db.unit_of_work() as uow:
            manifest = extract_oracle_manifest(
                uow, system_id=system.id, revision=rev, stage=stage_name
            )
            manifests[stage_name] = manifest

            # Verify semantic equivalence against the materialized HistoricalGraph
            graph = query_at_revision(uow, system_id=system.id, revision=rev)
            eq_res = compare_semantic_equivalence(graph, manifest)
            assert eq_res.equivalent, f"Failed at {stage_name}: {eq_res.details}"

    # Persist the oracle manifests to the repository fixtures
    save_oracle_manifests(manifests, ORACLE_DIR)

    # Verify C8 vs C7 negative control in manifests
    c7_manifest = manifests["C7"]
    c8_manifest = manifests["C8"]
    eq_control = compare_semantic_equivalence(c8_manifest, c7_manifest)
    assert eq_control.equivalent, f"Negative control failed: {eq_control.details}"


def test_oracle_comparator_detects_deliberate_deviations(repo: Path) -> None:
    """Verify that the semantic comparator correctly catches missing entities,
    extra entities, and missing relationships."""
    revisions = _build_evolution_history(repo)
    _git(repo, "checkout", "-q", revisions[0])

    db = MemoryDatabase()
    system = System(name="Oracle-C0", slug="oracle-c0")
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()

    service = _make_service(db)
    service.index(repo, system_id=system.id, revision=revisions[0])

    with db.unit_of_work() as uow:
        manifest_c0 = extract_oracle_manifest(
            uow, system_id=system.id, revision=revisions[0], stage="C0"
        )
        graph_c0 = query_at_revision(uow, system_id=system.id, revision=revisions[0])

    # 1. Self comparison is equivalent
    assert compare_semantic_equivalence(graph_c0, manifest_c0).equivalent

    # 2. Comparison against C1 manifest must FAIL (C1 has discount helper)
    # Extract C1
    _git(repo, "checkout", "-q", revisions[1])
    db1 = MemoryDatabase()
    system1 = System(name="Oracle-C1", slug="oracle-c1")
    with db1.unit_of_work() as uow:
        uow.systems.save(system1)
        uow.commit()
    service1 = _make_service(db1)
    service1.index(repo, system_id=system1.id, revision=revisions[1])

    with db1.unit_of_work() as uow:
        manifest_c1 = extract_oracle_manifest(
            uow, system_id=system1.id, revision=revisions[1], stage="C1"
        )

    # Comparing C0 graph against C1 manifest must report missing discount entities
    diff = compare_semantic_equivalence(graph_c0, manifest_c1)
    assert not diff.equivalent
    assert any("payments.discount" in e for e in diff.missing_entities)
    assert len(diff.missing_relationships) > 0
