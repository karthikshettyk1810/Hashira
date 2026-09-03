"""End-to-end: a real fixture repository, indexed through the full pipeline.

`tests/fixtures/python_basic/` is a small but realistic project (§ the
adversarial-identity plan): a `src/` layout, a `tests/` package, imports,
inheritance-free plain classes, and one genuinely unresolved call (nothing in
the fixture calls anything outside itself, so every call *should* resolve —
proving that a real, non-contrived project produces zero silent gaps).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from hashira.adapters.python import PythonAdapter, normalize
from hashira.application import IndexingService
from hashira.core import (
    Confidence,
    Entity,
    EntityStatus,
    EntityType,
    IdentityClaim,
    IdentityClaimKind,
    InferenceStatus,
    KnowledgeClass,
    Origin,
    RelationshipType,
    SnapshotStatus,
    System,
)
from hashira.storage.memory import MemoryDatabase

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "python_basic"


def _copy_fixture(tmp_path: Path) -> Path:
    dest = tmp_path / "project"
    shutil.copytree(FIXTURE, dest)
    return dest


def _new_service(db: MemoryDatabase) -> IndexingService:
    return IndexingService(db.unit_of_work, [PythonAdapter()], normalize)


def _entities_by_qn(db: MemoryDatabase, system_id: str) -> dict[str, Entity]:
    with db.unit_of_work() as uow:
        return {e.qualified_name: e for e in uow.graph.find_entities(system_id, limit=10_000)}


def _relationship_pairs(
    db: MemoryDatabase, system_id: str, rel_type: RelationshipType, *, current_only: bool = True
) -> set[tuple[str, str]]:
    with db.unit_of_work() as uow:
        entities = uow.graph.find_entities(system_id, limit=10_000)
        pairs: set[tuple[str, str]] = set()
        for entity in entities:
            for rel in uow.graph.get_relationships(entity.id, direction="out", types=[rel_type]):
                if current_only and not rel.is_current:
                    continue
                target = next((e for e in entities if e.id == rel.target_entity_id), None)
                if target is not None:
                    pairs.add(
                        (entity.qualified_name or entity.id, target.qualified_name or target.id)
                    )
    return pairs


@pytest.fixture
def system(db: MemoryDatabase) -> System:
    system = System(name="Shop", slug="shop")
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()
    return system


@pytest.fixture
def db() -> MemoryDatabase:
    return MemoryDatabase()


# --- the golden fixture, end to end ------------------------------------


def test_golden_fixture_indexes_cleanly(tmp_path: Path, db: MemoryDatabase, system: System) -> None:
    root = _copy_fixture(tmp_path)
    result = _new_service(db).index(root, system_id=system.id, revision="rev1")

    assert result.errors == []
    assert result.snapshot.status is SnapshotStatus.COMPLETE
    assert result.files_processed == 9  # 5 src + __init__ + 2 tests + tests/__init__
    assert result.ambiguous_count == 0


def test_golden_fixture_call_chain_matches_the_worked_example(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    """The exact chain from the design discussion: CheckoutService.checkout
    calls PaymentService (construct), PaymentService.process, and
    send_receipt -- all three resolved, since all three live in the fixture."""
    root = _copy_fixture(tmp_path)
    _new_service(db).index(root, system_id=system.id, revision="rev1")

    calls = _relationship_pairs(db, system.id, RelationshipType.CALLS)
    checkout = "shop.checkout.CheckoutService.checkout"
    assert (checkout, "shop.payments.PaymentService") in calls
    assert (checkout, "shop.payments.PaymentService.process") in calls
    assert (checkout, "shop.notifications.send_receipt") in calls
    assert (
        "shop.payments.PaymentService.process",
        "shop.payments.PaymentService.validate",
    ) in calls
    assert ("shop.payments.PaymentService.validate", "shop.utils.is_positive") in calls


def test_golden_fixture_imports_and_definitions(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    root = _copy_fixture(tmp_path)
    _new_service(db).index(root, system_id=system.id, revision="rev1")

    entities = _entities_by_qn(db, system.id)
    assert entities["shop.checkout.CheckoutService"].type is EntityType.SYMBOL
    assert entities["shop.payments"].type is EntityType.MODULE

    imports = _relationship_pairs(db, system.id, RelationshipType.IMPORTS)
    assert ("shop.checkout", "shop.payments.PaymentService") in imports
    assert ("shop.payments", "shop.utils.is_positive") in imports

    defines = _relationship_pairs(db, system.id, RelationshipType.DEFINES)
    assert ("shop.payments", "shop.payments.PaymentService") in defines
    assert ("shop.payments.PaymentService", "shop.payments.PaymentService.process") in defines


def test_reindexing_creates_a_new_snapshot_chained_to_the_last(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    root = _copy_fixture(tmp_path)
    service = _new_service(db)
    first = service.index(root, system_id=system.id, revision="rev1")
    second = service.index(root, system_id=system.id, revision="rev2")

    assert second.snapshot.parent_snapshot_id == first.snapshot.id
    with db.unit_of_work() as uow:
        latest = uow.snapshots.latest_complete(system.id)
        assert latest is not None
        assert latest.id == second.snapshot.id


def test_reindexing_four_times_is_fully_stable(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    """A regression guard for a real bug the manual verification of the
    boring-reindex fix caught: `PaymentService.process` in the fixture calls
    `PaymentResult` from two separate branches, and an earlier version of the
    normalizer emitted one relationship row per call site rather than per
    (source, target, type) triple -- so run 1 produced one more relationship
    than every run after it, since reconciliation only dedupes *across* runs,
    not within one run's own candidate set. Four runs, not two, so a
    one-off/first-run artifact can't hide as "eventually stable"."""
    root = _copy_fixture(tmp_path)
    service = _new_service(db)
    counts = [
        service.index(root, system_id=system.id, revision=f"rev{i}").relationships_upserted
        for i in range(1, 5)
    ]
    assert len(set(counts)) == 1, f"relationship count should be identical every run: {counts}"

    with db.unit_of_work() as uow:
        entities = uow.graph.find_entities(system.id, limit=10_000)
    assert all(e.status is EntityStatus.ACTIVE for e in entities)


