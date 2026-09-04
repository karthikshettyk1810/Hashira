"""`application.impact`: reverse/forward traversal mechanics, explainable
paths, per-hop (never per-path) epistemic state, and identity-lineage-aware
resolution -- against hand-built graphs, independent of any adapter.
"""

from __future__ import annotations

import pytest

from hashira.application.history import query_at_revision
from hashira.application.impact import (
    CoverageStatus,
    ImpactAnalyzer,
    follow_lineage,
    forward_impact,
    resolve_identity,
    reverse_impact,
)
from hashira.core import (
    Confidence,
    Entity,
    EntityStatus,
    EntityType,
    Evidence,
    IdentityClaim,
    IdentityClaimKind,
    KnowledgeClass,
    Origin,
    Relationship,
    RelationshipType,
    Snapshot,
    SnapshotStatus,
    SourceRef,
    System,
)
from hashira.ports.adapters import Limitation, LimitationKind, LimitationScope
from hashira.storage.memory import MemoryDatabase


@pytest.fixture
def db() -> MemoryDatabase:
    return MemoryDatabase()


@pytest.fixture
def system(db: MemoryDatabase) -> System:
    system = System(name="Checkout", slug="checkout-impact")
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()
    return system


def _entity(system: System, name: str, *, type: EntityType = EntityType.SYMBOL) -> Entity:
    return Entity(system_id=system.id, type=type, name=name, qualified_name=name)


def _superseded_entity(system: System, name: str) -> Entity:
    """A SUPERSEDED entity must carry the identity claims that justified it
    (§10's own model validator) -- this helper exists purely so lineage
    tests don't have to repeat that boilerplate."""
    return Entity(
        system_id=system.id,
        type=EntityType.SYMBOL,
        name=name,
        qualified_name=name,
        status=EntityStatus.SUPERSEDED,
        identity_claims=[
            IdentityClaim(
                kind=IdentityClaimKind.GIT_RENAME,
                value=f"rename::{name}",
                origin=Origin.GIT,
                confidence=Confidence.CERTAIN,
            )
        ],
    )


def _supersedes(
    system: System, successor: Entity, predecessor: Entity, *, evidence: Evidence | None = None
) -> Relationship:
    return Relationship(
        system_id=system.id,
        source_entity_id=successor.id,
        target_entity_id=predecessor.id,
        type=RelationshipType.SUPERSEDES,
        origin=Origin.DERIVED,
        knowledge_class=KnowledgeClass.DERIVATION,
        evidence_ids=[evidence.id] if evidence else [],
    )


def _evidence(system: System) -> Evidence:
    return Evidence(
        system_id=system.id,
        origin=Origin.STATIC_ANALYSIS,
        source=SourceRef(provider="python-ast", reference="app.py"),
        summary="observed",
        locator="app.py:1",
    )


def _rel(
    system: System,
    source: Entity,
    target: Entity,
    type: RelationshipType,
    *,
    evidence: Evidence | None = None,
    confidence: Confidence | None = None,
    knowledge_class: KnowledgeClass = KnowledgeClass.OBSERVATION,
    origin: Origin = Origin.STATIC_ANALYSIS,
) -> Relationship:
    return Relationship(
        system_id=system.id,
        source_entity_id=source.id,
        target_entity_id=target.id,
        type=type,
        origin=origin,
        knowledge_class=knowledge_class,
        confidence=confidence,
        evidence_ids=[evidence.id] if evidence else [],
    )


def _seed(
    db: MemoryDatabase,
    system: System,
    entities: list[Entity],
    relationships: list[Relationship],
    evidence: list[Evidence] | None = None,
) -> None:
    with db.unit_of_work() as uow:
        uow.graph.upsert_entities(entities)
        uow.graph.upsert_relationships(relationships)
        if evidence:
            uow.evidence.record(evidence)
        uow.commit()


# --- basic traversal mechanics ----------------------------------------------


