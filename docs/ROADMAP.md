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
- [x] **Revision-scoped historical queries** — `find_entities(revision=...)`
      and `get_relationships(revision=...)` still raise `NotImplementedError`
      on both storage ports (correctly: neither backend does this natively
      yet), but the capability itself now exists one layer up. See the
      temporal-queries milestone under Phase 2 below.

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
- [x] **Git adapter** (`src/hashira/adapters/git/`) — `runner.py` (the one
      place Hashira shells out to `git`), `repository.py` (parsed commits,
      ancestry, rename/copy detection, all tested against real temporary
      repositories, not mocked output), `adapter.py` (the `HistoryAdapter`
      port: commits become `Event`s, file changes and renames become
      `Observation`s, Git's own similarity score travels with each rename
      rather than being collapsed into a boolean). Tolerates a bad or stale
      caller-supplied revision (§30: adapters tolerate partial failure)
      rather than crashing the indexing run.
- [x] **`GIT_RENAME` evidence, wired into identity resolution**
      (`identity/git_evidence.py`) — a rename Git detected above a **90%**
      similarity threshold (deliberately stricter than Git's own 50% default;
      see the module for why) becomes a matching claim on both the old and
      new entities, attached *before* `identity.resolve()` runs. Pairing is
      precise: the module pairs by file path alone (exactly one module per
      file); a symbol only pairs when its simple name is unchanged *and*
      unambiguous on both sides — a symbol renamed in the same commit as its
      file gets no claim and correctly falls back to orphaned `NEW`, since
      path and name both changing at once leaves nothing to pair on.
      `IndexingService` now asks a configured `HistoryAdapter` what happened
      between the last indexed revision and this one, and feeds the result
      through before resolution (`application/indexing.py`).
- [x] **A path-representation bug found and fixed while verifying the above
      end-to-end**: the Python adapter recorded `source.file` relative to
      the *import root* (e.g. `shop/payments.py`, stripping a `src/`
      prefix), while Git always reports paths relative to the *repository
      root* (`src/shop/payments.py`) — the two never matched, so rename
      pairing silently found nothing. `extract_file` now takes both roots
      explicitly and records repo-root-relative paths (`adapters/python/extractor.py`).
      A reminder that this class of bug — two correct-looking components
      that silently fail to connect — is exactly what end-to-end adversarial
      testing catches and unit tests in isolation cannot.
- [x] **Adversarial suite extended with real Git history**
      (`tests/integration/test_git_identity.py`): simple rename → lineage
      preserved; module moved into a subdirectory → lineage preserved;
      rename plus a modest content edit → lineage preserved at `LIKELY`
      (not `CERTAIN`) confidence, matching Git's own reported similarity;
      rename plus a *symbol* rename in the same commit → the module links,
      the symbol correctly does not; total content rewrite disguised as a
      `git mv` → no lineage at all, whether or not Git's own heuristic still
      calls it a rename. Verified against both storage backends.
