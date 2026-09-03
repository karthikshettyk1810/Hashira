"""Stage 2: whole-run linking. Observations in, candidate IR out.

Stage 1 (`extractor.py`) sees one file and guesses, syntactically, what a call,
base class, or import might refer to. This stage is what actually *checks* a
guess — against every entity produced by every file in this run — before it is
allowed to become a `Relationship`. A guess that cannot be confirmed here stays
exactly what it was: an `Observation`, not a fact (spec §10's "uncertainty is
data, not failure", stated as a hard rule for this adapter).

What this stage does **not** do: identity resolution against previously
indexed entities. Every `Entity` returned here is a *candidate* with a freshly
minted id and a `QUALIFIED_NAME` identity claim; deciding whether it is new,
a match, or supersedes something already in the graph is `hashira.identity`'s
job, run by the caller (`hashira.application.indexing`) after this stage.

## A known, deliberate limitation

`QUALIFIED_NAME` is the *only* identity signal pure-AST analysis can honestly
produce — Python's `ast` module gives no stable cross-revision symbol id (that
needs LSP/SCIP), and this stage never talks to Git. Per the identity ladder's
own policy (`hashira.identity.resolver`), a single corroborating-tier signal
justifies `SUPERSEDES` (lineage preserved) but never an outright `MATCHED`
merge — deliberately, because a qualified name can be reused by coincidence.

The consequence, precisely (confirmed by
`tests/integration/test_python_indexing.py`'s adversarial suite, not assumed):

- **Re-indexing an unchanged file resolves as `SUPERSEDES`, not `MATCHED`.**
  The qualified name matches, but on its own that is only ever a
  corroborating-tier signal — never enough alone for an outright merge.
- **A rename resolves as plain `NEW`, with *no* lineage at all**, and the old
  entity is simply left orphaned (still `ACTIVE`; see
  `hashira.application.indexing`'s own documented gap on removal detection).
  The renamed candidate and the old entity no longer share *any* claim value
  — the one signal available changed along with the name — so there is
  nothing left to corroborate a connection on, and the ladder correctly does
  not invent one. This is more conservative than "falls back to `SUPERSEDES`
  instead of `MATCHED`"; it is "no connection recorded at all" until a Git
  adapter supplies real rename evidence (a `GIT_RENAME` claim) or a future
  language-server integration supplies a true `SYMBOL_ID`.

Neither is a bug in the ladder or a gap in this adapter's extraction — it is
the ladder correctly refusing to guess with the evidence honestly available
today. See `tests/fixtures/python_basic/` and its adversarial identity suite
for the exact scenarios this produces and why they are still safe (nothing is
silently overwritten, merged, or deleted — just less connected than it could
be with better evidence).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ...core.base import SourceLocation, TechnologyInfo
from ...core.entities import Entity, IdentityClaim
from ...core.enums import Confidence, EntityType, IdentityClaimKind, Origin, RelationshipType
from ...core.evidence import Observation
from ...core.ids import SystemID
from ...core.relationships import Relationship

__all__ = ["NormalizedRun", "normalize"]


@dataclass(frozen=True, slots=True)
class NormalizedRun:
    entities: list[Entity]
    relationships: list[Relationship]
    unresolved: list[Observation]
    """Observations that named a plausible target this run could not confirm
    — never promoted to a Relationship, kept for diagnostics/review."""


def _qualified_name_claim(qualified_name: str) -> IdentityClaim:
    return IdentityClaim(
        kind=IdentityClaimKind.QUALIFIED_NAME,
        value=qualified_name,
        origin=Origin.PARSER,
        confidence=Confidence.CERTAIN,
    )


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
        identity_claims=[_qualified_name_claim(qn)],
        first_seen_revision=revision,
        last_seen_revision=revision,
    )


def _symbol_entity(obs: Observation, *, system_id: SystemID, revision: str | None) -> Entity:
    qn = str(obs.payload["qualified_name"])
    return Entity(
        system_id=system_id,
        type=EntityType.SYMBOL,
        name=str(obs.payload["name"]),
        qualified_name=qn,
        source=SourceLocation(
            file=str(obs.payload["file"]),
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
        identity_claims=[_qualified_name_claim(qn)],
        first_seen_revision=revision,
        last_seen_revision=revision,
    )


def normalize(
    observations: Sequence[Observation], *, system_id: SystemID, revision: str | None
) -> NormalizedRun:
    entities: list[Entity] = []
    by_qualified_name: dict[str, Entity] = {}

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

    return NormalizedRun(entities=entities, relationships=relationships, unresolved=unresolved)