def test_reverse_impact_walks_backward_through_impact_edges(
    db: MemoryDatabase, system: System
) -> None:
    """route --EXPOSES--> handler --CALLS--> service --WRITES--> field"""
    route = _entity(system, "route", type=EntityType.INTERFACE)
    handler = _entity(system, "handler")
    service = _entity(system, "service")
    field = _entity(system, "field")
    ev = _evidence(system)
    _seed(
        db,
        system,
        [route, handler, service, field],
        [
            _rel(system, route, handler, RelationshipType.EXPOSES, evidence=ev),
            _rel(system, handler, service, RelationshipType.CALLS, evidence=ev),
            _rel(system, service, field, RelationshipType.WRITES, evidence=ev),
        ],
        [ev],
    )

    with db.unit_of_work() as uow:
        result = reverse_impact(uow, system_id=system.id, entity_id=field.id)

    assert {e.id for e in result.affected_entities} == {handler.id, service.id, route.id}
    assert result.direction == "reverse"
    assert result.start.id == field.id


def test_forward_impact_walks_the_opposite_direction(db: MemoryDatabase, system: System) -> None:
    a = _entity(system, "a")
    b = _entity(system, "b")
    c = _entity(system, "c")
    ev = _evidence(system)
    _seed(
        db,
        system,
        [a, b, c],
        [
            _rel(system, a, b, RelationshipType.CALLS, evidence=ev),
            _rel(system, b, c, RelationshipType.WRITES, evidence=ev),
        ],
        [ev],
    )

    with db.unit_of_work() as uow:
        result = forward_impact(uow, system_id=system.id, entity_id=a.id)

    assert {e.id for e in result.affected_entities} == {b.id, c.id}
    assert result.direction == "forward"


def test_structural_edges_are_not_walked_by_default(db: MemoryDatabase, system: System) -> None:
    module = _entity(system, "module", type=EntityType.MODULE)
    cls = _entity(system, "module.Class")
    ev = _evidence(system)
    defines = _rel(system, module, cls, RelationshipType.DEFINES, evidence=ev)
    _seed(db, system, [module, cls], [defines], [ev])

    with db.unit_of_work() as uow:
        result = reverse_impact(uow, system_id=system.id, entity_id=cls.id)

    assert result.affected_entities == ()


def test_edge_types_can_be_overridden(db: MemoryDatabase, system: System) -> None:
    a = _entity(system, "a")
    b = _entity(system, "b")
    ev = _evidence(system)
    _seed(db, system, [a, b], [_rel(system, a, b, RelationshipType.CALLS, evidence=ev)], [ev])

    with db.unit_of_work() as uow:
        result = forward_impact(
            uow,
            system_id=system.id,
            entity_id=a.id,
            edge_types=frozenset({RelationshipType.IMPORTS}),
        )

    assert result.affected_entities == ()


def test_a_cycle_does_not_loop_forever(db: MemoryDatabase, system: System) -> None:
    a = _entity(system, "a")
    b = _entity(system, "b")
    ev = _evidence(system)
    _seed(
        db,
        system,
        [a, b],
        [
            _rel(system, a, b, RelationshipType.CALLS, evidence=ev),
            _rel(system, b, a, RelationshipType.CALLS, evidence=ev),
        ],
        [ev],
    )

    with db.unit_of_work() as uow:
        result = reverse_impact(uow, system_id=system.id, entity_id=a.id)

    assert {e.id for e in result.affected_entities} == {b.id}


def test_unknown_entity_raises(db: MemoryDatabase, system: System) -> None:
    with db.unit_of_work() as uow, pytest.raises(KeyError):
        reverse_impact(uow, system_id=system.id, entity_id="ent_nonexistent")


# --- explainable paths -------------------------------------------------------