- [ ] **Still open**: real removal detection (an entity with no candidate
      and no Git-confirmed rename really is gone — not attempted yet, since
      it's easy to get wrong ahead of incremental indexing) and incremental
      indexing itself (skip unchanged files rather than re-deriving and
      reconciling away nothing every run). Branch/merge-aware indexing
      semantics remain deliberately out of scope (`changed_paths_in_commit`
      reports a merge commit's changes relative to its first parent only).
      Revision-scoped historical queries, previously listed here as open,
      are closed — see below.
- [x] **Django framework enricher** (`src/hashira/adapters/django/`) — the
      first proof that the graph is genuinely cross-domain, not just a code
      graph with extra steps. Built on Python's own observations, not a
      second parse of Python (`adapter.py`'s module docstring spells out the
      reuse boundary): models and views are detected from *resolved*
      inheritance (`python.inheritance` observations against a closed,
      evidence-based allowlist in `known_bases.py` — never a name-suffix
      guess), model fields and URL patterns are this adapter's own
      genuinely-new targeted parsing (Python's extractor has no concept of
      either), and field reads/writes are detected via the same
      `LOCAL_INSTANCE` pattern the Python resolver already established, reused
      via its public `resolve_expr`.
  - **The killer test passes**: `tests/integration/test_django_identity.py`
    indexes a real Django-shaped fixture (`tests/fixtures/django_basic/`)
    and answers *"what is affected if `Payment.status` changes?"* by walking
    the graph backward through non-structural edges — reaching
    `PaymentService.process` (writes/reads the field directly),
    `CheckoutView.post` and the test that calls it (both `CALLS` the
    service) — entirely from stored relationships, no LLM involved. The
    `/checkout/` route is one more hop away via `EXPOSES`.
  - **A model class is tagged, not duplicated or reclassified**: it stays
    `EntityType.SYMBOL` (still fundamentally a Python class — core/base.py's
    own rule that technology detail belongs in metadata, never a reshaped
    core envelope) with `metadata.framework`/`django_kind` set. Model
    fields and URL routes, which have no Python-symbol counterpart at all,
    get freshly minted `SYMBOL`/`INTERFACE` entities with the exact same
    `QUALIFIED_NAME` + `DECLARATION_ANCHOR` identity-claim shape Python's own
    entities carry — an unchanged Django project re-indexes exactly as
    boring as an unchanged Python one (verified, not assumed).
  - Two small shared utilities came out of this pass:
    `adapters/_dedup.py` (relationship deduplication, previously private to
    Python's normalizer) and `adapters/_identity_claims.py` (the
    `QUALIFIED_NAME`/`DECLARATION_ANCHOR` claim builders) — both normalizers
    now import the same logic instead of risking two normalizers deciding
    identity slightly differently.
  - `IndexingService` gained `framework_adapters`: each `enrich()` call
    receives everything extracted so far and returns only its own additions,
    merged in before normalization runs (`application/indexing.py`).
  - **Not attempted in this pass**: function-based views, abstract model
    inheritance chains (a model extending another model extending
    `models.Model`), `self.attr` field access (only local variables are
    tracked, matching the Python resolver's own `LOCAL_INSTANCE` limit), and
    resolving `include()`'d URL confs across files (a route whose target
    doesn't resolve stays an `INTERFACE` entity with no `EXPOSES` edge,
    correctly, rather than a guess).
- [x] **Temporal/revision-aware queries** — deliberately built next instead
      of FastAPI: Django proved *why* the graph exists, Git supplied the raw
      material for *when* it was true, and the two needed to connect before
      a second framework arrived. Three pieces, in order:
  - **Revision ancestry as a core concept, not a Git object** —
    `core/revisions.py::Revision` (system id, sha, parent shas, provider —
    the same natural sha string already used everywhere else in the IR, not
    a second opaque id every caller would have to translate through) and
    `RevisionGraph` (plain reachability over `parent_shas`: `is_ancestor`,
    `is_ancestor_or_self`, `ancestry_between`). The core still imports no
    Git; `IndexingService` populates `Revision` records from a
    `HistoryAdapter`'s `git.commit` observations
    (`application/indexing.py::_extract_revisions`) and persists them via a
    new `RevisionStore` port, backed on both storage backends and held to
    the same shared conformance suite as every other port.
  - **Snapshots stay exactly what they already were**: a named cut point
    over revision-keyed validity, not a second source of truth
    (`Snapshot.revision` already identified the point in history; nothing
    about that needed to change — see IR.md's temporal design note, which
    this milestone confirmed rather than revised).
  - **Historical query, kept deliberately boring**:
    `application/history.py::query_at_revision` loads the *current*,
    fully-materialized graph and filters it against each entity's/edge's
    revision-keyed validity using real ancestry, not string equality or
    wall-clock time — "materialize the snapshot for that revision" means
    "compute this filtered view on demand," not "store a second copy of the
    graph per revision." `revision=None` means today, exactly what the
    ordinary (non-historical) ports already return. Superseded entities are
    tracked correctly even though `Entity` itself never records *when* a
    supersession happened (`identity/resolver.py::apply`'s `SUPERSEDES`
    branch flips status but leaves `last_seen_revision` alone) — the
    lineage `Relationship`'s own `valid_from_revision` is the only record of
    that moment, so the query reads it from there.
  - **The brutal integration test** (`tests/integration/test_temporal_queries.py`):
    a real Git history (commits A–D) over the Django fixture, where a
    structural edit at C breaks the `Payment.status` impact chain
    (`CheckoutView.post` stops calling the service) and a route change at D
    must not leak backwards — `query_at_revision(..., revision=A)` shows the
    old full chain, `revision=C` and `revision=None` (today) both show the
    broken one, and D's new route is invisible at every earlier revision. A
    second test drives the existing rename-lineage scenario
    (`payments/services.py` → a new `billing/` package) across three
    revisions plus an unrelated *disguised* rename in the same history,
    proving `query_at_revision` shows the pre-rename entity only before the
    move, the post-rename entity (with its `GIT_RENAME` claim and
    `SUPERSEDES` edge intact) only from the move onward, and still refuses
    to link the disguised case at any revision — the revision-query layer
    does not launder a bad merge into looking legitimate just because time
    passed.
  - **A real bug found and fixed while building the second test**: filtering
    relationships to "both endpoints present at this revision" silently
    dropped every `SUPERSEDES` lineage edge the moment a query reached or
    passed the revision it was minted at — because a lineage edge's entire
    purpose is to point from a currently-present entity back at one that, by
    definition, is not. Fixed in `application/history.py::_bounded` by
    exempting `SUPERSEDES` from the target-presence check.
  - **Deliberately not built**: event sourcing, a temporal SQL abstraction
    layer, branch-aware semantic merging, CRDT-like graph reconciliation,
    incremental indexing, a storage-native/indexed implementation of
    `find_entities(revision=...)` itself (still `NotImplementedError` on
    both backends — reserved for when this needs to run faster than "load
    the whole graph and filter it in Python"), and the FastAPI adapter.
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
