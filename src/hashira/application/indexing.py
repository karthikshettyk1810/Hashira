"""The indexing pipeline: adapters → normalization → identity → storage.

This is the only place those pieces meet. An adapter never touches a
`UnitOfWork`; `hashira.identity` never touches storage; storage never parses
source. Wiring them together — and giving the whole run the §30 transactional
guarantee — is this module's entire job.

```
LanguageAdapter.extract()   ->  Observation[] + Evidence[]         (pure, no I/O)
FrameworkAdapter.enrich()   ->  more Observation[]/Evidence[]      (pure, no I/O)
DataAdapter.enrich()        ->  more Observation[]/Evidence[]      (pure, no I/O)
Normalizer (injected)       ->  candidate Entity[]/Relationship[]  (pure)
identity.resolve()/apply()  ->  MATCHED/SUPERSEDES/NEW/AMBIGUOUS   (pure)
UnitOfWork                  ->  everything committed as one transaction
```

Extraction and normalization happen *before* the transaction opens — they are
pure CPU work with no reason to hold a database connection. Only identity
resolution and persistence run inside the `with uow:` block, so a crash
anywhere in that block leaves the last known-good snapshot exactly as it was.

This service is deliberately ignorant of which *language*, *framework*, or
*data layer* it is indexing: `Normalizer` below is a structural Protocol,
not an import of `hashira.adapters.python.normalizer.normalize`, and
`framework_adapters`/`data_adapters` are each just a list of `Adapter`s —
nothing here hardcodes Python, Django, or SQLAlchemy. A caller wires a
specific combination together (see `tests/integration/test_python_indexing.py`
for Python alone, `tests/integration/test_django_identity.py` for Python +
Django, `tests/integration/test_sqlalchemy_identity.py` for Python +
SQLAlchemy with *no* framework adapter at all); the day a second language,
framework, or data adapter exists, this file does not change. Framework and
data adapters both enrich what came before them — each `enrich()` call
receives everything extracted so far and returns only its own additions
(`adapters/django/adapter.py`'s module docstring explains why an enricher
must not re-derive language-level structure). Data adapters run after
framework adapters, matching the layering `ports/adapters.py::DataAdapter`
documents, but must not depend on a framework adapter having run at all —
that independence is the entire point of splitting the two kinds apart
(see `adapters/sqlalchemy/adapter.py`'s module docstring).

## A boring re-index is boring again

Two related bugs the adversarial suite caught, now fixed here:

- **Relationships are reconciled, not just appended.** `_reconcile_relationships`
  diffs this run's structural edges (DEFINES/IMPORTS/CALLS/EXTENDS) against
  whatever is already current in storage before writing anything: an edge
  that is still observed is left exactly as it was (same row, same
  `valid_from`, same evidence — not replaced), an edge no longer observed is
  closed via `Relationship.close()` (`valid_until_revision` set, never
  deleted), and only genuinely new edges are inserted. Re-indexing an
  unchanged project now writes zero new relationship rows.
- **Entities carrying `DECLARATION_ANCHOR` (file + qualified name + kind, see
  `adapters/python/normalizer.py`) now resolve as `MATCHED`, not `SUPERSEDES`,
  when nothing about their declaration site changed.** Re-indexing an
  unchanged project now produces zero new entities and zero supersessions —
  the identity ladder no longer manufactures a new entity generation on every
  run just because the only signal available was, until now, correctly too
  weak to merge on its own.

## What this still does not do, on purpose

- **No incremental indexing.** Every call is a full re-index of the given
  root (ROADMAP.md Phase 2). Nothing *skips* a file because it did not
  change — the reconciliation above makes a full re-index cheap to *persist*
  when nothing changed, not cheap to *compute*.
- **No removal detection.** An entity with no candidate in this run is left
  untouched, never marked `REMOVED`. Its *relationships* do get closed by the
  reconciliation above (they're no longer observed), but the entity itself
  is not — distinguishing "genuinely deleted" from "renamed, pending Git
  evidence" safely needs more than this pass does yet.

## Git rename evidence, now wired in

A `HistoryAdapter` (e.g. `hashira.adapters.git.GitAdapter`) is optional. When
one is configured, each run asks it what happened between the *last indexed
revision* (the previous snapshot's, if any) and this one — its commits become
`Event`s, and any rename it detected above `identity.DEFAULT_MIN_SIMILARITY`
becomes a `GIT_RENAME` claim on both the old and new entities
(`identity.attach_rename_evidence`), *before* identity resolution runs. That
claim sits at corroborating tier (`identity/resolver.py`), so a bare rename
now resolves as `SUPERSEDES`-with-lineage rather than a disconnected `NEW` —
still not an outright `MATCHED`, deliberately: a heuristic rename match is
evidence to weigh, not a fact to trust blindly.

**A rename with no configured history adapter, or one Git's own detection
doesn't surface, is unchanged**: `NEW` with no lineage, exactly as before —
this is an addition, not a replacement of the old behavior's safety.

None of this is unsafe — nothing is silently merged, overwritten, or deleted.

## Revision ancestry, now recorded

Each `git.commit` observation a history adapter reports carries that commit's
parent shas. `_extract_revisions` turns those into `core.revisions.Revision`
records and persists them via `uow.revisions.record(...)` in the same
transaction as everything else — so ancestry accumulates across runs exactly
as commits are reported (only the range between the last indexed revision and
this one, per the Git rename section above). `application.history` reads
them back to answer revision-scoped queries (`query_at_revision`); this
module itself does not query them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .. import identity
from ..core.entities import Entity
from ..core.enums import RelationshipType, SnapshotStatus
from ..core.events import Event
from ..core.evidence import Evidence, Inference, Observation
from ..core.ids import SystemID
from ..core.relationships import Relationship
from ..core.revisions import Revision
from ..core.schema import IR_VERSION
from ..core.snapshots import Snapshot, SnapshotStatistics
from ..identity import GitRename
from ..ports.adapters import (
    DataAdapter,
    ExtractionResult,
    FrameworkAdapter,
    HistoryAdapter,
    LanguageAdapter,
)
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
        history_adapters: Sequence[HistoryAdapter] = (),
        framework_adapters: Sequence[FrameworkAdapter] = (),
        data_adapters: Sequence[DataAdapter] = (),
    ) -> None:
        self._uow_factory = uow_factory
        self._adapters = adapters
        self._normalize = normalize
        self._history_adapters = history_adapters
        self._framework_adapters = framework_adapters
        self._data_adapters = data_adapters

    def index(self, root: Path, *, system_id: SystemID, revision: str | None) -> IndexingResult:
        with self._uow_factory() as uow:
            parent = uow.snapshots.latest_complete(system_id)

        all_observations: list[Observation] = []
        all_evidence: list[Evidence] = []
        all_events: list[Event] = []
        errors: list[str] = []
        files_processed = 0

        for adapter in self._adapters:
            files = list(adapter.owned_files(root))
            files_processed += len(files)
            extraction = adapter.extract(root, files, system_id=system_id, revision=revision)
            all_observations.extend(extraction.observations)
            all_evidence.extend(extraction.evidence)
            errors.extend(extraction.errors)

        # Framework adapters enrich what the language adapters already found
        # — they never re-derive it. `base` is a read-only snapshot of the
        # language-level result so far; each adapter's `enrich()` returns
        # only its own additions (see adapters/django/adapter.py), which get
        # folded into the same running lists everything else here uses.
        for framework_adapter in self._framework_adapters:
            base = ExtractionResult(
                observations=list(all_observations), evidence=list(all_evidence)
            )
            addition = framework_adapter.enrich(root, base, system_id=system_id, revision=revision)
            all_observations.extend(addition.observations)
            all_evidence.extend(addition.evidence)
            errors.extend(addition.errors)

        # Data adapters enrich the same running result, after framework
        # adapters -- but must not depend on one having run. A DataAdapter's
        # whole point is to mean the same thing whether or not any
        # framework adapter is even configured (see ports/adapters.py's
        # `DataAdapter` docstring).
        for data_adapter in self._data_adapters:
            base = ExtractionResult(
                observations=list(all_observations), evidence=list(all_evidence)
            )
            addition = data_adapter.enrich(root, base, system_id=system_id, revision=revision)
            all_observations.extend(addition.observations)
            all_evidence.extend(addition.evidence)
            errors.extend(addition.errors)

        renames: list[GitRename] = []
        revisions: list[Revision] = []
        for history_adapter in self._history_adapters:
            history = history_adapter.extract(
                root,
                system_id=system_id,
                since_revision=parent.revision if parent else None,
                until_revision=revision,
            )
            all_observations.extend(history.observations)
            all_evidence.extend(history.evidence)
            all_events.extend(history.events)
            errors.extend(history.errors)
            renames.extend(_extract_renames(history.observations))
            revisions.extend(_extract_revisions(history.observations, system_id=system_id))

        run = self._normalize(all_observations, system_id=system_id, revision=revision)

        with self._uow_factory() as uow:
            snapshot = self._resolve_and_persist(
                uow,
                system_id=system_id,
                revision=revision,
                parent=parent,
                candidates=run.entities,
                structural_relationships=run.relationships,
                observations=all_observations,
                evidence=all_evidence,
                events=all_events,
                renames=renames,
                revisions=revisions,
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
        parent: Snapshot | None,
        candidates: list[Entity],
        structural_relationships: list[Relationship],
        observations: list[Observation],
        evidence: list[Evidence],
        events: list[Event],
        renames: list[GitRename],
        revisions: list[Revision],
        unresolved_count: int,
        files_processed: int,
        errors: list[str],
    ) -> Snapshot:
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

        existing_pool = list(uow.graph.find_entities(system_id, limit=_ALL_ENTITIES_LIMIT))
        if renames:
            # Attach GIT_RENAME claims to both sides of a trusted rename
            # *before* resolution runs, so the ladder has evidence to weigh
            # (identity/git_evidence.py) — this never decides the outcome by
            # itself; it only makes the connection visible to the resolver.
            candidates, existing_pool = identity.attach_rename_evidence(
                candidates, existing_pool, renames
            )
        existing_by_id = {e.id: e for e in existing_pool}

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

        remapped_structural = [
            rel.model_copy(
                update={
                    "source_entity_id": remap(rel.source_entity_id),
                    "target_entity_id": remap(rel.target_entity_id),
                }
            )
            for rel in structural_relationships
        ]
        to_insert, stale_ids, current_count = self._reconcile_relationships(
            uow, entities=existing_by_id.values(), observed=remapped_structural
        )

        uow.observations.record(observations)
        uow.evidence.record(evidence)
        if events:
            uow.events.append_many(events)
        if revisions:
            uow.revisions.record(revisions)
        uow.graph.upsert_entities([*resolved_entities, *superseded_entities])
        uow.graph.upsert_relationships([*to_insert, *lineage_relationships])
        if stale_ids:
            uow.graph.close_relationships(stale_ids, revision=revision or "unknown")
        for inference in ambiguity_inferences:
            uow.inferences.save(inference)

        completed = snapshot.model_copy(
            update={
                "status": SnapshotStatus.COMPLETE,
                "statistics": SnapshotStatistics(
                    entity_count=len(resolved_entities),
                    relationship_count=current_count + len(lineage_relationships),
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

    @staticmethod
    def _reconcile_relationships(
        uow: UnitOfWork,
        *,
        entities: Iterable[Entity],
        observed: list[Relationship],
    ) -> tuple[list[Relationship], list[str], int]:
        """Diff this run's structural edges against whatever is already
        current in storage, so a boring re-index neither manufactures a
        duplicate row for every unchanged call nor leaves a renamed symbol's
        stale edges sitting there forever.

        Returns the edges that are genuinely new (to insert), the ids of
        edges no longer observed (to close), and the total number of edges
        that are current once this run's decisions are applied — an edge
        that already existed and is still observed is neither inserted nor
        closed; it simply stays exactly as it was, including its original
        `valid_from` and evidence.

        Lineage (`SUPERSEDES`) edges are handled separately by the caller:
        each is a one-time historical fact minted at the moment a
        supersession is detected, not a recurring structural observation, so
        there is nothing to reconcile them against.
        """
        previous_by_key: dict[tuple[str, str, RelationshipType], Relationship] = {}
        seen_ids: set[str] = set()
        for entity in entities:
            for rel in uow.graph.get_relationships(entity.id, direction="out"):
                if rel.id in seen_ids or not rel.is_current:
                    continue
                seen_ids.add(rel.id)
                previous_by_key[(rel.source_entity_id, rel.target_entity_id, rel.type)] = rel

        observed_keys = {(rel.source_entity_id, rel.target_entity_id, rel.type) for rel in observed}
        to_insert = [
            rel
            for rel in observed
            if (rel.source_entity_id, rel.target_entity_id, rel.type) not in previous_by_key
        ]
        stale_ids = [rel.id for key, rel in previous_by_key.items() if key not in observed_keys]
        current_count = len(previous_by_key) - len(stale_ids) + len(to_insert)
        return to_insert, stale_ids, current_count


def _extract_renames(observations: Sequence[Observation]) -> list[GitRename]:
    """Pull `GitRename` value objects out of a `HistoryAdapter`'s raw
    ``git.file_change`` observations — the shape `identity.attach_rename_evidence`
    actually wants, decoupled from any one adapter's payload dict layout."""
    renames: list[GitRename] = []
    for obs in observations:
        if obs.kind != "git.file_change" or obs.payload.get("status") != "RENAMED":
            continue
        old_path = obs.payload.get("old_path")
        new_path = obs.payload.get("path")
        similarity = obs.payload.get("similarity")
        if (
            isinstance(old_path, str)
            and isinstance(new_path, str)
            and isinstance(similarity, int | float)
        ):
            renames.append(
                GitRename(old_path=old_path, new_path=new_path, similarity=float(similarity))
            )
    return renames


def _extract_revisions(
    observations: Sequence[Observation], *, system_id: SystemID
) -> list[Revision]:
    """Pull `Revision` ancestry records out of a `HistoryAdapter`'s raw
    ``git.commit`` observations — the shape `core.revisions.RevisionGraph`
    actually wants, decoupled from any one adapter's payload dict layout
    (mirrors `_extract_renames` above)."""
    revisions: list[Revision] = []
    for obs in observations:
        if obs.kind != "git.commit":
            continue
        sha = obs.payload.get("sha")
        parent_shas = obs.payload.get("parent_shas")
        if not isinstance(sha, str) or not isinstance(parent_shas, list):
            continue
        authored_at = obs.payload.get("authored_at")
        message = obs.payload.get("message")
        revisions.append(
            Revision(
                system_id=system_id,
                sha=sha,
                parent_shas=tuple(p for p in parent_shas if isinstance(p, str)),
                authored_at=(
                    datetime.fromisoformat(authored_at) if isinstance(authored_at, str) else None
                ),
                message=message if isinstance(message, str) else None,
            )
        )
    return revisions
