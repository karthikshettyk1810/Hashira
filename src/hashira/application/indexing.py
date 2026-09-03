"""The indexing pipeline: adapters → normalization → identity → storage.

This is the only place those pieces meet. An adapter never touches a
`UnitOfWork`; `hashira.identity` never touches storage; storage never parses
source. Wiring them together — and giving the whole run the §30 transactional
guarantee — is this module's entire job.

```
LanguageAdapter.extract()  ->  Observation[] + Evidence[]      (pure, no I/O)
Normalizer (injected)      ->  candidate Entity[]/Relationship[]  (pure)
identity.resolve()/apply() ->  MATCHED/SUPERSEDES/NEW/AMBIGUOUS   (pure)
UnitOfWork                 ->  everything committed as one transaction
```

Extraction and normalization happen *before* the transaction opens — they are
pure CPU work with no reason to hold a database connection. Only identity
resolution and persistence run inside the `with uow:` block, so a crash
anywhere in that block leaves the last known-good snapshot exactly as it was.

This service is deliberately ignorant of which *language* it is indexing:
`Normalizer` below is a structural Protocol, not an import of
`hashira.adapters.python.normalizer.normalize`. A caller wires a specific
adapter and its matching normalizer together (see
`tests/integration/test_python_indexing.py`); nothing here hardcodes Python.
The day a second language adapter exists, this file does not change.

## What this does not do, on purpose

- **No incremental indexing.** Every call is a full re-index of the given
  root (ROADMAP.md Phase 2). Nothing skips a file because it did not change.
- **No removal detection.** An entity with no candidate in this run is left
  untouched, never marked `REMOVED` — safely diffing against a prior run
  needs more than this pass does yet.
- **No stale-relationship retraction.** A relationship not re-observed this
  run is never closed via `Relationship.close()`; only new edges are added.
  Combined with the point below, a renamed symbol leaves its *old* CALLS/
  IMPORTS/EXTENDS edges sitting there as `is_current` alongside the new ones.
- **Consequently: re-indexing anything unchanged produces a new `SUPERSEDES`
  link, every time, for every entity.** `QUALIFIED_NAME` is the only identity
  signal pure-AST analysis can offer (see `adapters/python/normalizer.py`),
  and it is deliberately not strong enough alone to justify `MATCHED` — so an
  unchanged symbol, re-indexed on revision N+1, correctly cannot be
  distinguished from "this symbol was replaced by a coincidentally identical
  one." The ladder's refusal to guess is correct; the compounding cost is
  real. All four items above close together once a Git adapter supplies
  rename/no-change evidence and incremental indexing lands (ROADMAP.md).

None of this is unsafe — nothing is silently merged, overwritten, or deleted.
It is conservative to the point of leaving real cleanup work for Phase 2,
which is the trade this pass deliberately makes.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .. import identity
from ..core.entities import Entity
from ..core.enums import SnapshotStatus
from ..core.evidence import Evidence, Inference, Observation
from ..core.ids import SystemID
from ..core.relationships import Relationship
from ..core.schema import IR_VERSION
from ..core.snapshots import Snapshot, SnapshotStatistics
from ..ports.adapters import LanguageAdapter
from ..ports.repositories import UnitOfWork

__all__ = ["IndexingResult", "IndexingService", "Normalizer"]


class _NormalizedRun(Protocol):
    """Structural shape any language's Stage-2 normalizer must return.

    Matches `hashira.adapters.python.normalizer.NormalizedRun` without
    importing it — this file has no language-specific dependency (§18).
    """

    entities: list[Entity]
    relationships: list[Relationship]
    unresolved: list[Observation]


class Normalizer(Protocol):
    """Turns one run's raw Observations into candidate IR (Stage 2)."""

    def __call__(
        self, observations: Sequence[Observation], *, system_id: SystemID, revision: str | None
    ) -> _NormalizedRun: ...


_INDEXING_VERSION = "0.1.0"

#: Large enough that a v0.1 fixture-scale system never hits it. Revisit once a
#: real repository's entity count makes a full fetch-then-resolve pass costly
#: — see this module's docstring on what incremental indexing still owes.
_ALL_ENTITIES_LIMIT = 1_000_000


@dataclass(frozen=True, slots=True)
class IndexingResult:
    snapshot: Snapshot
    entities_upserted: int
    relationships_upserted: int
    ambiguous_count: int
    unresolved_observation_count: int
    files_processed: int
    errors: list[str] = field(default_factory=list)


