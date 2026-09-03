"""Stage 2: cross-file linking. A resolved-looking guess only becomes a
Relationship when the target actually exists among this run's candidates."""

from __future__ import annotations

from pathlib import Path

from hashira.adapters.python.extractor import extract_file
from hashira.adapters.python.normalizer import normalize
from hashira.core.enums import EntityType, RelationshipType
from hashira.core.ids import IDPrefix, new_id


def _extract_all(tmp_path: Path, files: dict[str, str], system_id: str):  # type: ignore[no-untyped-def]
    observations = []
    for relpath, source in files.items():
        path = tmp_path / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    for relpath in files:
        result = extract_file(
            tmp_path / relpath, import_root=tmp_path, system_id=system_id, revision="rev1"
        )
        assert result.errors == []
        observations.extend(result.observations)
    return observations


def test_module_defines_class_defines_method(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {"shop/payments.py": "class PaymentService:\n    def process(self):\n        pass\n"},
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    by_qn = {e.qualified_name: e for e in run.entities}
    assert by_qn["shop.payments"].type is EntityType.MODULE
    assert by_qn["shop.payments.PaymentService"].type is EntityType.SYMBOL
    assert by_qn["shop.payments.PaymentService.process"].type is EntityType.SYMBOL

    defines = [r for r in run.relationships if r.type is RelationshipType.DEFINES]
    pairs = {(r.source_entity_id, r.target_entity_id) for r in defines}
    assert (by_qn["shop.payments"].id, by_qn["shop.payments.PaymentService"].id) in pairs
    assert (
        by_qn["shop.payments.PaymentService"].id,
        by_qn["shop.payments.PaymentService.process"].id,
    ) in pairs


def test_cross_file_call_becomes_a_relationship_only_when_the_target_exists(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "shop/payments.py": "class PaymentService:\n    def process(self):\n        pass\n",
            "shop/checkout.py": (
                "from .payments import PaymentService\n\n\n"
                "class CheckoutService:\n"
                "    def checkout(self):\n"
                "        payment = PaymentService()\n"
                "        payment.process()\n"
            ),
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    by_qn = {e.qualified_name: e for e in run.entities}

    calls = [r for r in run.relationships if r.type is RelationshipType.CALLS]
    pairs = {(r.source_entity_id, r.target_entity_id) for r in calls}
    checkout_method = by_qn["shop.checkout.CheckoutService.checkout"].id
    assert (checkout_method, by_qn["shop.payments.PaymentService"].id) in pairs
    assert (checkout_method, by_qn["shop.payments.PaymentService.process"].id) in pairs


def test_call_to_a_file_not_in_this_run_stays_unresolved_not_a_relationship(tmp_path: Path) -> None:
    """PaymentService is imported but its defining file is never extracted in
    this run -- the call must not silently resolve to nothing, or worse,
    invent an entity. It must simply not become a Relationship."""
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "shop/checkout.py": (
                "from .payments import PaymentService\n\n\ndef checkout():\n    PaymentService()\n"
            )
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert [r for r in run.relationships if r.type is RelationshipType.CALLS] == []
    assert [r for r in run.relationships if r.type is RelationshipType.IMPORTS] == []
    assert any(o.kind == "python.call" for o in run.unresolved)
    assert any(o.kind == "python.import" for o in run.unresolved)
    # And critically: no phantom entity was invented for the unresolved target.
    assert "shop.payments.PaymentService" not in {e.qualified_name for e in run.entities}


def test_unresolved_call_never_becomes_a_relationship(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path, {"shop/checkout.py": "def checkout():\n    something_unknown.foo()\n"}, system_id
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert [r for r in run.relationships if r.type is RelationshipType.CALLS] == []
    assert any(o.kind == "python.call" for o in run.unresolved)


def test_inheritance_becomes_extends_when_the_base_is_in_this_run(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "shop/models.py": (
                "class BaseModel:\n    pass\n\n\nclass Payment(BaseModel):\n    pass\n"
            )
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    by_qn = {e.qualified_name: e for e in run.entities}
    extends = [r for r in run.relationships if r.type is RelationshipType.EXTENDS]
    assert len(extends) == 1
    assert extends[0].source_entity_id == by_qn["shop.models.Payment"].id
    assert extends[0].target_entity_id == by_qn["shop.models.BaseModel"].id


def test_every_entity_carries_a_qualified_name_identity_claim(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path, {"shop/payments.py": "class PaymentService:\n    pass\n"}, system_id
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    for entity in run.entities:
        assert len(entity.identity_claims) == 1
        assert entity.identity_claims[0].value == entity.qualified_name


def test_every_relationship_cites_the_evidence_of_its_source_observation(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path, {"shop/payments.py": "class PaymentService:\n    pass\n"}, system_id
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert run.relationships
    for rel in run.relationships:
        assert rel.evidence_ids