def test_path_carries_the_full_relationship_and_resolved_evidence(
    db: MemoryDatabase, system: System
) -> None:
    a = _entity(system, "a")
    b = _entity(system, "b")
    ev = _evidence(system)
    rel = _rel(system, a, b, RelationshipType.CALLS, evidence=ev, confidence=Confidence.LIKELY)
    _seed(db, system, [a, b], [rel], [ev])

    with db.unit_of_work() as uow:
        result = reverse_impact(uow, system_id=system.id, entity_id=b.id)

    assert len(result.paths) == 1
    path = result.paths[0]
    assert len(path.hops) == 1
    hop = path.hops[0]
    assert hop.relationship.id == rel.id
    assert hop.relationship.type is RelationshipType.CALLS
    assert hop.relationship.confidence is Confidence.LIKELY
    assert hop.source.id == a.id
    assert hop.target.id == b.id
    assert len(hop.evidence) == 1
    assert hop.evidence[0].id == ev.id
    assert path.endpoint.id == a.id


def test_multi_hop_path_preserves_order_start_to_endpoint(
    db: MemoryDatabase, system: System
) -> None:
    a = _entity(system, "a")
    b = _entity(system, "b")
    c = _entity(system, "c")
    ev = _evidence(system)
    _seed(
        db,
        system,
        [a, b, c],
        [
            _rel(system, a, b, RelationshipType.CALLS, evidence=ev),
            _rel(system, b, c, RelationshipType.WRITES, evidence=ev),
        ],
        [ev],
    )

    with db.unit_of_work() as uow:
        result = reverse_impact(uow, system_id=system.id, entity_id=c.id)

    # Every reached entity gets its own path -- b (one hop) and a (two
    # hops) are both "affected", not just the deepest one.
    assert {p.endpoint.id for p in result.paths} == {a.id, b.id}
    path_to_a = next(p for p in result.paths if p.endpoint.id == a.id)
    assert [hop.relationship.type for hop in path_to_a.hops] == [
        RelationshipType.WRITES,
        RelationshipType.CALLS,
    ]


# --- path confidence is per-hop, never a synthesized path score -------------


def test_weakest_confidence_is_the_minimum_hop_not_a_blended_score(
    db: MemoryDatabase, system: System
) -> None:
    a = _entity(system, "a")
    b = _entity(system, "b")
    c = _entity(system, "c")
    ev = _evidence(system)
    _seed(
        db,
        system,
        [a, b, c],
        [
            _rel(system, a, b, RelationshipType.CALLS, evidence=ev, confidence=Confidence.CERTAIN),
            _rel(
                system,
                b,
                c,
                RelationshipType.DEPENDS_ON,
                evidence=ev,
                confidence=Confidence.SPECULATIVE,
                knowledge_class=KnowledgeClass.HYPOTHESIS,
                origin=Origin.LLM,
            ),
        ],
        [ev],
    )

    with db.unit_of_work() as uow:
        result = reverse_impact(uow, system_id=system.id, entity_id=c.id)

    path_to_a = next(p for p in result.paths if p.endpoint.id == a.id)
    # Each hop keeps its own confidence -- CERTAIN and SPECULATIVE both
    # still readable individually.
    assert [hop.relationship.confidence for hop in path_to_a.hops] == [
        Confidence.SPECULATIVE,
        Confidence.CERTAIN,
    ]
    assert path_to_a.weakest_confidence is Confidence.SPECULATIVE
    assert path_to_a.has_speculative_hop is True

    path_to_b = next(p for p in result.paths if p.endpoint.id == b.id)
    assert path_to_b.has_speculative_hop is True  # the one hop itself is speculative


def test_a_path_with_no_speculative_hops_is_not_flagged(db: MemoryDatabase, system: System) -> None:
    a = _entity(system, "a")
    b = _entity(system, "b")
    ev = _evidence(system)
    _seed(
        db,
        system,
        [a, b],
        [_rel(system, a, b, RelationshipType.CALLS, evidence=ev, confidence=Confidence.LIKELY)],
        [ev],
    )

    with db.unit_of_work() as uow:
        result = reverse_impact(uow, system_id=system.id, entity_id=b.id)

    assert result.paths[0].has_speculative_hop is False


# --- identity-lineage-aware resolution --------------------------------------


