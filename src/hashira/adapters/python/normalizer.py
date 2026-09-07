"""Stage 2: whole-run linking. Observations in, candidate IR out.

Stage 1 (`extractor.py`) sees one file and guesses, syntactically, what a call,
base class, or import might refer to. This stage is what actually *checks* a
guess — against every entity produced by every file in this run — before it is
allowed to become a `Relationship`. A guess that cannot be confirmed here stays
exactly what it was: an `Observation`, not a fact (spec §10's "uncertainty is
data, not failure", stated as a hard rule for this adapter).

What this stage does **not** do: identity resolution against previously
indexed entities. Every `Entity` returned here is a *candidate* with a freshly
minted id, a `QUALIFIED_NAME` claim and a `DECLARATION_ANCHOR` claim (file +
qualified name + kind, all at once); deciding whether it is new, a match, or
supersedes something already in the graph is `hashira.identity`'s job, run by
the caller (`hashira.application.indexing`) after this stage.

## What `DECLARATION_ANCHOR` fixes, and what it deliberately does not

An earlier version of this adapter emitted only `QUALIFIED_NAME`, which is
merely corroborating-tier in the identity ladder — never enough alone for an
outright `MATCHED` merge. The adversarial suite
(`tests/integration/test_python_indexing.py`) caught the consequence: an
**unchanged file, re-indexed, minted a brand-new entity generation every
single run** (`SUPERSEDES`, never `MATCHED`) — an ordinary `hashira index`
would have gradually poisoned the graph with lineage churn before Git even
entered the picture.

`DECLARATION_ANCHOR` closes that gap: file path + qualified name + kind,
matching exactly, is strong enough to merge in place. It is not independent
of `QUALIFIED_NAME` — it is derived from it — but it is a materially narrower
claim ("the exact site a prior entity came from," not "a name I recognize
somewhere"), so it does not weaken the guard against qualified names
colliding by coincidence in an unrelated file (see `identity/resolver.py`'s
docstring for why that distinction holds).

**What it still cannot do, honestly: a rename.** The moment the file or the
name changes, `DECLARATION_ANCHOR` changes with it — there is nothing left to
match against the old entity, and the candidate resolves as plain `NEW` with
no lineage at all, leaving the old entity orphaned (still `ACTIVE`; see
`hashira.application.indexing`'s documented gap on removal detection). That is
not a bug in the ladder or a gap in this adapter's extraction — it is the
ladder correctly refusing to guess with the evidence honestly available
today. Closing it for real needs a Git adapter's `GIT_RENAME` claim (or a
future language-server integration's true `SYMBOL_ID`) — see ROADMAP.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ...core.base import SourceLocation, TechnologyInfo
from ...core.entities import Entity
from ...core.enums import EntityType, Origin, RelationshipType
from ...core.evidence import Observation
from ...core.ids import SystemID
from ...core.relationships import Relationship
from .._dedup import deduplicate_relationships
from .._identity_claims import declaration_anchor_claim, qualified_name_claim

__all__ = ["NormalizedRun", "normalize"]


@dataclass(frozen=True, slots=True)
class NormalizedRun:
    entities: list[Entity]
    relationships: list[Relationship]
    unresolved: list[Observation]
    """Observations that named a plausible target this run could not confirm
    — never promoted to a Relationship, kept for diagnostics/review."""


def _module_entity(obs: Observation, *, system_id: SystemID, revision: str | None) -> Entity:
    qn = str(obs.payload["qualified_name"])
    file = str(obs.payload["file"])
    name = qn.rsplit(".", 1)[-1] if "." in qn else qn
    return Entity(
        system_id=system_id,
        type=EntityType.MODULE,
        name=name,
        qualified_name=qn,
        source=SourceLocation(file=file, revision=revision),
        technology=TechnologyInfo(language="python"),
        identity_claims=[
            qualified_name_claim(qn),
            declaration_anchor_claim(file, qn, "module"),
        ],
        first_seen_revision=revision,
        last_seen_revision=revision,
    )


def _symbol_entity(obs: Observation, *, system_id: SystemID, revision: str | None) -> Entity:
    qn = str(obs.payload["qualified_name"])
    file = str(obs.payload["file"])
    kind = str(obs.payload["kind"])
    return Entity(
        system_id=system_id,
        type=EntityType.SYMBOL,
        name=str(obs.payload["name"]),
        qualified_name=qn,
        source=SourceLocation(
            file=file,
            revision=revision,
            line_start=int(obs.payload["line_start"]),  # type: ignore[call-overload]
            line_end=int(obs.payload["line_end"]),  # type: ignore[call-overload]
        ),
        technology=TechnologyInfo(language="python"),
        metadata={
            "kind": obs.payload["kind"],
            "parent_kind": obs.payload["parent_kind"],
            "decorators": obs.payload["decorators"],
        },
        identity_claims=[
            qualified_name_claim(qn),
            declaration_anchor_claim(file, qn, kind),
        ],
        first_seen_revision=revision,
        last_seen_revision=revision,
    )


def _retarget_through_module_instance(
    resolved_qn: str, instance_class_by_qn: dict[str, str]
) -> str | None:
    """A call Stage 1 resolved (single-file) against an *imported name*'s own
    literal target -- e.g. `kafka_publisher.push_notification` resolving to
    `common.kafka.kafka_publisher.push_notification` -- never matches a real
    entity when the imported name is actually a module-level *instance* of a
    class defined elsewhere (`kafka_publisher = KafkaEventPublisher()` in
    `common/kafka/__init__.py`), not a class/function/submodule itself.
    Stage 1 cannot know this (a different file); this stage can, from every
    `python.module_instance` observation the whole run produced. Re-targets
    through the *longest* matching instance prefix, so a nested singleton
    (rare, but possible) resolves against its own most specific binding."""
    best_prefix = ""
    for instance_qn in instance_class_by_qn:
        prefix = f"{instance_qn}."
        if resolved_qn.startswith(prefix) and len(prefix) > len(best_prefix):
            best_prefix = prefix
    if not best_prefix:
        return None
    remainder = resolved_qn[len(best_prefix) :]
    return f"{instance_class_by_qn[best_prefix[:-1]]}.{remainder}"


def normalize(
    observations: Sequence[Observation], *, system_id: SystemID, revision: str | None
) -> NormalizedRun:
    entities: list[Entity] = []
    by_qualified_name: dict[str, Entity] = {}
    instance_class_by_qn: dict[str, str] = {
        str(obs.payload["qualified_name"]): str(obs.payload["instance_of"])
        for obs in observations
        if obs.kind == "python.module_instance"
    }

    for obs in observations:
        if obs.kind == "python.module":
            entity = _module_entity(obs, system_id=system_id, revision=revision)
            entities.append(entity)
            by_qualified_name[entity.qualified_name] = entity  # type: ignore[index]

    for obs in observations:
        if obs.kind == "python.symbol":
            entity = _symbol_entity(obs, system_id=system_id, revision=revision)
            entities.append(entity)
            by_qualified_name[entity.qualified_name] = entity  # type: ignore[index]

    relationships: list[Relationship] = []
    unresolved: list[Observation] = []

    for obs in observations:
        if obs.kind == "python.symbol":
            parent_qn = str(obs.payload["parent_qualified_name"])
            child_qn = str(obs.payload["qualified_name"])
            parent = by_qualified_name.get(parent_qn)
            child = by_qualified_name.get(child_qn)
            if parent is not None and child is not None:
                relationships.append(
                    Relationship(
                        system_id=system_id,
                        source_entity_id=parent.id,
                        target_entity_id=child.id,
                        type=RelationshipType.DEFINES,
                        origin=Origin.PARSER,
                        evidence_ids=list(obs.evidence_ids),
                        valid_from_revision=revision,
                    )
                )

        elif obs.kind == "python.import":
            importer = by_qualified_name.get(str(obs.payload["importer_qualified_name"]))
            target = by_qualified_name.get(str(obs.payload["target"]))
            if importer is not None and target is not None and importer.id != target.id:
                relationships.append(
                    Relationship(
                        system_id=system_id,
                        source_entity_id=importer.id,
                        target_entity_id=target.id,
                        type=RelationshipType.IMPORTS,
                        origin=Origin.STATIC_ANALYSIS,
                        evidence_ids=list(obs.evidence_ids),
                        valid_from_revision=revision,
                    )
                )
            else:
                unresolved.append(obs)

        elif obs.kind == "python.inheritance":
            resolved_qn = obs.payload.get("resolved_qualified_name")
            subclass = by_qualified_name.get(str(obs.payload["class_qualified_name"]))
            base = by_qualified_name.get(str(resolved_qn)) if resolved_qn else None
            if subclass is not None and base is not None:
                relationships.append(
                    Relationship(
                        system_id=system_id,
                        source_entity_id=subclass.id,
                        target_entity_id=base.id,
                        type=RelationshipType.EXTENDS,
                        origin=Origin.STATIC_ANALYSIS,
                        evidence_ids=list(obs.evidence_ids),
                        valid_from_revision=revision,
                    )
                )
            else:
                unresolved.append(obs)

        elif obs.kind == "python.call":
            resolved_qn = obs.payload.get("resolved_qualified_name")
            caller = by_qualified_name.get(str(obs.payload["caller_qualified_name"]))
            callee = by_qualified_name.get(str(resolved_qn)) if resolved_qn else None
            if callee is None and resolved_qn:
                retargeted = _retarget_through_module_instance(
                    str(resolved_qn), instance_class_by_qn
                )
                if retargeted is not None:
                    callee = by_qualified_name.get(retargeted)
            if caller is not None and callee is not None and caller.id != callee.id:
                relationships.append(
                    Relationship(
                        system_id=system_id,
                        source_entity_id=caller.id,
                        target_entity_id=callee.id,
                        type=RelationshipType.CALLS,
                        origin=Origin.STATIC_ANALYSIS,
                        evidence_ids=list(obs.evidence_ids),
                        valid_from_revision=revision,
                    )
                )
            else:
                unresolved.append(obs)

    return NormalizedRun(
        entities=entities,
        relationships=deduplicate_relationships(relationships),
        unresolved=unresolved,
    )
