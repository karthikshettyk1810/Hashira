"""Stage 2: cross-file linking. A resolved-looking guess only becomes a
Relationship when the target actually exists among this run's candidates."""

from __future__ import annotations

from pathlib import Path

from hashira.adapters.python.extractor import extract_file
from hashira.adapters.python.normalizer import normalize
from hashira.core.enums import EntityType, IdentityClaimKind, RelationshipType
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


def test_method_parameter_kinds_and_names_are_preserved(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "shop/payments.py": (
                "class PaymentService:\n"
                "    def process(self, /, payment, *events, timeout, **options):\n"
                "        pass\n"
            )
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    method = next(
        e for e in run.entities if e.qualified_name == "shop.payments.PaymentService.process"
    )

    assert method.metadata["parameters"] == [
        {"kind": "positional_only", "name": "self"},
        {"kind": "positional_or_keyword", "name": "payment"},
        {"kind": "var_positional", "name": "events"},
        {"kind": "keyword_only", "name": "timeout"},
        {"kind": "var_keyword", "name": "options"},
    ]


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


def test_every_entity_carries_qualified_name_and_declaration_anchor_claims(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path, {"shop/payments.py": "class PaymentService:\n    pass\n"}, system_id
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    for entity in run.entities:
        claim_kinds = {claim.kind for claim in entity.identity_claims}
        assert claim_kinds == {
            IdentityClaimKind.QUALIFIED_NAME,
            IdentityClaimKind.DECLARATION_ANCHOR,
        }

        qn_claim = next(
            c for c in entity.identity_claims if c.kind is IdentityClaimKind.QUALIFIED_NAME
        )
        assert qn_claim.value == entity.qualified_name

        anchor_claim = next(
            c for c in entity.identity_claims if c.kind is IdentityClaimKind.DECLARATION_ANCHOR
        )
        assert entity.qualified_name in anchor_claim.value
        assert entity.source is not None
        assert entity.source.file in anchor_claim.value


def test_declaration_anchor_changes_when_the_file_changes(tmp_path: Path) -> None:
    """The whole point: an anchor must not survive a move on its own -- that
    boundary is exactly where Git evidence is still required."""
    system_id = new_id(IDPrefix.SYSTEM)
    before = _extract_all(
        tmp_path, {"shop/payments.py": "class PaymentService:\n    pass\n"}, system_id
    )
    run_before = normalize(before, system_id=system_id, revision="rev1")
    anchor_before = next(
        c.value
        for e in run_before.entities
        for c in e.identity_claims
        if c.kind is IdentityClaimKind.DECLARATION_ANCHOR
        and e.qualified_name == "shop.payments.PaymentService"
    )

    after = _extract_all(
        tmp_path, {"shop/billing.py": "class PaymentService:\n    pass\n"}, system_id
    )
    run_after = normalize(after, system_id=system_id, revision="rev2")
    anchor_after = next(
        c.value
        for e in run_after.entities
        for c in e.identity_claims
        if c.kind is IdentityClaimKind.DECLARATION_ANCHOR
        and e.qualified_name == "shop.billing.PaymentService"
    )
    assert anchor_before != anchor_after


def test_every_relationship_cites_the_evidence_of_its_source_observation(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path, {"shop/payments.py": "class PaymentService:\n    pass\n"}, system_id
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert run.relationships
    for rel in run.relationships:
        assert rel.evidence_ids


def test_two_call_sites_to_the_same_target_produce_one_relationship(tmp_path: Path) -> None:
    """CALLS means "does A call B at all", not "how many times" -- a
    function calling the same target from two call sites must not produce
    two relationship rows (found via a real fixture: PaymentService.process
    constructs PaymentResult from two different branches)."""
    system_id = new_id(IDPrefix.SYSTEM)
    source = (
        "class Result:\n"
        "    def __init__(self, ok):\n"
        "        self.ok = ok\n\n\n"
        "class Service:\n"
        "    def run(self, ok):\n"
        "        if ok:\n"
        "            return Result(True)\n"
        "        return Result(False)\n"
    )
    observations = _extract_all(tmp_path, {"shop/service.py": source}, system_id)
    run = normalize(observations, system_id=system_id, revision="rev1")

    calls = [r for r in run.relationships if r.type is RelationshipType.CALLS]
    assert len(calls) == 1
    # Evidence from both call sites survives the merge -- neither is dropped.
    assert len(calls[0].evidence_ids) == 2


# --- module-level singleton instances (a real-repository finding) ---------


def test_call_through_an_imported_module_level_singleton_resolves(tmp_path: Path) -> None:
    """`kafka_publisher = KafkaEventPublisher()` at module scope in one file,
    imported and called from another (`from common.kafka import
    kafka_publisher; kafka_publisher.push_notification(...)`) -- a real
    production repository's dominant singleton-client idiom. Stage 1 (single
    file) can only resolve the call against the *literal* import target
    (`common.kafka.kafka_publisher.push_notification`), which names no real
    entity; Stage 2 must re-target it through the `python.module_instance`
    observation the defining file produced."""
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "common/kafka/publisher.py": (
                "class KafkaEventPublisher:\n"
                "    def push_notification(self, payload):\n"
                "        pass\n"
            ),
            "common/kafka/__init__.py": (
                "from common.kafka.publisher import KafkaEventPublisher\n\n"
                "kafka_publisher = KafkaEventPublisher()\n"
            ),
            "common/outbox/service.py": (
                "def enqueue_push_notification(payload):\n"
                "    from common.kafka import kafka_publisher\n"
                "    kafka_publisher.push_notification(payload)\n"
            ),
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    by_qn = {e.qualified_name: e for e in run.entities}

    calls = [r for r in run.relationships if r.type is RelationshipType.CALLS]
    pairs = {(r.source_entity_id, r.target_entity_id) for r in calls}
    caller = by_qn["common.outbox.service.enqueue_push_notification"].id
    callee = by_qn["common.kafka.publisher.KafkaEventPublisher.push_notification"].id
    assert (caller, callee) in pairs


def test_singleton_retargeting_never_fabricates_a_match_for_an_unrelated_name(
    tmp_path: Path,
) -> None:
    """A module-level instance whose class defines no method matching the
    call's own suffix must not resolve to *something else* nearby -- an
    unmatched retarget must stay unresolved, not a wrong-but-plausible-
    looking edge."""
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "common/kafka/publisher.py": (
                "class KafkaEventPublisher:\n"
                "    def push_notification(self, payload):\n"
                "        pass\n"
            ),
            "common/kafka/__init__.py": (
                "from common.kafka.publisher import KafkaEventPublisher\n\n"
                "kafka_publisher = KafkaEventPublisher()\n"
            ),
            "common/outbox/service.py": (
                "def send(payload):\n"
                "    from common.kafka import kafka_publisher\n"
                "    kafka_publisher.some_unrelated_method(payload)\n"
            ),
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    by_qn = {e.qualified_name: e for e in run.entities}
    calls = [r for r in run.relationships if r.type is RelationshipType.CALLS]
    # The module-level `kafka_publisher = KafkaEventPublisher()` instantiation
    # itself is a real, legitimate CALLS edge (the module really does call
    # the constructor) -- unrelated to retargeting, and expected here.
    pairs = {(r.source_entity_id, r.target_entity_id) for r in calls}
    assert pairs == {
        (
            by_qn["common.kafka"].id,
            by_qn["common.kafka.publisher.KafkaEventPublisher"].id,
        )
    }
    unresolved_calls = [
        obs
        for obs in run.unresolved
        if obs.kind == "python.call"
        and obs.payload["callee_expr"] == "kafka_publisher.some_unrelated_method"
    ]
    assert len(unresolved_calls) == 1
