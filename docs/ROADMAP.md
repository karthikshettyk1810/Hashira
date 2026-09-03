# Roadmap

Phases per spec §36, annotated with the MVP-scope decision recorded in
[ARCHITECTURE.md](ARCHITECTURE.md#mvp-scope-for-this-build).

## Phase 0 — Specification and research

- [x] Read and evaluate the spec.
- [x] Freeze the first machine-readable contracts (§44) — **this repository,
      as of `IR_VERSION = 0.1.0`**.
- [ ] Competitive review (SCIP/Sourcegraph, Glean, CodeQL) to sharpen the
      cross-domain differentiation claim in §38.
- [ ] Fixture repository design for the Python-deep MVP slice.

## Phase 1 — Core

- [x] Domain models: `System`, `Entity`, `Relationship`, `Evidence`,
      `Observation`, `Inference`, `Event`, `Snapshot`, `Change`, `Incident`,
      `Deployment`, `SystemState`/`Drift` (`src/hashira/core/`).
- [x] Storage ports: `SystemRepository`, `GraphRepository`, `EventStore`,
      `ObservationStore`, `EvidenceStore`, `InferenceStore`, `SnapshotStore`,
      `UnitOfWork` (`src/hashira/ports/repositories.py`).
- [x] Adapter ports: `LanguageAdapter`, `FrameworkAdapter`,
      `InfrastructureAdapter`, `IntegrationAdapter`,
      `AdapterCapabilities`/`ExtractionResult` (`src/hashira/ports/adapters.py`).
- [x] Intelligence port: `IntelligenceProvider`, `EmbeddingProvider`
      (`src/hashira/ports/intelligence.py`).
- [x] JSON Schema export and version-compatibility check
      (`src/hashira/core/schema.py`, `scripts/export_schema.py`).
- [x] Contract tests: core purity (no infra imports, one-way dependency),
      schema stability, port satisfiability (`tests/contract/`).
- [x] **Identity resolution ladder** — `src/hashira/identity/resolver.py`,
      20 tests in `tests/unit/test_identity.py`. See
      [IR.md's status section](IR.md#identity-resolution-10--status) for the
      policy and what is still open.
- [x] The **adversarial fixture suite** against real renames/extract-method/
      module-reorg diffs — pulled forward rather than deferred, immediately
      after the Python language adapter landed (`tests/integration/test_python_indexing.py`,
      against `tests/fixtures/python_basic/`). It found real, honest gaps the
      ladder-only design couldn't have surfaced on its own — see Phase 2.
- [x] **SQLite implementation of every port** (local-first default) —
      `src/hashira/storage/sqlite/`. One JSON-per-record column plus indexed
      filter columns (schema.py); every port backed by a live
      `sqlalchemy.Connection` scoped to one `SqliteUnitOfWork` transaction.
- [x] **`UnitOfWork` implementation with the §30 transactional guarantee** —
      a failed indexing run cannot corrupt the last known-good snapshot,
      because its writes never become visible: forgetting to `commit()`, an
      explicit `rollback()`, and an exception mid-transaction all behave
      identically. Proven by
      `test_a_failed_indexing_run_never_corrupts_the_last_known_good_snapshot`
      in the shared conformance suite below, not just asserted.
- [x] **A second port implementation, `hashira.storage.memory`** (in-process,
      copy-on-write transactions) — not strictly asked for, but the cheapest
      way to make "the ports are a real abstraction" a tested fact: one
      shared suite (`tests/contract/uow_conformance.py`, 23 tests) runs
      against both backends via `from ... import *` into each backend's own
      fixture file (`tests/contract/test_memory_conformance.py`,
      `tests/integration/test_sqlite_storage.py`). A future PostgreSQL
      implementation is held to the exact same suite.
- [ ] PostgreSQL implementation of every port (hosted/team store).
- [ ] **Revision-scoped historical queries** (`find_entities(revision=...)`,
      `get_relationships(revision=...)`) currently raise `NotImplementedError`
      rather than guess — §13's historical queries need a real Git revision
      ordering, which nothing produces yet. Closing this is Phase 2 work,
      once the Git/History adapter exists. `at: datetime` (wall-clock)
      queries work correctly today via `Relationship.held_at()`.

## Phase 2 — Python Ecosystem Intelligence (per the MVP scope decision)

Renamed from "Python/Django/Celery indexer" for a reason: this phase is
Hashira learning to read a Django/Celery **target project** — see
[ARCHITECTURE.md#hashiras-own-stack-vs-what-hashira-understands](ARCHITECTURE.md#hashiras-own-stack-vs-what-hashira-understands).
None of the frameworks below become a Hashira dependency.

```
HistoryAdapter        LanguageAdapter        FrameworkAdapter        DataAdapter        AsyncAdapter
    Git          →     Python (ast/         →  Django, FastAPI   →   Django ORM,   →    Celery, Kafka
                        tree-sitter)                                 SQLAlchemy,
                                                                      migrations
```

The Python language adapter must produce a useful, if shallower, graph entirely
on its own — plain imports, calls, class hierarchy — before any framework
enricher is layered on. That ordering is what keeps the universal model honest:
if `INTERFACE`/`DATA_ENTITY`/`PROCESS` only ever show up *with* a framework
adapter attached, something has leaked framework-specific assumptions into the
core rather than the adapter.

- [x] **Python language adapter** (`src/hashira/adapters/python/`) — stdlib
      `ast`, not tree-sitter (see [ADAPTERS.md](ADAPTERS.md#python-adapter)
      for why). Stands alone against a plain-Python fixture repo
      (`tests/fixtures/python_basic/`), with zero framework assumptions:
      modules, classes, functions, methods, async functions, imports,
      calls, inheritance and decorators, each carrying source location and
      evidence. Best-effort call/base-class resolution (`self.`/local-var/
      import/module-local) stays syntax-only and never invents an entity for
      something it cannot confirm — see `adapters/python/resolve.py`.
- [x] **`IndexingService`** (`src/hashira/application/indexing.py`) — wires
      adapter → normalizer → identity resolution → storage as one
      transaction, language-agnostic by construction (a `Normalizer`
      Protocol, not an import of the Python one). This is the piece that
      actually exercises the §30 guarantee end to end, not just at the
      storage layer.
- [x] **The adversarial identity suite ran, found real gaps, and two of them
      got fixed before Git** — not deferred as "known limitations" once it
      became clear they were indexing-orchestration bugs, not things only
      Git evidence could fix. Confirmed by
      `tests/integration/test_python_indexing.py` against real file mutations:
  - ~~Unchanged-file re-indexing produced `SUPERSEDES`, not `MATCHED`~~ —
    **fixed** (IR 0.1.2): added `IdentityClaimKind.DECLARATION_ANCHOR` (file
    path + qualified name + kind, all at once) as a strong-tier signal. Not
    independent of `QUALIFIED_NAME` — derived from it — but materially
    narrower ("the exact site a prior entity came from," not "a name I
    recognize somewhere"), so it does not reopen the "qualified name alone
    must not merge" guard. An unchanged project now re-indexes to zero new
    entities, zero supersessions. See `identity/resolver.py` and
    `adapters/python/normalizer.py`.
  - ~~Stale relationships were never retracted~~ — **fixed**: `IndexingService`
    now reconciles structural relationships (DEFINES/IMPORTS/CALLS/EXTENDS)
    against what's already current in storage before writing anything — an
    edge still observed is left exactly as it was (same row), an edge no
    longer observed is closed via `Relationship.close()`, and only genuinely
    new edges are inserted. An unchanged project now writes zero new
    relationship rows; a renamed symbol's stale edges are actually retracted.
    See `IndexingService._reconcile_relationships`.
  - **A rename still produces plain `NEW` with *no* lineage at all**, and the
    old entity is left `ACTIVE` and orphaned — this one genuinely does need
    Git evidence: the file and qualified name are the only signals available,
    and a rename changes both at once, leaving nothing to match the old
    entity on. Not a bug in the ladder — correctly refusing to guess.
  - Structural similarity (copy/paste, extracted methods) correctly never
    creates a false connection — confirmed, not just asserted by the resolver
    unit tests.
  - Two existing entities sharing a qualified name correctly produce
    `AMBIGUOUS` (a `SPECULATIVE` `HYPOTHESIS`), never a guessed pick.
- [ ] **Close the rename gap**: Git adapter (repository discovery, commit/
      rename history, revisions) supplying `GIT_RENAME` identity claims, so a
      rename can resolve as `SUPERSEDES`-with-lineage instead of orphaning
      the old entity. Also: real removal detection (an entity with no
      candidate and no Git-confirmed rename really is gone) and incremental
      indexing (skip unchanged files rather than re-deriving and reconciling
      away nothing every run). This is now the most concretely justified next
      step in this phase — not a guess about what might matter later.
- [ ] Django framework enricher — reads a target Django app; views/URLs →
      `INTERFACE`, models → `DATA_ENTITY`. Ships as one adapter among several
      `FrameworkAdapter` implementations, not as a Hashira dependency.
- [ ] Celery async enricher — reads a target Celery app; tasks → `PROCESS`,
      queues → `MESSAGE_CHANNEL`.
- [ ] Postgres data adapter, from migrations/schema (§21: "do not make vector
      search the source of truth" applies here too — schema facts come from
      migrations, not inference).
- [ ] TypeScript language adapter (structural only, to prove IR portability
      per §42 — deferred relative to the original §35 sequencing).

## Phase 3 — Developer UX

- [ ] CLI per §28: `init`, `index`, `status`, `explain`, `impact`, `trace`,
      `dependencies`, `history`, `architecture`. Human-readable by default,
      `--json` for automation.
- [ ] Query services per §24: `get_entity`, `find_entities`,
      `get_relationships`, `find_dependents`, `find_dependencies`,
      `trace_path`, `impact_analysis`, `get_history`, `get_snapshot`,
      `find_incidents`, `find_evidence`, `get_current_state`.
- [ ] Impact analysis per §25, distinguishing graph reachability from
      semantic risk (`RiskAssessment` already models this in `core/changes.py`;
      the traversal engine that populates it does not exist yet).

## Phase 4 — Agent integration

- [ ] MCP tools per §27: `system.explain`, `system.impact`, `system.trace`,
      `system.dependencies`, `system.history`, `system.incidents`,
      `system.architecture`, `system.search`.
- [ ] `hashira mcp` / `hashira agent` CLI commands.

## Phase 5 — Runtime intelligence

- [ ] CI integration, Sentry/observability integration.
- [ ] Deployment and incident tracking wired into the graph
      (`core/state.py::Deployment`, `core/incidents.py::Incident` already
      modeled).

## Phase 6 — Engineering intelligence

- [ ] Risk reasoning, causal hypotheses (`core/incidents.py::CausalHypothesis`
      already enforces evidence-before-`CONFIRMED`), historical pattern
      detection.

## Phase 7 — Controlled autonomy

- [ ] Planning, code changes, verification, policy engine, rollback/recovery
      (§29). Explicitly out of scope until the Phase 0–6 foundation is solid
      per the §42 definition of done.

## Phase 8 — Ecosystem

- [ ] Java/Go/Rust/.NET adapters, infrastructure adapters, hosted
      collaboration, enterprise controls.

## Definition of done for the foundation (§42)

Gates Phase 7. Copied here so it stays visible against the phase list above:

- [x] System IR schema is versioned and validated.
- [x] Core entities/relationships are stable enough for a real adapter — the
      Python adapter's entire extraction (§8's MODULE/SYMBOL, §11's
      DEFINES/IMPORTS/CALLS/EXTENDS) needed zero core changes beyond the
      `IdentityClaimKind` tightening in 0.1.1. Still needs a *second* language
      adapter to prove it wasn't Python-shaped by accident.
- [x] Provenance is mandatory for derived knowledge (enforced in
      `core/evidence.py`, `core/relationships.py`).
- [x] Events are immutable and idempotent (enforced in `core/events.py` and
      proven at the persistence layer by the shared conformance suite against
      both storage backends).
- [x] Snapshots are reproducible from source — `IndexingService` produces a
      real `COMPLETE` snapshot from an actual repository now, not just a
      round-tripped record (`SqliteSnapshotStore`/`MemoryDatabase`,
      `latest_complete()` correctly ignoring non-`COMPLETE` ones).
- [ ] Incremental indexing works (confirmed *not* working yet, precisely —
      see Phase 2's adversarial-suite findings — rather than merely unbuilt).
- [ ] At least two language ecosystems map into the same semantic model.
- [x] SQLite persistence can rebuild a graph without vendor lock-in — the
      port is the only thing an indexer talks to (`src/hashira/storage/sqlite/`),
      confirmed by running the identical Python-adapter pipeline against both
      `SqliteDatabase` and `MemoryDatabase` with zero adapter-side changes.
- [ ] The same is true of a PostgreSQL implementation, once it exists.
- [ ] CLI and JSON APIs can query the same domain services.
- [ ] MCP can expose read-only intelligence without modifying core.
- [x] Tests cover identity, temporal, provenance and graph invariants
      (`tests/unit/`, `tests/contract/`, `tests/integration/`) — 204 tests as
      of the Python adapter landing, including the adversarial identity suite
      against a real fixture repository, not just the resolver in isolation.
- [x] Documentation includes adapter and extension rules — `ADAPTERS.md` now
      has a worked example (the Python adapter) rather than only a plan.
