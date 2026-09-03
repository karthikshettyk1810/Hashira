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
    db: MemoryDatabase, system_id: str, rel_type: RelationshipType
) -> set[tuple[str, str]]:
    with db.unit_of_work() as uow:
        entities = uow.graph.find_entities(system_id, limit=10_000)
        pairs: set[tuple[str, str]] = set()
        for entity in entities:
            for rel in uow.graph.get_relationships(entity.id, direction="out", types=[rel_type]):
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


# --- adversarial identity: the same pipeline, real mutations -------------


def test_unchanged_file_reindexed_supersedes_not_matches(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    """The documented limitation (see normalizer.py): pure qualified-name
    matching cannot justify a merge on its own, so even a byte-identical
    re-index produces lineage, not an in-place update, until a Git adapter
    supplies real rename/no-change evidence. This is true for *every* entity
    on *every* re-index, since v0.1 has no incremental indexing yet (nothing
    skips a file just because it didn't change) -- so this one symbol stands
    in for what happens to the whole graph on every run."""
    root = _copy_fixture(tmp_path)
    service = _new_service(db)
    service.index(root, system_id=system.id, revision="rev1")
    service.index(root, system_id=system.id, revision="rev2")

    with db.unit_of_work() as uow:
        all_entities = uow.graph.find_entities(system.id, limit=10_000)
    sharing_qn = [e for e in all_entities if e.qualified_name == "shop.payments.PaymentService"]
    active = [e for e in sharing_qn if e.status is EntityStatus.ACTIVE]
    superseded = [e for e in sharing_qn if e.status is EntityStatus.SUPERSEDED]
    assert len(active) == 1
    assert len(superseded) == 1

    lineage = _relationship_pairs(db, system.id, RelationshipType.SUPERSEDES)
    assert (active[0].qualified_name, superseded[0].qualified_name) in lineage


def test_class_rename_is_new_with_the_old_entity_orphaned(
    tmp_path: Path, db: MemoryDatabase, system: System
) -> None:
    """The honest finding, not the originally-hoped-for one: a rename changes
    the qualified name, which was the *only* signal this candidate and the
    old entity could have shared. With nothing left to corroborate, the
    ladder correctly does not guess a connection -- the renamed class comes
    back as NEW, with zero lineage to its former self, and the old entity is
    left ACTIVE and orphaned (v0.1 has no removal detection either). This is
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

    # The *stale* edge to the old symbol is not retracted, though -- another
    # facet of the same limitation: this pipeline only ever adds relationship
    # observations, it never calls `close_relationships()` for an edge no
    # longer observed. Diffing against the prior run to retract stale edges
    # is incremental-indexing work, not yet built (see indexing.py's
    # docstring and ROADMAP.md Phase 2). Documented here, not hidden.
    assert (
        "shop.checkout.CheckoutService.checkout",
        "shop.payments.PaymentService.process",
    ) in calls


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

    # Every unchanged entity in the fixture legitimately gets SUPERSEDES
    # lineage on this second full re-index (see the "unchanged file" test
    # above) -- that is expected. What must never happen is the *clone*
    # appearing in that lineage at all: it is new, not a continuation of
    # anything, and structural similarity must not manufacture a connection.
    lineage = _relationship_pairs(db, system.id, RelationshipType.SUPERSEDES)
    clone_names = {
        "shop.refunds",
        "shop.refunds.RefundService",
        "shop.refunds.RefundResult",
        "shop.refunds.RefundService.process",
        "shop.refunds.RefundService.validate",
    }
    assert not any(source in clone_names or target in clone_names for source, target in lineage)


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