def test_resolve_identity_returns_the_entity_itself_when_present(
    db: MemoryDatabase, system: System
) -> None:
    a = _entity(system, "a")
    _seed(db, system, [a], [])
    with db.unit_of_work() as uow:
        graph = query_at_revision(uow, system_id=system.id, revision=None)
    assert resolve_identity(graph, a.id) is not None
    assert resolve_identity(graph, a.id).id == a.id  # type: ignore[union-attr]


def test_resolve_identity_follows_supersedes_lineage(db: MemoryDatabase, system: System) -> None:
    old = _entity(system, "old").model_copy(
        update={
            "status": EntityStatus.SUPERSEDED,
            "identity_claims": [],
        }
    )
    # A SUPERSEDED entity must carry the claims that justified it (§10);
    # give it one so construction succeeds.
    from hashira.core import IdentityClaim, IdentityClaimKind

    old = old.model_copy(
        update={
            "identity_claims": [
                IdentityClaim(
                    kind=IdentityClaimKind.GIT_RENAME,
                    value="old->new",
                    origin=Origin.GIT,
                    confidence=Confidence.LIKELY,
                )
            ]
        }
    )
    new = _entity(system, "new")
    lineage = Relationship(
        system_id=system.id,
        source_entity_id=new.id,
        target_entity_id=old.id,
        type=RelationshipType.SUPERSEDES,
        origin=Origin.DERIVED,
        knowledge_class=KnowledgeClass.DERIVATION,
        valid_from_revision="rev2",
    )
    _seed(db, system, [old, new], [lineage])

    with db.unit_of_work() as uow:
        graph = query_at_revision(uow, system_id=system.id, revision=None)
        resolved = resolve_identity(graph, old.id)
        assert resolved is not None
        assert resolved.id == new.id

        # And reverse_impact/forward_impact transparently follow it too.
        result = reverse_impact(uow, system_id=system.id, entity_id=old.id)
        assert result.resolved_from == old.id
        assert result.start.id == new.id


def test_resolve_identity_returns_none_for_a_dead_end(db: MemoryDatabase, system: System) -> None:
    with db.unit_of_work() as uow:
        graph = query_at_revision(uow, system_id=system.id, revision=None)
    assert resolve_identity(graph, "ent_nowhere") is None


# --- the ImpactAnalyzer convenience wrapper ---------------------------------


def test_impact_analyzer_delegates_to_the_module_functions(
    db: MemoryDatabase, system: System
) -> None:
    a = _entity(system, "a")
    b = _entity(system, "b")
    ev = _evidence(system)
    _seed(db, system, [a, b], [_rel(system, a, b, RelationshipType.CALLS, evidence=ev)], [ev])

    with db.unit_of_work() as uow:
        analyzer = ImpactAnalyzer(uow=uow, system_id=system.id)
        reverse = analyzer.reverse_impact(b.id)
        forward = analyzer.forward_impact(a.id)

    assert {e.id for e in reverse.affected_entities} == {a.id}
    assert {e.id for e in forward.affected_entities} == {b.id}


# --- follow_lineage ----------------------------------------------------------


def test_follow_lineage_walks_both_directions_of_a_chain(
    db: MemoryDatabase, system: System
) -> None:
    """a -> (renamed to) -> b -> (renamed to) -> c, asked about the middle
    generation: one predecessor, one successor, each with its own edge and
    evidence."""
    a = _superseded_entity(system, "a")
    b = _superseded_entity(system, "b")
    c = _entity(system, "c")
    ev_ab = _evidence(system)
    ev_bc = _evidence(system)
    _seed(
        db,
        system,
        [a, b, c],
        [_supersedes(system, b, a, evidence=ev_ab), _supersedes(system, c, b, evidence=ev_bc)],
        [ev_ab, ev_bc],
    )

    with db.unit_of_work() as uow:
        result = follow_lineage(uow, system_id=system.id, entity_id=b.id)

    assert result.start.id == b.id
    assert len(result.predecessors) == 1
    assert result.predecessors[0].predecessor.id == a.id
    assert result.predecessors[0].successor.id == b.id
    assert [e.id for e in result.predecessors[0].evidence] == [ev_ab.id]

    assert len(result.successors) == 1
    assert result.successors[0].predecessor.id == b.id
    assert result.successors[0].successor.id == c.id
    assert [e.id for e in result.successors[0].evidence] == [ev_bc.id]