# --- adversarial identity: the same pipeline, real mutations -------------


def test_unchanged_project_reindex_is_boring(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    """The fix: `DECLARATION_ANCHOR` (file + qualified name + kind) is a
    strong-tier signal, so an unchanged file re-indexed resolves as MATCHED
    in place -- no new entity generation, no lineage, and (thanks to
    relationship reconciliation) no duplicate relationship rows either. A
    `hashira index` on an untouched project should produce nothing new."""
    root = _copy_fixture(tmp_path)
    service = _new_service(db)
    service.index(root, system_id=system.id, revision="rev1")

    with db.unit_of_work() as uow:
        before_entities = {e.id: e for e in uow.graph.find_entities(system.id, limit=10_000)}
        before_relationship_ids = {
            rel.id
            for e in before_entities.values()
            for rel in uow.graph.get_relationships(e.id, direction="out")
        }

    second = service.index(root, system_id=system.id, revision="rev2")

    with db.unit_of_work() as uow:
        after_entities = {e.id: e for e in uow.graph.find_entities(system.id, limit=10_000)}
        after_relationship_ids = {
            rel.id
            for e in after_entities.values()
            for rel in uow.graph.get_relationships(e.id, direction="out")
        }

    # Same entity ids, same status -- nothing new, nothing superseded.
    assert set(before_entities) == set(after_entities)
    assert all(e.status is EntityStatus.ACTIVE for e in after_entities.values())

    # Same relationship rows, by id -- nothing new, nothing closed, no
    # duplicates. (A closed edge would still have its old id, but would no
    # longer be `is_current`; a duplicate would add a new id. Neither happened.)
    assert before_relationship_ids == after_relationship_ids
    with db.unit_of_work() as uow:
        all_current = [
            rel
            for e in after_entities.values()
            for rel in uow.graph.get_relationships(e.id, direction="out")
        ]
        assert all(rel.is_current for rel in all_current)

    assert second.entities_upserted == len(after_entities)
    assert second.snapshot.statistics.entity_count == len(after_entities)


def test_unrelated_entities_are_unaffected_by_an_unchanged_reindex(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    """A second, narrower check on the same fix: every entity's id is stable
    across a no-op re-index, not just the one this test happens to sample."""
    root = _copy_fixture(tmp_path)
    service = _new_service(db)
    service.index(root, system_id=system.id, revision="rev1")
    ids_before = {e.id for e in _entities_by_qn(db, system.id).values()}

    service.index(root, system_id=system.id, revision="rev2")
    ids_after = {e.id for e in _entities_by_qn(db, system.id).values()}

    assert ids_before == ids_after


def test_adding_one_call_reconciles_precisely(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    """The three-way split relationship reconciliation is supposed to make:
    a genuinely new edge is inserted, every untouched edge is left exactly
    as it was (same row), and nothing is closed, since nothing disappeared."""
    root = _copy_fixture(tmp_path)
    service = _new_service(db)
    service.index(root, system_id=system.id, revision="rev1")

    with db.unit_of_work() as uow:
        before_ids = {
            rel.id
            for e in uow.graph.find_entities(system.id, limit=10_000)
            for rel in uow.graph.get_relationships(e.id, direction="out")
        }

    utils = root / "src" / "shop" / "utils.py"
    utils.write_text(utils.read_text() + "\n\ndef is_negative(amount):\n    return amount < 0\n")
    checkout = root / "src" / "shop" / "checkout.py"
    checkout.write_text(
        checkout.read_text()
        .replace(
            "from .payments import PaymentService",
            "from .payments import PaymentService\nfrom .utils import is_negative",
        )
        .replace(
            "        payment = PaymentService()",
            "        is_negative(order.total)\n        payment = PaymentService()",
        )
    )
    service.index(root, system_id=system.id, revision="rev2")

    with db.unit_of_work() as uow:
        after = {
            rel.id: rel
            for e in uow.graph.find_entities(system.id, limit=10_000)
            for rel in uow.graph.get_relationships(e.id, direction="out")
        }

    after_ids = set(after)
    new_ids = after_ids - before_ids
    # shop.utils DEFINES is_negative, shop.checkout IMPORTS is_negative,
    # and checkout.checkout CALLS is_negative -- exactly the three new facts.
    assert len(new_ids) == 3
    assert all(after[i].is_current for i in new_ids)

    # Every previously-existing edge id survives untouched: none were closed,
    # none were replaced with a fresh row for the same semantic edge.
    assert before_ids <= after_ids
    assert all(after[i].is_current for i in before_ids)

    calls = _relationship_pairs(db, system.id, RelationshipType.CALLS)
    assert ("shop.checkout.CheckoutService.checkout", "shop.utils.is_negative") in calls


def test_class_rename_is_new_with_the_old_entity_orphaned(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    """A rename changes both the qualified name and `DECLARATION_ANCHOR` (see
    normalizer.py), which were the *only* signals this candidate and the old
    entity could have shared. With nothing left to corroborate, the ladder
    correctly does not guess a connection -- the renamed class comes back as
    NEW, with zero lineage to its former self, and the old entity is left
    ACTIVE and orphaned (v0.1 has no removal detection either). This is
    safe (nothing is silently merged or deleted) but not useful continuity;
    closing that gap needs a Git adapter's rename evidence (ROADMAP.md)."""
    root = _copy_fixture(tmp_path)
    service = _new_service(db)
    service.index(root, system_id=system.id, revision="rev1")

    payments = root / "src" / "shop" / "payments.py"
    checkout = root / "src" / "shop" / "checkout.py"
    payments.write_text(payments.read_text().replace("PaymentService", "PaymentProcessor"))
    checkout.write_text(checkout.read_text().replace("PaymentService", "PaymentProcessor"))
    service.index(root, system_id=system.id, revision="rev2")

    entities = _entities_by_qn(db, system.id)
    assert "shop.payments.PaymentProcessor" in entities
    assert entities["shop.payments.PaymentProcessor"].status is EntityStatus.ACTIVE
    # The old entity is not deleted and not marked superseded -- just orphaned.
    assert entities["shop.payments.PaymentService"].status is EntityStatus.ACTIVE

    lineage = _relationship_pairs(db, system.id, RelationshipType.SUPERSEDES)
    assert ("shop.payments.PaymentProcessor", "shop.payments.PaymentService") not in lineage

    # The renamed class's own methods survive under their new qualified name,
    # and the (also-updated) caller gets a fresh CALLS edge to the new symbol.
    assert "shop.payments.PaymentProcessor.process" in entities
    calls = _relationship_pairs(db, system.id, RelationshipType.CALLS)
    assert (
        "shop.checkout.CheckoutService.checkout",
        "shop.payments.PaymentProcessor.process",
    ) in calls

    # The *stale* edge to the old symbol is retracted, not left dangling:
    # relationship reconciliation (indexing.py) closes any edge not
    # re-observed this run. It still exists -- history stays readable -- but
    # is no longer current.
    assert (
        "shop.checkout.CheckoutService.checkout",
        "shop.payments.PaymentService.process",
    ) not in calls
    all_calls_including_closed = _relationship_pairs(
        db, system.id, RelationshipType.CALLS, current_only=False
    )
    assert (
        "shop.checkout.CheckoutService.checkout",
        "shop.payments.PaymentService.process",
    ) in all_calls_including_closed


def test_extracted_method_is_new_not_falsely_merged(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    """A brand-new symbol born from refactoring must never be mistaken for
    something that already existed, no matter how structurally similar."""
    root = _copy_fixture(tmp_path)
    service = _new_service(db)
    service.index(root, system_id=system.id, revision="rev1")

    payments = root / "src" / "shop" / "payments.py"
    original = payments.read_text()
    mutated = original.replace(
        "    def validate(self, amount):\n        return is_positive(amount)\n",
        (
            "    def validate(self, amount):\n"
            "        return validate_payment_amount(amount)\n"
            "\n\n"
            "def validate_payment_amount(amount):\n"
            "    return is_positive(amount)\n"
        ),
    )
    assert mutated != original
    payments.write_text(mutated)
    service.index(root, system_id=system.id, revision="rev2")

    entities = _entities_by_qn(db, system.id)
    new_fn = entities["shop.payments.validate_payment_amount"]
    assert new_fn.status is EntityStatus.ACTIVE
    # It must not be linked as a supersession of anything -- it is genuinely new.
    lineage = _relationship_pairs(db, system.id, RelationshipType.SUPERSEDES)
    assert new_fn.qualified_name not in {target for _source, target in lineage}
    assert new_fn.qualified_name not in {source for source, _target in lineage}

    calls = _relationship_pairs(db, system.id, RelationshipType.CALLS)
    assert (
        "shop.payments.PaymentService.validate",
        "shop.payments.validate_payment_amount",
    ) in calls


def test_structurally_similar_code_in_a_new_module_is_never_linked(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    """A near-identical copy/paste of PaymentService.process, under a
    different name in a different module, must resolve as NEW -- structural
    similarity alone is not, and must never become, an identity signal here."""
    root = _copy_fixture(tmp_path)
    service = _new_service(db)
    service.index(root, system_id=system.id, revision="rev1")

    clone = root / "src" / "shop" / "refunds.py"
    clone.write_text(
        "from .utils import is_positive\n"
        "from .models import Payment\n\n\n"
        "class RefundResult:\n"
        "    def __init__(self, success):\n"
        "        self.success = success\n\n\n"
        "class RefundService:\n"
        "    def process(self, amount):\n"
        "        if self.validate(amount):\n"
        "            payment = Payment(amount, 'refunded')\n"
        "            return RefundResult(True)\n"
        "        return RefundResult(False)\n\n"
        "    def validate(self, amount):\n"
        "        return is_positive(amount)\n"
    )
    service.index(root, system_id=system.id, revision="rev2")

    entities = _entities_by_qn(db, system.id)
    clone_entity = entities["shop.refunds.RefundService.process"]
    original_entity = entities["shop.payments.PaymentService.process"]
    assert clone_entity.id != original_entity.id
    assert clone_entity.status is EntityStatus.ACTIVE
    assert original_entity.status is EntityStatus.ACTIVE  # untouched by the clone

    # Nothing was renamed, only a new file added -- with unchanged entities
    # now resolving as MATCHED (not SUPERSEDES), the whole run should
    # produce zero lineage at all. In particular, the clone must not appear
    # anywhere in it: it is new, not a continuation of anything, and
    # structural similarity must not manufacture a connection.
    lineage = _relationship_pairs(db, system.id, RelationshipType.SUPERSEDES)
    assert not lineage


def _entity_with_qualified_name_claim(system_id: str, qualified_name: str, name: str) -> Entity:
    """A hand-built entity carrying the *same kind* of claim the real Python
    adapter produces -- a hit in `identity.resolver._hits_for` only counts
    when both sides share a claim kind, so this must be QUALIFIED_NAME, not
    something stronger, to reproduce what real (mis)indexed data would carry."""
    return Entity(
        system_id=system_id,
        type=EntityType.SYMBOL,
        name=name,
        qualified_name=qualified_name,
        identity_claims=[
            IdentityClaim(
                kind=IdentityClaimKind.QUALIFIED_NAME,
                value=qualified_name,
                origin=Origin.PARSER,
                confidence=Confidence.CERTAIN,
            )
        ],
    )


def test_duplicate_qualified_name_in_storage_is_ambiguous_not_guessed(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    """Constructed directly rather than through two organic files: the graph
    already contains *two* entities sharing a qualified name (a state that
    should not normally arise, but the resolver must not paper over it if it
    does -- see identity/resolver.py's AMBIGUOUS outcome). Re-indexing the
    fixture produces a third, real candidate for the same qualified name,
    which ties with both and must not silently pick one."""
    root = _copy_fixture(tmp_path)

    qn = "shop.payments.PaymentService"
    first = _entity_with_qualified_name_claim(system.id, qn, "PaymentService")
    second = _entity_with_qualified_name_claim(system.id, qn, "PaymentService")
    with db.unit_of_work() as uow:
        uow.graph.upsert_entities([first, second])
        uow.commit()

    service = _new_service(db)
    service.index(root, system_id=system.id, revision="rev1")

    with db.unit_of_work() as uow:
        proposed = uow.inferences.find(system.id, status=InferenceStatus.PROPOSED.value)
    hypotheses = [i for i in proposed if i.knowledge_class is KnowledgeClass.HYPOTHESIS]
    assert hypotheses, "an ambiguous match must surface as a hypothesis, not silently resolve"
    subjects = set(hypotheses[0].subject_entity_ids)
    assert {first.id, second.id} <= subjects

    # And critically: neither pre-existing duplicate was silently chosen as
    # the "real" match, nor merged away -- both remain, and the real
    # candidate now exists as a third entity of its own rather than being
    # dropped or forced into a guess.
    with db.unit_of_work() as uow:
        all_entities = uow.graph.find_entities(system.id, limit=10_000)
    sharing_qn = [e for e in all_entities if e.qualified_name == qn]
    assert len(sharing_qn) == 3
    assert {first.id, second.id} <= {e.id for e in sharing_qn}
