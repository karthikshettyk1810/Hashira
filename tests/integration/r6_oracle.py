"""R6 Oracle Manifest & Semantic Equivalence Comparator (§13, R6 Protocol).

Extracts and verifies machine-comparable ground truth manifests for every
evolution commit (C0..C8), enforcing semantic equality over storage-dependent
byte representations.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from hashira.application.history import HistoricalGraph, query_at_revision
from hashira.core import RelationshipType
from hashira.core.ids import SystemID
from hashira.ports.repositories import UnitOfWork


@dataclass(frozen=True, slots=True)
class IdentityClaimOracle:
    kind: str
    value: str
    origin: str
    confidence: str


@dataclass(frozen=True, slots=True)
class EntityOracle:
    qualified_name: str
    name: str
    type: str
    identity_claims: list[IdentityClaimOracle] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RelationshipOracle:
    source_qname: str
    target_qname: str
    type: str
    confidence: str


@dataclass(frozen=True, slots=True)
class LineageOracle:
    source_qname: str
    target_qname: str
    valid_from: str | None


@dataclass(frozen=True, slots=True)
class GraphOracleManifest:
    stage: str
    revision: str
    entity_count: int
    relationship_count: int
    entities: dict[str, EntityOracle]
    relationships: list[RelationshipOracle]
    lineages: list[LineageOracle]
    evidence_locations: dict[str, list[str]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GraphOracleManifest:
        entities = {
            k: EntityOracle(
                qualified_name=v["qualified_name"],
                name=v["name"],
                type=v["type"],
                identity_claims=[
                    IdentityClaimOracle(**c) for c in v.get("identity_claims", [])
                ],
            )
            for k, v in data["entities"].items()
        }
        relationships = [
            RelationshipOracle(**r) for r in data["relationships"]
        ]
        lineages = [
            LineageOracle(**item) for item in data.get("lineages", [])
        ]
        return cls(
            stage=data["stage"],
            revision=data["revision"],
            entity_count=data["entity_count"],
            relationship_count=data["relationship_count"],
            entities=entities,
            relationships=relationships,
            lineages=lineages,
            evidence_locations=data.get("evidence_locations", {}),
        )


@dataclass(frozen=True, slots=True)
class SemanticEquivalenceResult:
    equivalent: bool
    missing_entities: list[str] = field(default_factory=list)
    unexpected_entities: list[str] = field(default_factory=list)
    mismatched_entity_types: list[str] = field(default_factory=list)
    missing_relationships: list[str] = field(default_factory=list)
    unexpected_relationships: list[str] = field(default_factory=list)
    mismatched_confidences: list[str] = field(default_factory=list)
    missing_lineages: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)


def extract_oracle_manifest(
    uow: UnitOfWork, *, system_id: SystemID, revision: str, stage: str
) -> GraphOracleManifest:
    """Extract a canonical, storage-agnostic ground truth oracle manifest
    from a clean full index of `revision`."""
    graph = query_at_revision(uow, system_id=system_id, revision=revision)
    id_to_qname = {e.id: e.qualified_name for e in graph.entities}

    # Extract all entities in database for resolving lineage targets that may be historical
    all_known_entities: dict[str, str] = {
        e.id: (e.qualified_name or e.name)
        for e in uow.graph.find_entities(system_id, limit=1_000_000)
    }

    entities: dict[str, EntityOracle] = {}
    evidence_locations: dict[str, list[str]] = {}

    for e in sorted(graph.entities, key=lambda x: x.qualified_name or x.name):
        qname = e.qualified_name or e.name
        claims = [
            IdentityClaimOracle(
                kind=c.kind.value,
                value=c.value,
                origin=c.origin.value,
                confidence=c.confidence.value,
            )
            for c in e.identity_claims
        ]
        entities[qname] = EntityOracle(
            qualified_name=qname,
            name=e.name,
            type=e.type.value,
            identity_claims=claims,
        )
        if e.source and e.source.file:
            loc = f"{e.source.file}:{e.source.line_start or 0}"
            evidence_locations.setdefault(qname, []).append(loc)

    relationships: list[RelationshipOracle] = []
    lineages: list[LineageOracle] = []

    for rel in sorted(
        graph.relationships,
        key=lambda r: (
            id_to_qname.get(r.source_entity_id, ""),
            id_to_qname.get(r.target_entity_id, all_known_entities.get(r.target_entity_id, "")),
            r.type.value,
        ),
    ):
        src_qname = id_to_qname.get(rel.source_entity_id)
        tgt_qname = id_to_qname.get(rel.target_entity_id) or all_known_entities.get(
            rel.target_entity_id
        )
        if not src_qname or not tgt_qname:
            continue

        conf_str = rel.confidence.value if rel.confidence is not None else "CERTAIN"
        if rel.type is RelationshipType.SUPERSEDES:
            lineages.append(
                LineageOracle(
                    source_qname=src_qname,
                    target_qname=tgt_qname,
                    valid_from=rel.valid_from_revision,
                )
            )
        else:
            relationships.append(
                RelationshipOracle(
                    source_qname=src_qname,
                    target_qname=tgt_qname,
                    type=rel.type.value,
                    confidence=conf_str,
                )
            )

    return GraphOracleManifest(
        stage=stage,
        revision=revision,
        entity_count=len(entities),
        relationship_count=len(relationships),
        entities=entities,
        relationships=relationships,
        lineages=lineages,
        evidence_locations=evidence_locations,
    )


def compare_semantic_equivalence(
    actual: HistoricalGraph | GraphOracleManifest,
    expected: GraphOracleManifest,
) -> SemanticEquivalenceResult:
    """Compare an actual graph or manifest against the expected oracle manifest
    using semantic equality (qualified names, entity types, relationship types,
    and confidences) independent of internal IDs or storage ordering."""
    actual_entities: dict[str, EntityOracle]
    actual_relationships: list[RelationshipOracle]

    if isinstance(actual, HistoricalGraph):
        id_to_qname = {e.id: (e.qualified_name or e.name) for e in actual.entities}
        actual_entities = {
            (e.qualified_name or e.name): EntityOracle(
                qualified_name=(e.qualified_name or e.name),
                name=e.name,
                type=e.type.value,
            )
            for e in actual.entities
        }
        actual_relationships = []
        for r in actual.relationships:
            if r.type is RelationshipType.SUPERSEDES:
                continue
            src = id_to_qname.get(r.source_entity_id)
            tgt = id_to_qname.get(r.target_entity_id)
            if src and tgt:
                conf = r.confidence.value if r.confidence is not None else "CERTAIN"
                actual_relationships.append(
                    RelationshipOracle(
                        source_qname=src,
                        target_qname=tgt,
                        type=r.type.value,
                        confidence=conf,
                    )
                )
    else:
        actual_entities = actual.entities
        actual_relationships = actual.relationships

    # 1. Entity Set Comparison
    act_ent_keys = set(actual_entities.keys())
    exp_ent_keys = set(expected.entities.keys())
    missing_entities = sorted(exp_ent_keys - act_ent_keys)
    unexpected_entities = sorted(act_ent_keys - exp_ent_keys)

    mismatched_types: list[str] = []
    for common_k in act_ent_keys & exp_ent_keys:
        act_t = actual_entities[common_k].type
        exp_t = expected.entities[common_k].type
        if act_t != exp_t:
            mismatched_types.append(f"{common_k}: expected {exp_t}, got {act_t}")

    # 2. Relationship Set Comparison
    act_rel_set = {
        (r.source_qname, r.target_qname, r.type, r.confidence)
        for r in actual_relationships
    }
    exp_rel_set = {
        (r.source_qname, r.target_qname, r.type, r.confidence)
        for r in expected.relationships
    }

    missing_rels = [
        f"({s} -> {t} [{rel_type}] {conf})"
        for (s, t, rel_type, conf) in sorted(exp_rel_set - act_rel_set)
    ]
    unexpected_rels = [
        f"({s} -> {t} [{rel_type}] {conf})"
        for (s, t, rel_type, conf) in sorted(act_rel_set - exp_rel_set)
    ]

    is_equiv = (
        not missing_entities
        and not unexpected_entities
        and not mismatched_types
        and not missing_rels
        and not unexpected_rels
    )

    details: list[str] = []
    if not is_equiv:
        if missing_entities:
            details.append(f"Missing entities: {missing_entities}")
        if unexpected_entities:
            details.append(f"Unexpected entities: {unexpected_entities}")
        if mismatched_types:
            details.append(f"Mismatched types: {mismatched_types}")
        if missing_rels:
            details.append(f"Missing relationships: {missing_rels}")
        if unexpected_rels:
            details.append(f"Unexpected relationships: {unexpected_rels}")

    return SemanticEquivalenceResult(
        equivalent=is_equiv,
        missing_entities=missing_entities,
        unexpected_entities=unexpected_entities,
        mismatched_entity_types=mismatched_types,
        missing_relationships=missing_rels,
        unexpected_relationships=unexpected_rels,
        details=details,
    )


def save_oracle_manifests(manifests: dict[str, GraphOracleManifest], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for stage, manifest in manifests.items():
        dest = output_dir / f"oracle_{stage.lower()}.json"
        dest.write_text(manifest.to_json())