def test_follow_lineage_from_the_oldest_generation_has_no_predecessors(
    db: MemoryDatabase, system: System
) -> None:
    a = _superseded_entity(system, "a")
    b = _entity(system, "b")
    _seed(db, system, [a, b], [_supersedes(system, b, a)])

    with db.unit_of_work() as uow:
        result = follow_lineage(uow, system_id=system.id, entity_id=a.id)

    assert result.predecessors == ()
    assert len(result.successors) == 1
    assert result.successors[0].successor.id == b.id


def test_follow_lineage_from_the_newest_generation_has_no_successors(
    db: MemoryDatabase, system: System
) -> None:
    a = _superseded_entity(system, "a")
    b = _entity(system, "b")
    _seed(db, system, [a, b], [_supersedes(system, b, a)])

    with db.unit_of_work() as uow:
        result = follow_lineage(uow, system_id=system.id, entity_id=b.id)

    assert result.successors == ()
    assert len(result.predecessors) == 1
    assert result.predecessors[0].predecessor.id == a.id


def test_follow_lineage_with_no_lineage_at_all_returns_empty_both_ways(
    db: MemoryDatabase, system: System
) -> None:
    a = _entity(system, "a")
    _seed(db, system, [a], [])

    with db.unit_of_work() as uow:
        result = follow_lineage(uow, system_id=system.id, entity_id=a.id)

    assert result.start.id == a.id
    assert result.predecessors == ()
    assert result.successors == ()


def test_follow_lineage_stops_at_a_fork_rather_than_guessing(
    db: MemoryDatabase, system: System
) -> None:
    """Two entities both claim to supersede the same predecessor -- which
    one is the "real" lineage is genuinely ambiguous, so the walk in that
    direction stops rather than picking one, the same "do not guess"
    discipline `identity.resolve` applies to a tied resolution decision."""
    a = _superseded_entity(system, "a")
    b = _entity(system, "b")
    c = _entity(system, "c")
    _seed(db, system, [a, b, c], [_supersedes(system, b, a), _supersedes(system, c, a)])

    with db.unit_of_work() as uow:
        result = follow_lineage(uow, system_id=system.id, entity_id=a.id)

    assert result.successors == ()


def test_follow_lineage_unknown_entity_raises(db: MemoryDatabase, system: System) -> None:
    with db.unit_of_work() as uow, pytest.raises(KeyError):
        follow_lineage(uow, system_id=system.id, entity_id="ent_nonexistent")


def test_impact_analyzer_delegates_follow_lineage_too(db: MemoryDatabase, system: System) -> None:
    a = _superseded_entity(system, "a")
    b = _entity(system, "b")
    _seed(db, system, [a, b], [_supersedes(system, b, a)])

    with db.unit_of_work() as uow:
        analyzer = ImpactAnalyzer(uow=uow, system_id=system.id)
        result = analyzer.follow_lineage(b.id)

    assert len(result.predecessors) == 1
    assert result.predecessors[0].predecessor.id == a.id


# --- coverage (Impact Analysis v0.2/v0.3) -------------------------------------


def test_coverage_is_complete_with_no_snapshot_and_no_limitation_metadata(
    db: MemoryDatabase, system: System
) -> None:
    """No snapshot recorded at all (as in every other test in this file,
    which never index anything), no `coverage_limitation_kinds` on the
    start entity -- coverage has nothing to report and reads COMPLETE."""
    a = _entity(system, "a")
    b = _entity(system, "b")
    ev = _evidence(system)
    _seed(db, system, [a, b], [_rel(system, a, b, RelationshipType.CALLS, evidence=ev)], [ev])

    with db.unit_of_work() as uow:
        result = reverse_impact(uow, system_id=system.id, entity_id=b.id)

    assert result.coverage.status is CoverageStatus.COMPLETE
    assert result.coverage.limitations == ()