class IndexingService:
    """Runs every configured language adapter over one root and persists the
    result as a snapshot, resolving identity against whatever the system
    already knows."""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        adapters: Sequence[LanguageAdapter],
        normalize: Normalizer,
    ) -> None:
        self._uow_factory = uow_factory
        self._adapters = adapters
        self._normalize = normalize

    def index(self, root: Path, *, system_id: SystemID, revision: str | None) -> IndexingResult:
        all_observations: list[Observation] = []
        all_evidence: list[Evidence] = []
        errors: list[str] = []
        files_processed = 0

        for adapter in self._adapters:
            files = list(adapter.owned_files(root))
            files_processed += len(files)
            extraction = adapter.extract(root, files, system_id=system_id, revision=revision)
            all_observations.extend(extraction.observations)
            all_evidence.extend(extraction.evidence)
            errors.extend(extraction.errors)

        run = self._normalize(all_observations, system_id=system_id, revision=revision)

        with self._uow_factory() as uow:
            snapshot = self._resolve_and_persist(
                uow,
                system_id=system_id,
                revision=revision,
                candidates=run.entities,
                structural_relationships=run.relationships,
                observations=all_observations,
                evidence=all_evidence,
                unresolved_count=len(run.unresolved),
                files_processed=files_processed,
                errors=errors,
            )
            uow.commit()

        return IndexingResult(
            snapshot=snapshot,
            entities_upserted=snapshot.statistics.entity_count,
            relationships_upserted=snapshot.statistics.relationship_count,
            ambiguous_count=snapshot.statistics.inference_count,
            unresolved_observation_count=len(run.unresolved),
            files_processed=files_processed,
            errors=errors,
        )

    def _resolve_and_persist(
        self,
        uow: UnitOfWork,
        *,
        system_id: SystemID,
        revision: str | None,
        candidates: list[Entity],
        structural_relationships: list[Relationship],
        observations: list[Observation],
        evidence: list[Evidence],
        unresolved_count: int,
        files_processed: int,
        errors: list[str],
    ) -> Snapshot:
        parent = uow.snapshots.latest_complete(system_id)
        snapshot = uow.snapshots.create(
            Snapshot(
                system_id=system_id,
                revision=revision or "unknown",
                parent_snapshot_id=parent.id if parent else None,
                indexing_version=_INDEXING_VERSION,
                ir_version=IR_VERSION,
                status=SnapshotStatus.BUILDING,
            )
        )

        existing_by_id = {
            e.id: e for e in uow.graph.find_entities(system_id, limit=_ALL_ENTITIES_LIMIT)
        }
        id_remap: dict[str, str] = {}
        resolved_entities: list[Entity] = []
        superseded_entities: list[Entity] = []
        lineage_relationships: list[Relationship] = []
        ambiguity_inferences: list[Inference] = []

        for candidate in candidates:
            pool = list(existing_by_id.values())
            decision = identity.resolve(candidate, existing=pool)
            outcome = identity.apply(candidate, decision, existing=pool, revision=revision)

            id_remap[candidate.id] = outcome.entity.id
            resolved_entities.append(outcome.entity)
            existing_by_id[outcome.entity.id] = outcome.entity

            if outcome.superseded is not None:
                superseded_entities.append(outcome.superseded)
                existing_by_id[outcome.superseded.id] = outcome.superseded
            if outcome.relationship is not None:
                lineage_relationships.append(outcome.relationship)
            if outcome.inference is not None:
                ambiguity_inferences.append(outcome.inference)

        def remap(entity_id: str) -> str:
            return id_remap.get(entity_id, entity_id)

        final_relationships = [
            rel.model_copy(
                update={
                    "source_entity_id": remap(rel.source_entity_id),
                    "target_entity_id": remap(rel.target_entity_id),
                }
            )
            for rel in structural_relationships
        ] + lineage_relationships

        uow.observations.record(observations)
        uow.evidence.record(evidence)
        uow.graph.upsert_entities([*resolved_entities, *superseded_entities])
        uow.graph.upsert_relationships(final_relationships)
        for inference in ambiguity_inferences:
            uow.inferences.save(inference)

        completed = snapshot.model_copy(
            update={
                "status": SnapshotStatus.COMPLETE,
                "statistics": SnapshotStatistics(
                    entity_count=len(resolved_entities),
                    relationship_count=len(final_relationships),
                    observation_count=len(observations),
                    evidence_count=len(evidence),
                    inference_count=len(ambiguity_inferences),
                    files_processed=files_processed,
                    adapters_failed=errors,
                ),
            }
        )
        uow.snapshots.update(completed)
        return completed