def test_coverage_reflects_limitation_kinds_on_the_start_entity(
    db: MemoryDatabase, system: System
) -> None:
    field = _entity(system, "field").model_copy(
        update={"metadata": {"coverage_limitation_kinds": ["RETURN_VALUE_PROVENANCE"]}}
    )
    caller = _entity(system, "caller")
    ev = _evidence(system)
    _seed(
        db,
        system,
        [caller, field],
        [_rel(system, caller, field, RelationshipType.WRITES, evidence=ev)],
        [ev],
    )

    with db.unit_of_work() as uow:
        result = reverse_impact(uow, system_id=system.id, entity_id=field.id)

    assert result.coverage.status is CoverageStatus.PARTIAL
    assert len(result.coverage.limitations) == 1
    limitation = result.coverage.limitations[0]
    assert limitation.kind is LimitationKind.RETURN_VALUE_PROVENANCE
    assert limitation.scope is LimitationScope.ORM_ATTRIBUTE_ACCESS
    assert limitation.detail


def test_coverage_ignores_unknown_limitation_kind_strings(
    db: MemoryDatabase, system: System
) -> None:
    """`metadata` is an untyped `dict[str, object]` boundary -- a kind
    string this build of Hashira does not recognize (e.g. written by a
    newer version) is skipped, not raised."""
    field = _entity(system, "field").model_copy(
        update={"metadata": {"coverage_limitation_kinds": ["SOME_FUTURE_KIND"]}}
    )
    ev = _evidence(system)
    _seed(db, system, [field], [], [ev])

    with db.unit_of_work() as uow:
        result = reverse_impact(uow, system_id=system.id, entity_id=field.id)

    assert result.coverage.status is CoverageStatus.COMPLETE
    assert result.coverage.limitations == ()


def test_coverage_reads_structural_limitations_off_the_latest_complete_snapshot(
    db: MemoryDatabase, system: System
) -> None:
    a = _entity(system, "a")
    b = _entity(system, "b")
    ev = _evidence(system)
    _seed(db, system, [a, b], [_rel(system, a, b, RelationshipType.CALLS, evidence=ev)], [ev])
    raw_sql = Limitation(
        kind=LimitationKind.RAW_SQL,
        scope=LimitationScope.RAW_SQL_REFERENCES,
        detail="raw SQL is not analyzed",
    )
    with db.unit_of_work() as uow:
        uow.snapshots.create(
            Snapshot(
                system_id=system.id,
                revision="rev1",
                indexing_version="0.1.0",
                ir_version="0.1.5",
                status=SnapshotStatus.COMPLETE,
                diagnostics=[
                    f"limitation: {raw_sql.model_dump_json()}",
                    "not a limitation, some other diagnostic",
                ],
            )
        )
        uow.commit()

    with db.unit_of_work() as uow:
        result = reverse_impact(uow, system_id=system.id, entity_id=b.id)

    assert result.coverage.limitations == (raw_sql,)
    assert result.coverage.status is CoverageStatus.PARTIAL


def test_coverage_never_changes_a_hop_s_confidence(db: MemoryDatabase, system: System) -> None:
    """The invariant `docs/IR.md` states explicitly: coverage is reported
    alongside a result, never folded into `Confidence` -- a CERTAIN edge
    stays CERTAIN even when `coverage.status` is PARTIAL."""
    field = _entity(system, "field").model_copy(
        update={"metadata": {"coverage_limitation_kinds": ["UNTYPED_PARAMETER"]}}
    )
    caller = _entity(system, "caller")
    ev = _evidence(system)
    rel = _rel(
        system, caller, field, RelationshipType.WRITES, evidence=ev, confidence=Confidence.CERTAIN
    )
    _seed(db, system, [caller, field], [rel], [ev])

    with db.unit_of_work() as uow:
        result = reverse_impact(uow, system_id=system.id, entity_id=field.id)

    assert result.coverage.status is CoverageStatus.PARTIAL
    assert result.paths[0].hops[0].relationship.confidence is Confidence.CERTAIN
