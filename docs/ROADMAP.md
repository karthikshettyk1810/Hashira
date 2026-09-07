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
- [x] **FastAPI framework enricher** (`src/hashira/adapters/fastapi/`) — not
      built to add feature count, but as an architectural test: can two
      materially different frameworks produce equivalent concepts in the
      same System IR without contaminating core or the Python adapter?
  - **Same reuse discipline as Django, applied to a different idiom.**
    Django keys off resolved inheritance; FastAPI has no base-class
    vocabulary to key off, so this adapter resolves *decorators and call
    expressions* instead (`@app.get(...)`, `Depends(...)`,
    `include_router(...)`) — through the same `resolve_expr` mechanism,
    against the same kind of curated, fully-qualified allowlist
    (`known_symbols.py`, Django's `known_bases.py` for a different surface).
    A variable named `app` that is not actually `FastAPI()` is not detected,
    matching Django's own "a class named `PaymentModel` proves nothing by
    its name" discipline.
  - **`adapters/_python_index.py` came out of proving this** — Django's own
    `_Index`/`_TreeCache`, extracted once a second framework adapter needed
    exactly the same plumbing over Python's observations, joining
    `_dedup.py`/`_identity_claims.py` as shared infrastructure two
    independent adapters actually needed, not infrastructure designed in
    advance of being needed. Route/handler/dependency *semantics* stayed
    completely separate per adapter — see the design-question bullet below.
  - Supports: `FastAPI()`/`APIRouter()` recognition, `@app.*`/`@router.*`
    route registration, `include_router(..., prefix=...)` (one hop of
    prefix composition — documented, not attempted deeper),
    `Depends(...)` as its own `DEPENDS_ON` edge — deliberately *not* folded
    into `CALLS` just because the syntax contains a call expression — and
    basic request/response model association (a parameter/return annotation
    resolving to a class Python's own `python.inheritance` observations
    confirm extends `pydantic.BaseModel`).
  - **A route handler is tagged, not duplicated**, the exact rule proven for
    Django's models/views: it stays `EntityType.SYMBOL` with
    `metadata.framework`/`fastapi_kind` set. A route gets a freshly minted
    `INTERFACE` entity (`EXPOSES`-linked to the handler), since a route has
    independent system meaning no Python symbol carries.
  - **Request/response model association deliberately stays out of the
    graph as an edge type.** Neither `CONSUMES`/`PRODUCES` (already
    reserved for message-queue direction — see `docs/ARCHITECTURE.md`'s
    Celery example) nor `DEPENDS_ON` (reserved for `Depends()`) fit, and
    inventing a new relationship type for two adapters' first pass at this
    would have been exactly the premature taxonomy this milestone was
    explicitly warned against. Recorded as metadata on the handler entity
    instead.
  - **The design question this milestone was built to surface**: Django and
    FastAPI now independently reach for the same shape of concept — HTTP
    method, route, handler — and both independently minted
    `EntityType.INTERFACE` + `EXPOSES` for it, with neither adapter
    importing from the other. That is evidence a framework-neutral "route"
    concept might eventually deserve a typed core representation. **Not
    promoted yet** — two adapters converging once is a signal worth
    watching, not proof. *Adapters discover abstractions; core should not
    predict them.*
  - **The real definition of done**
    (`tests/integration/test_cross_framework_equivalence.py`), not "the
    fixture indexes successfully": two independently-indexed, semantically
    equivalent applications (`tests/fixtures/django_basic/`, reused as-is
    since it already matched the target shape;
    `tests/fixtures/fastapi_checkout/`, new) are asked the *same* question —
    "what is affected if `PaymentService.process` changes?" — by *one*
    `reverse_impact` function that imports nothing from either adapter
    package. Both traversals reach something playing "the handler that
    receives the request" and something playing "the test that verifies the
    behavior." The exact impact sets are not asserted equal on purpose —
    Django's chain has an intervening view class FastAPI's plain function
    handler does not, so FastAPI's route ends up reachable in the same
    unbroken traversal while Django's needs a documented extra hop — a real,
    expected architectural difference, not a discrepancy to paper over.
  - **Not attempted in this pass**: SQLAlchemy or any persistence layer (a
    `DataAdapter`'s job, not a framework enricher's — see below),
    field-level access on Pydantic models (no FastAPI/Pydantic equivalent of
    Django's `_extract_field_accesses` — association is handler-to-model,
    not model-field-to-handler), class-based endpoints (function handlers
    only), and multi-hop router nesting.
- [x] **SQLAlchemy data adapter** (`src/hashira/adapters/sqlalchemy/`) — the
      first `DataAdapter` (`ports/adapters.py`), a kind deliberately distinct
      from `FrameworkAdapter`: it must never care whether the code it
      enriches belongs to FastAPI, Django, a CLI, or nothing at all. Proven,
      not just asserted — `tests/integration/test_sqlalchemy_identity.py`
      indexes a framework-free fixture with *no* `FrameworkAdapter`
      configured at all.
  - **Detects both real-world declarative styles**: SQLAlchemy 2.0's
    class-based root (`class Base(DeclarativeBase): pass`, resolvable
    through Python's own `python.inheritance`) and the still-extremely-common
    factory style (`Base = declarative_base()`, a plain module-level
    variable Python's extractor never tracks — found by this adapter itself,
    the same way `adapters.fastapi.adapter` finds `app = FastAPI()`). Both
    feed one `known_bases` set expanded to a fixpoint, so a multi-level
    hierarchy resolves regardless of which style introduced its root — this
    genuinely needed the fixpoint to re-resolve every class's own bases
    itself, since `python.inheritance` alone cannot see past a
    locally-assigned `Base` variable at any depth (caught by
    `test_a_multi_level_base_hierarchy_still_resolves` during development,
    not by inspection). A class is only a table if its own body declares
    `__tablename__`; an intermediate abstract base/mixin is correctly never
    classified as one.
  - **The design question resolved carefully, as asked**: ORM class vs.
    database entity. Every other enricher here tags an existing Python
    entity in place, because the framework construct *is* that Python
    construct. An ORM class and its table are not — one is source
    structure, the other a runtime/data structure with independent
    identity. So this is the one place a framework enricher mints a
    *second*, linked entity: the class stays tagged
    (`metadata.framework`/`sqlalchemy_kind`, the same rule as everywhere
    else), and a fresh `EntityType.DATA_ENTITY` is minted for the table.
  - **Two new relationships, added only after checking the existing
    vocabulary honestly didn't cover them** (`docs/IR.md`'s entry on this
    milestone has the full reasoning): `RelationshipType.MAPS_TO` (class →
    table — not `EXTENDS`/`IMPLEMENTS`, which are code-structural, and not
    `RELATED_TO`, which is too vague for an impact query to use) and
    `RelationshipType.REFERENCES` (a foreign key between two columns — a
    relational-database fact independent of any one adapter, distinct from
    `DEPENDS_ON`, which already spans build-time imports and runtime
    dependency injection). Columns are `CONTAINS`-related to their table,
    matching Django's model-field pattern; `ForeignKey("table.column")`
    resolves via direct qualified-name lookup, no extra resolution needed
    since a column's own qualified name and the FK string share the same
    format by construction.
  - **Basic read/write evidence, kept as an independent copy of Django's
    pattern, not shared** — the milestone's explicit instruction: don't
    refactor Django's model handling into shared plumbing yet, only once a
    second adapter's *independent* needs prove what's actually common. What
    did prove common and got extracted
    (`adapters/_python_index.py::PythonIndex`/`PythonTreeCache`/`find_class_node`)
    is pure plumbing over Python's own observations, never data-semantic
    logic — Django's adapter was refactored to use the shared version too,
    proven safe by its own full test suite staying green throughout.
  - **A genuinely new, generic piece of infrastructure, not
    FastAPI-SQLAlchemy bridge code**: `adapters/_compose.py::compose_normalizers`.
    `IndexingService` takes exactly one `Normalizer`, but composing two
    independent enrichers' own `normalize()` naively — each calling
    Python's normalizer itself, each minting a *different* fresh id for the
    same underlying Python class — would silently duplicate every plain
    `CALLS`/`IMPORTS`/`DEFINES` edge Python's own normalizer produces (only
    caught by writing `test_no_duplicate_relationship_rows_from_composing_two_enrichers`
    and finding it necessary, not by design review). Fixed by running
    Python's normalizer exactly once and chaining each enricher's new
    `enrich_normalized_run` (added alongside each existing `normalize`, now
    a one-line wrapper) onto the same entity pool. Nothing in `_compose.py`
    names FastAPI or SQLAlchemy.
  - **Then plugged, completely unmodified, into the FastAPI fixture**
    (`tests/fixtures/fastapi_checkout/payments/db_models.py`, new;
    `payments/services.py` now genuinely reads/writes a SQLAlchemy
    `Payment.status` instead of a bare local variable) alongside
    `FastAPIAdapter` in one `IndexingService` run
    (`tests/integration/test_fastapi_sqlalchemy_together.py`) — one fully
    connected graph from the HTTP route down to the database column, no
    bridge code. All previously-passing FastAPI-only and cross-framework
    tests were re-verified green after the fixture change, not assumed
    safe.
  - **Watching, not acting**: Django's and SQLAlchemy's model/field
    handling now both independently reach for the same shape of concept — a
    data entity, contained fields, read/write evidence — evidence worth
    watching for a future shared data-semantic layer, deliberately not
    acted on now. *Adapters discover abstractions; core should not predict
    them* — the same principle established for `EntityType.INTERFACE`/
    `EXPOSES` in the FastAPI milestone, now applied to data semantics.
  - **Not attempted in this pass**: query-shape analysis, sessions/
    transactions, async SQLAlchemy, Alembic migrations, raw SQL, hybrid
    properties, `relationship(...)` construct parsing (deep relationship
    inference was explicitly out of scope — only a column's direct
    `ForeignKey(...)` argument is read), and multi-hop `ForeignKey` chains
    beyond a direct string reference.
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
      `trace_path`, `get_history`, `get_snapshot`, `find_incidents`,
      `find_evidence`, `get_current_state` -- `impact_analysis` itself has
      moved up; see below.
- [x] **Impact Analysis v0.1** (`src/hashira/application/impact.py`) — the
      traversal engine §25 needed. Deliberately built *before* the CLI/query-
      service layer around it: the milestone question was "can Hashira
      reason over the system it indexed, not merely store it" — deterministic
      graph reasoning, not LLM reasoning — and that question is answered by
      the engine existing and being correct, not by how it's exposed.
  - **`reverse_impact`/`forward_impact`**, both walking
    `core.relationships.IMPACT_EDGES` (defined since the Django milestone,
    genuinely used for the first time here) — finally making real the
    `_reverse_impact` helper every framework-milestone test since Django has
    been reimplementing by hand (`test_django_identity.py`,
    `test_fastapi_sqlalchemy_together.py`, ...).
  - **Explainable paths, not a set of names**: a result is a sequence of
    `ImpactPath`s, each a sequence of `ImpactHop`s — one per traversed
    `Relationship`, carrying the entities on both ends and its evidence
    (resolved once per query, not per hop, via a single batched
    `uow.evidence.get_many` call). v0.1 keeps exactly one (shortest) path
    per reached entity, not every path between two entities — a deliberate
    "boring first" scope, like `application/history.py`'s own.
  - **Path confidence is not a thing this module invents.** Collapsing a
    path's several, independently-sourced per-hop confidences into one
    number would smuggle back the false precision `Confidence`'s ordinal
    design (§11, `docs/IR.md`) exists to refuse — a CERTAIN hop followed by
    a SPECULATIVE one must not read as "pretty confident" for the whole
    path. `ImpactPath.weakest_confidence` is a plain minimum over the
    *existing* per-hop values, not a synthesized score; every hop's own
    confidence stays inspectable individually.
  - **Identity-lineage-aware, as a separate, explicit step.**
    `resolve_identity` follows `SUPERSEDES` lineage forward so a query about
    an entity id a later revision superseded (a rename) keeps working
    instead of silently returning nothing — reported via
    `ImpactResult.resolved_from`, never folded invisibly into the walk.
    `SUPERSEDES` itself stays out of `IMPACT_EDGES` on purpose: lineage is
    not the same claim as "affects".
  - **The killer integration test**
    (`tests/integration/test_impact_historical.py`) combines everything
    built so far — FastAPI, SQLAlchemy, Python, Git, and temporal queries —
    over real Git history on the `fastapi_checkout` fixture (commits A/B/C).
    It surfaced a genuine, honest boundary worth documenting rather than
    working around: a bare attribute-level rename (a column simply renamed,
    with no corresponding file move) has no Git-backed identity signal to
    attach today — the same limitation `identity/git_evidence.py` already
    states for a Python symbol renamed in the same commit as its file. The
    test instead exercises the rename story that *does* have real evidence
    behind it (a file move changing a class's qualified name, resolved via
    existing `GIT_RENAME` lineage) side by side with an entity whose
    identity is untouched by that same move (a SQLAlchemy column, keyed by
    `table.field` rather than file path) — proving `resolve_identity` is
    exercised only where it is actually needed, not applied as a blanket
    assumption.
  - **`application/`, not `core/`** — core knows entities, relationships,
    evidence, revisions, snapshots; this module knows how to traverse those
    primitives to answer a system question, kept as its own file
    (`impact.py`) alongside `history.py`, not folded into either core or
    `IndexingService`.
  - **Not attempted in this pass**: the CLI/MCP surface above this engine
    (Phase 3/4, still open), enumerating every path between two entities
    (only the shortest per reached entity), and any new identity-resolution
    capability for bare attribute-level renames — closed by the very next
    milestone, immediately below.
- [x] **Identity Resolution v0.2: declaration-level lineage** — closes the
      gap the Impact Analysis milestone explicitly declined to fake: a
      symbol renamed *within* a file that itself never moved, which Git's
      file-level rename detection has nothing to see. Not solved with
      similarity matching — every gate below is a structural fact, never a
      score (`docs/IR.md`'s entry on this milestone has the full reasoning).
  - **`IdentityClaimKind.DECLARATION_LINEAGE`** (`core/enums.py`), sitting
    at corroborating tier next to `GIT_RENAME`/`MIGRATION_LINEAGE` — the
    same ladder rule, a new evidence source. `GitAdapter` reports a
    `MODIFIED` file's content *before* the change as a raw fact
    (`git.file_change.old_content`); `adapters/sqlalchemy/adapter.py::
    _detect_declaration_renames` re-parses it with the adapter's own column
    extraction and compares old columns to new; `identity/declaration_evidence.py::
    attach_declaration_lineage_evidence` turns an unambiguous 1:1
    correspondence into a claim on both sides — the sibling of
    `git_evidence.py::attach_rename_evidence`, one level down (a
    declaration inside a file, not a file inside a repository). Git's own
    rename mechanism was deliberately left untouched, exactly as asked:
    "don't distort it to accommodate symbol-level evolution."
  - **Three cases, one discipline, proven with the exact fixture the
    milestone specified**: a clean rename (same type family) resolves
    `SUPERSEDES` with evidence-appropriate confidence (`CERTAIN` when every
    detail agrees, `LIKELY` when only the family does); a delete-and-recreate
    with no correspondence signal produces the existing, already-correct
    behavior (orphaned old entity, disconnected `NEW`) unchanged; a rename
    where the type family *also* changes (`String` → `Integer`) is treated
    the same as the second case, not a weaker version of the first — "the
    system should not automatically conclude 'same entity because the name
    changed'" was the explicit instruction, and no claim is proposed at all
    for it, not a low-confidence one.
  - **A real, pre-existing bug this surfaced**:
    `application/indexing.py::_reconcile_relationships` fetched every
    outgoing edge to decide what counts as stale, including `SUPERSEDES` --
    which this run's structural observations never contain, since it is a
    one-time historical fact, not a recurring one. Any existing lineage
    edge therefore read as "no longer observed" on the very next, otherwise
    unrelated re-index and got silently closed -- a bug that had been
    latent since the original Git-rename milestone, only surfaced now
    because this milestone's killer test was the first to re-index *after*
    a supersession existed. Fixed by excluding `SUPERSEDES` from
    reconciliation entirely, with a regression test added to
    `test_git_identity.py` (the original mechanism's own home), proving the
    fix protects both mechanisms, not just the new one.
  - **The killer integration test**
    (`tests/integration/test_identity_evolution.py`), over the same
    combined FastAPI + SQLAlchemy + Git fixture Impact Analysis used:
    `payments.status` renamed to `payments.state` in one commit (only the
    model), the *usage* catching up in a separate, later commit (only the
    service) — deliberately not collapsed into one atomic change, since the
    gap between "identity survived" and "anything actually uses the new
    name yet" is itself worth being able to answer precisely.
    `application.impact.reverse_impact`, asked about the pre-rename id at
    any later revision, transparently follows the lineage and reports
    exactly what was truly reachable at that point -- empty, in the gap;
    the full chain again, once the usage caught up.
  - **Not attempted in this pass**: a class *and* one of its declarations
    renamed in the same commit (each mechanism stays conservative alone;
    stacking both is a future case, matching `git_evidence.py`'s own
    "path and name both changed" refusal), a table itself being renamed,
    and generalizing declaration-lineage detection to Django or plain
    Python symbols (the mechanism is generic -- `application/indexing.py::
    _extract_declaration_renames` matches any `*.declaration_rename`
    observation kind -- but nothing yet produces one outside SQLAlchemy;
    watched, not built ahead of a second adapter's need).
- [x] **Impact Analysis v0.2: coverage-aware analysis** — not requested in
      advance; discovered by running the actual product research MCP read
      surface v0.1 was built to enable: a real coding agent, given the MCP
      server and one honest task ("map the blast radius of a
      `Payment.status` representation change, no hints about which tools
      exist"), against an unfamiliar, deliberately messy fixture with a
      real historical rename, dynamic dispatch, an unresolved external SDK
      call, and a raw-SQL worker. Baseline (no Hashira) and MCP-augmented
      runs found the *same* impact set — Hashira did not save work, it cost
      an extra detour — because `reverse_impact("payments.status")`
      returned 2 correct paths while genuinely missing `PaymentService.
      process`/`.refund` (SQLAlchemy's v0.1 field-access tracking only
      recognized `payment = Payment()` local instantiation, not
      `def process(self, payment: Payment)` typed parameters) and the
      result carried no signal that it might be incomplete. The
      augmented agent's own instinct ("that felt too small") saved it, not
      anything Hashira told it — and its own wishlist named the fix
      unprompted: *"a completeness/confidence indicator on reverse_impact
      would help a lot."* The real finding: **a correct graph traversal is
      not automatically a complete system answer**, and the fix ordering
      matters — widen adapter coverage first and the exact same silent gap
      just reopens one provenance form later (raw SQL, dynamic dispatch, a
      repository abstraction, ...); establish coverage semantics first and
      every future gap stays honest by construction. `docs/IR.md`'s
      "Coverage is not confidence" entry has the full three-state framework
      (`FOUND` / `NOT_OBSERVABLE` / recognized-but-unresolved) and the two
      permanent invariants this milestone commits to.
  - **`ImpactCoverage`** (`application/impact.py`, new): `status`
    (`COMPLETE`/`PARTIAL`), `unresolved_access_count` (a real, per-query
    count read off the queried entity's own `metadata`, populated at
    indexing time from adapter-reported `*.unresolved_field_access`
    observations), and `limitations` (adapter-declared, pipeline-wide
    strings, e.g. "raw SQL is not analyzed", read off the latest complete
    snapshot's `diagnostics`). `ImpactResult.coverage` is a *required*
    field, deliberately -- a caller cannot construct a result that forgets
    to say whether it is complete. Confidence stays exactly where it was:
    per-hop, per-relationship, never touched by coverage
    (`test_coverage_never_changes_a_hop_s_confidence` is the regression
    test for that specific promise).
  - **Two new, previously-unused extensibility points did the whole job,
    zero `IR_VERSION` bump needed.** `AdapterCapabilities.known_limitations`
    (`ports/adapters.py`, new field, not part of the versioned IR contract)
    is where an adapter states its own gaps in plain language.
    `Entity.metadata`/`Snapshot.diagnostics` (both existing, both
    previously empty in practice) are where `application/indexing.py`
    writes the per-column unresolved count and the pipeline-wide
    limitations list, respectively -- `core`'s already-open extensibility
    points turned out to be exactly the right shape for this, without
    touching the versioned schema at all.
  - **Typed object provenance, widened by one form (the first concrete
    coverage improvement).** `adapters/sqlalchemy/adapter.py::
    _typed_parameter_instances` (new) recognizes `def process(self,
    payment: Payment)` as a second provenance form alongside the existing
    `payment = Payment()` local instantiation -- "typed object provenance"
    named explicitly as the general concept, of which a return value, an
    attribute, a collection element, and a factory call remain unsupported
    and stay named in `known_limitations` rather than silently absent. An
    attribute access recognized as *plausibly* relevant (its name matches a
    real column) but not resolvable through any supported form is reported
    too, as a new `sqlalchemy.unresolved_field_access` observation kind --
    scoped tightly to parameters specifically (not arbitrary local
    variables) so an unrelated same-named attribute on some genuinely
    unrelated object (a gateway SDK's response, say) is never miscounted.
  - **Proven against the real, load-bearing fixture, not only synthetic
    ones.** `PaymentService.mark_refunded` (new method,
    `tests/fixtures/fastapi_checkout/payments/services.py`) writes
    `payment.status` through a typed parameter; every existing test
    asserting an exact `reverse_impact` affected-entity set for
    `payments.status` had to be updated to include it --
    `test_fastapi_sqlalchemy_together.py`, `test_impact_historical.py`,
    `test_identity_evolution.py`, `test_mcp_read_surface.py` -- which is
    itself the proof this reaches real code, not only a hand-built unit
    fixture. `payments/reporting.py` (new file, raw SQL over
    `payments.status`) is what `tests/integration/test_mcp_read_surface.py`
    points at to prove the limitations warning survives a real MCP
    protocol round trip, not just an internal call.
  - **Deliberately not attempted: raw SQL parsing.** Per the milestone's
    own explicit instruction -- building a parser now would repeat the
    exact mistake being corrected. The limitation is named, not chased.
  - **Not attempted in this pass**: `NOT_FOUND_AFTER_COVERAGE` in its
    strictest form (an adapter affirmatively confirming no relevant access
    exists at a specific site, rather than recognizing one it could not
    resolve) -- v0.2's `unresolved_access_count` is an honest, narrower
    stand-in, documented as such in `docs/IR.md`; per-query (rather than
    pipeline-wide) adapter attribution for `limitations`, which would need
    persisting which adapters actually produced a given snapshot; and the
    second agent-experiment scenario (a fixture with genuinely full
    coverage, to test whether `reverse_impact` becomes unwieldy at volume)
    -- deliberately deferred until coverage semantics existed to interpret
    that scenario's results honestly.
- [x] **Impact Analysis v0.3: structured limitations + return-value
      provenance** — driven by *re-running* the same agent experiment
      twice more after v0.2 landed, not by planning ahead. First re-run: a
      real bug, not a research finding — `coverage` was correct on the
      wire but the `reverse_impact`/`forward_impact` MCP tool
      *descriptions* never told an agent it existed, so the re-run agent
      never looked, even reading the exact JSON that carried it. Fixed by
      editing the tool descriptions alone (`mcp/server.py`) — a one-line
      lesson worth keeping: correct data on the wire is necessary, not
      sufficient; an agent cannot act on a field it was never told to
      check. Second re-run, confound removed: the agent used `PARTIAL`
      exactly as intended — read `limitations`, deliberately investigated
      the surfaces named, and found a *third*, unnamed gap (`getattr`
      dynamic dispatch) on its own, because `PARTIAL` alone had taught it
      not to trust a short result. Its own wishlist asked, unprompted, for
      limitations "categorized by kind" — the actual driver of this
      milestone, not a plan made in advance of the evidence.
  - **A limitation describes a capability boundary, not an individual
    missed edge.** `ImpactCoverage.limitations` is now `tuple[Limitation, ...]`
    (`ports/adapters.py`: `Limitation{kind: LimitationKind, scope:
    LimitationScope, detail: str}`), replacing v0.2's flat
    `unresolved_access_count: int` entirely. A count invites the exact
    false precision `Confidence`'s ordinal design was built to refuse, one
    level up ("3 unresolved" reads as "probably fine" regardless of
    whether the true number is 3 or 3,000) — grouping and filtering now
    happens on `kind`, a closed, small vocabulary (`RAW_SQL`,
    `UNTYPED_PARAMETER`, `RETURN_VALUE_PROVENANCE` today), never on a
    magnitude. `docs/IR.md`'s "coverage is not confidence" entry has the
    full reasoning, including why `LimitationKind` lives in `ports/adapters.py`
    rather than `core/enums.py` despite being exactly the kind of small
    closed vocabulary that module is for: it describes a boundary of
    *Hashira's own analysis*, not a fact about the analyzed system, so it
    never touches the versioned IR contract — this milestone needed zero
    `IR_VERSION` bumps, on either pass, for the same reason v0.2 didn't.
  - **Structural vs. conditional limitations, now split.** v0.2's
    `AdapterCapabilities.known_limitations` blended "always true"
    (raw SQL) with "true only for entities actually affected" (instance-
    type tracking gaps) into one blanket list attached to every query.
    v0.3 keeps `known_limitations` for structural gaps only; conditional
    gaps are reported per-entity, from that entity's own
    `metadata["coverage_limitation_kinds"]` (`application/indexing.py`,
    populated from `sqlalchemy.unresolved_field_access` observations each
    now tagged with *which* provenance form fell short) — declaring a
    conditional gap unconditionally would have been the same "blanket
    pessimism" overcorrection this milestone exists to avoid, in the
    opposite direction from v0.1's silence.
  - **Typed object provenance, widened by a second form: return values.**
    `adapters/sqlalchemy/adapter.py::_return_value_instances` resolves
    `payment = repo.get(...)` when `repo`'s own type is already known
    (local instantiation or a typed parameter -- both needed broadening
    from model-only to *any* known class, so a non-model service/repository
    object is trackable too) and `repo.get`'s own method definition,
    looked up cross-module wherever it actually lives, carries an explicit
    `-> Payment` return annotation. Deliberately one-hop: a return value
    that is itself another return value, or a call through `self.method()`
    (`resolve_expr`'s existing `SELF` handling only covers `self.attr`,
    not a bare `self`), are explicitly *not* attempted -- named as
    remaining gaps, not guessed past. When the callee's own type is
    unknown at all (an external SDK's client), nothing is claimed either
    way, matching the same false-positive discipline the parameter form
    already established. Proven against the real `fastapi_checkout`
    fixture (`PaymentRepository`/`PaymentService.close`, new), the same
    way the typed-parameter form was proven in the prior pass.
  - **Three further categories, found and deliberately not built**:
    framework/runtime reflection (Pydantic `orm_mode` reading an ORM
    attribute with no source-level access to see at all -- needs a
    framework adapter producing explicit serialization evidence, not
    another AST case bolted onto the SQLAlchemy adapter), dynamic dispatch
    (`getattr(obj, name)(...)` -- a call-resolution gap, not a
    field-access provenance gap), and chained/`self.method()` return-value
    provenance. Each is logged in `docs/IR.md` so the next widening picks
    the right abstraction on purpose.
  - **Not attempted in this pass**: any of the three categories above, and
    the second agent-experiment scenario (full-coverage stress test) --
    still deferred, now behind two additional, unplanned iterations rather
    than one.
- [x] **Impact Analysis v0.4: the field-access coverage audit** — a third
      re-run of the same agent experiment, structured limitations live,
      produced the cleanest result of the series: the agent read
      `coverage.limitations`, specifically investigated the one disclosed
      category (`RAW_SQL`), and confirmed it was real. It also found
      something the tool never disclosed at all -- `Payment(status=x)`
      constructor-keyword writes tracked as neither a resolved edge nor a
      declared limitation -- by manually cross-checking the tool's
      relationship list against files it had already read. The verdict:
      *"declared limitation ≠ exhaustive limitation inventory,"* so a full
      manual sweep stayed the rational move even with working, well-
      understood coverage. The fix scoped here is not "add a fourth
      limitation kind" (same disclosure gap, one row over) but an audit:
      enumerate every syntactic way a mapped field can be touched, and
      require each one to land on `SUPPORTED` or a declared
      `LimitationKind` -- never silence -- as an executable artifact
      (`tests/unit/test_sqlalchemy_coverage_matrix.py`), not a claim in
      prose. `docs/IR.md`'s "field-access coverage audit" entry has the
      full table and reasoning.
  - **Constructor keyword writes are now `SUPPORTED`.** `Payment(status=x)`
    is deterministic -- the call itself names the model, no instance
    tracking needed. `adapters/sqlalchemy/adapter.py::_extract_field_accesses`
    now matches an `ast.Call`'s keyword arguments against the resolved
    callee's columns; `Payment(foo="bar")` invents nothing; an import
    alias still resolves; two constructor calls to the same field dedupe
    into one relationship with both call sites' evidence retained (the
    existing `adapters/_dedup.py` merge -- no normalizer change needed,
    since this is just another `sqlalchemy.field_access` observation); and
    the edge is retracted by the existing reconciliation mechanism once
    the keyword disappears on re-index, proven by a new integration test
    (`tests/integration/test_sqlalchemy_constructor_writes.py`).
  - **Two more gaps now reported, deliberately never resolved.**
    `getattr(x, "status")`/`setattr(x, "status", v)` with a *literal*
    field name are now detected and tagged `DYNAMIC_ATTRIBUTE_ACCESS` --
    resolving them was explicitly rejected even though the literal makes
    it technically easy, since supporting one literal-name case invites
    `getattr(x, field_name)`/`getattr(x, mapping[key])` next, each a
    fundamentally different and unbounded kind of guessing; a computed
    name stays correctly silent, not a false limitation. `self`/`cls`
    method-return values and one level of chained return value
    (`b = a.other()` where `a` is itself return-value-sourced, recognized
    via a new `call_derived_names` set rather than guessed at generically)
    are now tagged `RETURN_VALUE_PROVENANCE` -- `self`/`cls` are
    recognized by name without resolving which class they refer to,
    deliberately, so this stays a reported gap rather than an ad-hoc
    exception to "unresolvable stays a limitation."
  - **`LimitationScope` renamed for semantics, not aesthetics.**
    `FIELD_ACCESS` implied every limitation was an AST attribute-access
    operation -- true for three kinds, but not `RAW_SQL`, which has no
    attribute node at all; it references a field through a completely
    different surface. Split into `ORM_ATTRIBUTE_ACCESS` and
    `RAW_SQL_REFERENCES`, so a caller can ask about gaps in one surface
    without the other. Zero `IR_VERSION` impact, same as v0.2/v0.3 --
    `LimitationKind`/`LimitationScope` describe a boundary of Hashira's
    own analysis, not the analyzed system.
  - **Deliberately still not attempted, kept separate on purpose**:
    framework/runtime reflection (Pydantic `orm_mode` -- needs a framework
    adapter producing its own serialization evidence, not another AST
    case here), dynamic dispatch with a *computed* name (no literal to
    check, so nothing can even be asserted), and chains beyond one hop.
    `docs/IR.md` has the reasoning for each.
  - **Not attempted in this pass**: the three items above, and the
    second agent-experiment scenario (full-coverage stress test) -- the
    latter now deliberately waiting on a fourth re-run first, per the
    milestone's own new success criterion: does an agent, given this
    milestone's changes, discover *any* unsupported field-impact mechanism
    Hashira neither modeled nor disclosed? If several adversarial passes
    turn up nothing new, `PARTIAL` starts meaning "here are the specific
    surfaces you still need to inspect manually" rather than "here are
    some limitations we happen to know about" -- only then does beating
    the ~55k-token no-Hashira baseline become a reasonable expectation
    rather than a hope.
  - **The fourth re-run closed the loop, and completed this milestone's
    own contract.** It independently rediscovered exactly the two gaps
    already logged above -- dynamic dispatch and framework reflection --
    and nothing outside the documented taxonomy across four passes: the
    taxonomy itself has stabilized. But both were correct in this
    document's own prose and still unreachable from
    `AdapterCapabilities.known_limitations` at runtime, so an agent still
    had to rediscover them by hand. Closed with the smallest possible
    change: `DYNAMIC_DISPATCH` (`PythonAdapter`, which owns `CALLS`-edge
    construction) and `FRAMEWORK_REFLECTION` (`FastAPIAdapter`, which owns
    response-model handling) join `RAW_SQL` as structural, unconditional
    `known_limitations` -- no per-call-site detection, no conditional
    per-entity attachment, no `Confidence` change, no new AST analysis.
    `docs/IR.md`'s "closing the disclosure gap" entry has the full
    account. This is part of Impact Analysis v0.4, not a new milestone --
    it completes the disclosure contract v0.4 already committed to,
    rather than opening a new one.

## Phase 4 — Agent integration

- [x] **MCP read surface v0.1** (`src/hashira/mcp/`) — the first thing an
      external agent can talk to, deliberately "very small and read-only":
      a transport layer over capabilities Hashira already proves
      internally, not a new source of intelligence.
  - **One architecture rule, enforced structurally**: `Agent -> MCP ->
    Application services -> System IR/Storage/History`, never the reverse
    and never a shortcut across it — `mcp/server.py` imports only from
    `hashira.application`, never `ports.repositories.UnitOfWork`'s
    sub-ports, storage, or adapter internals directly. Two new, otherwise
    unnecessary `application/` modules (`graph.py`'s `get_entity`/
    `get_relationships`/`get_entity_at_revision`, `search.py`'s
    `search_entities`) exist purely so MCP has a proper intermediary to
    call even for a one-line id lookup — convenience was traded for
    honoring the diagram literally.
  - **Seven tools, IDs first**: `get_entity`, `get_relationships`,
    `query_at_revision`, `reverse_impact`, `forward_impact`,
    `follow_lineage`, and `search_entities` — the one name-based tool,
    exact/substring text matching only, explicitly "discovery, not
    identity" (no fuzzy ranking, no embeddings). Every other tool takes an
    opaque entity id; the canonical agent flow chains them exactly as
    specified: `search_entities` → id → `reverse_impact`/`forward_impact`
    → `get_entity`/`get_relationships` → `query_at_revision` →
    `follow_lineage`.
  - **`follow_lineage`** (`application/impact.py`) is new: walks
    `SUPERSEDES` edges in both directions from one entity, stopping (never
    guessing) at a fork — more than one predecessor or successor candidate
    — since choosing a branch would be exactly the kind of invented
    certainty this project refuses elsewhere. `query_at_revision`'s MCP
    tool deliberately does *not* return the module's own `HistoricalGraph`
    (the whole graph at a revision) but composes `get_entity_at_revision` +
    `get_relationships(revision=...)`, scoped to one entity's neighborhood
    — the historical primitive, without becoming the "give me the entire
    graph" endpoint explicitly kept out of scope.
  - **Losslessness is the non-negotiable requirement, not an aspiration.**
    `mcp/serialize.py` reuses each `core` IR record's own proven JSON form
    (`IRModel.model_dump(mode="json")`) rather than hand-picking fields — a
    `Relationship` crossing the MCP boundary still carries its
    `knowledge_class`, `confidence`, `origin`, `evidence_ids`, and
    `valid_from_revision`/`valid_until_revision`. `reverse_impact`/
    `forward_impact` return every path and every hop, never collapsed to a
    flat list of names; no prose, no ranking, no summarization happens
    inside Hashira at all — the marquee test below asserts real `Evidence`
    records (with `locator`/`source`) survive serialization on every
    traversed hop, not just that a relationship type string came through.
  - **A real, honest asymmetry this surfaced, not papered over**: a
    `SUPERSEDES` relationship the identity resolver mints
    (`identity/resolver.py`) carries `confidence` and a `metadata.rationale`
    string but no `evidence_ids` — unlike a `CALLS`/`WRITES` edge from
    static analysis, lineage evidence lives on the *entity's own*
    `identity_claims` (e.g. `DECLARATION_LINEAGE`), not as attached
    `Evidence` rows on the edge. `follow_lineage`'s MCP tool still returns
    both — the relationship's rationale and the successor entity's full
    claim list — so an agent can see *why* Hashira believes a lineage edge
    is real even though `LineageHop.evidence` itself is empty for this
    case; asserted explicitly in the marquee test rather than assumed.
  - **The marquee integration test**
    (`tests/integration/test_mcp_read_surface.py`) makes a real MCP
    protocol call — `mcp.client._memory.InMemoryTransport` +
    `mcp.ClientSession`, no subprocess, no external client "to keep
    unrelated variables out" — against `build_server()` wired to the same
    combined FastAPI + SQLAlchemy + Git fixture every framework/temporal/
    identity milestone before this one already proved out: search
    "Payment" → find `payments.status` → `reverse_impact` → service →
    route handler → route, with every hop's evidence intact; then, after a
    declaration-level rename commit (`payments.status` → `payments.state`,
    the exact fixture from Identity Resolution v0.2), the *old* entity id
    → `follow_lineage` → the new entity id → `reverse_impact` at the later
    revision, still correctly empty until the service catches up, and
    still resolvable by the original id via `resolved_from`. Proves an
    external agent can discover a system concept, inspect why Hashira
    believes it exists, understand its impact, travel through history, and
    continue reasoning about the evolved concept — without knowing
    anything about Django, FastAPI, SQLAlchemy, Git internals, SQLite, or
    Hashira's own Python implementation.
  - **`hashira mcp --db <path> --system <slug>`** (`cli/main.py`) makes the
    already-declared `[project.scripts] hashira` entry point real for the
    first time — plain `argparse`, one subcommand, running the built
    server over stdio (`MCPServer.run(transport="stdio")`). Surfaced a
    latent, pre-existing mypy gap: `ports.repositories.UnitOfWork`'s
    mutable Protocol attributes make no concrete backend (`SqliteUnitOfWork`,
    `MemoryUnitOfWork`) a *structural* subtype of it under strict invariant
    matching, even though every method callers actually use is satisfied —
    invisible until this CLI became the first `src/hashira`-scoped (mypy-
    checked) call site to bind a concrete `Database.unit_of_work` to a
    `Callable[[], UnitOfWork]` parameter; every earlier caller lived in
    `tests/`, outside mypy's `files` scope. Documented with one explicit,
    narrowly-scoped `cast` rather than weakening the protocol.
  - **Explicitly out of scope for v0.1, per the milestone's own
    instruction**: mutation tools, `apply_change`, code editing, shell
    execution, Git commits, agent-triggered indexing, an LLM provider
    inside Hashira, authentication/remote multi-tenancy, a hosted server,
    WebSocket transport, and a "give me the entire graph" endpoint. Local
    stdio, one system bound at `build_server()` construction, is
    deliberately the whole surface.
  - **Not attempted in this pass**: the optional seventh-tool question was
    already settled the other way — `search_entities` shipped as part of
    the seven, not held back, since manual testing needed it immediately;
    a real external MCP client (Claude Desktop, Codex) exercising this
    server is the user's own explicitly stated next step, deliberately
    *not* started here to keep this milestone's own definition of done
    free of client-specific variables.

- [x] **Impact Presentation v0.1: a projection, not a second source of
      truth** (`application/impact.py`'s `ImpactGroup`/`ImpactSummary`/
      `summarize_impact`, `mcp/serialize.py`'s `serialize_impact_summary`,
      `mcp/server.py`'s `summarize_impact` tool) — a fifth agent
      experiment, run against a deliberately large fixture (36 files, a
      "broad checkout" system) this time rather than the coverage-taxonomy
      fixtures before it, put a *correct* `reverse_impact` result (35
      paths, 35 entities, `coverage: PARTIAL`) in front of a real agent and
      watched it build its own tooling anyway: a script to flatten 373KB of
      per-hop-duplicated entity JSON, then a second, manual pass grouping
      the flattened list by directory because the flat path list gave it
      no structure to reason over. It named the fix itself, unprompted:
      *"a named-cluster grouping would likely have both sped up my
      synthesis and made the one real gap I found easier to spot sooner."*
      `docs/IR.md`'s "Impact Presentation v0.1" entry has the full account.
  - **Two rules, both held to a test, not just prose**: `ImpactResult`
    itself does not change at all — `summarize_impact` computes a
    projection *from* an already-produced result, never a new traversal,
    never a new inference. And a summary never carries a full entity
    record, only a bare `entity_id` + display name per group member —
    `get_entity`/`get_relationships` are the drill-down. The invariant this
    guarantees, `test_summarize_impact_never_invents_an_entity_id`
    (`tests/unit/test_impact.py`), asserts the *set* of every id named
    across a summary's groups equals, exactly, the canonical result's own
    affected-entity-id set: nothing invented, nothing dropped.
  - **Grouping is derived from existing IR structure, not a new
    vocabulary.** `_group_key` groups by the directory an entity's own
    `Entity.source.file` lives in — already-present data, zero `IR_VERSION`
    bump. Run against the same fixture, this reproduces, exactly, the
    clustering the agent built by hand (`app/routers`: 16, `app/services`:
    8, `tests`: 8, `app/repositories`: 2, `app/workers`: 1) without
    inventing an `API_LAYER`/`SERVICE_LAYER`/`PERSISTENCE_LAYER` vocabulary
    that a differently-organized repository (commands/events/consumers/
    projections) would not share. No importance ranking either — groups
    order by `entity_count` descending only; deciding a service matters
    more than a test is left to the agent, per the milestone's own explicit
    instruction not to invent an importance score.
  - **A new, separate MCP tool, not a mode flag** — `reverse_impact`/
    `forward_impact` stay "the authoritative, detailed analysis";
    `summarize_impact` is "an agent-oriented projection of that analysis,"
    kept as a distinct tool so the distinction is visible at the protocol
    level rather than buried in a parameter an agent might not read.
    `coverage` crosses into the projection unchanged and stays first in
    both the dataclass field order and the MCP serialization — a caller's
    first read answers "how much should I trust this" before "what's in
    it," matching `serialize_impact_result`'s own existing ordering.
  - **Explicitly not attempted, per the milestone's own instruction**: no
    relevance/importance ranking of groups or entities; no new inference
    beyond what the underlying `reverse_impact`/`forward_impact` already
    produced; no core IR vocabulary for "layers." A second, differently-
    shaped stress fixture — to test whether directory-based grouping
    generalizes past this one repository's own conventions, or merely
    fits it — is deliberately deferred to a follow-up milestone, once this
    projection has been re-validated against the same fixture that
    surfaced the problem in the first place.
  - **Addendum — re-validated, then generalized.** Re-run against the
    unmodified `checkout_broad` fixture: `summarize_impact` cut the payload
    from 373KB to ~4KB and the agent neither rebuilt the grouping nor
    called `reverse_impact` more than once, using it only to fetch evidence
    it had already decided it needed. A second, deliberately different
    fixture followed (`order_events` — commands/consumers/workers/
    projections/API/an external gateway boundary, with one shared service
    called from three unrelated entry points, specifically to see whether
    directory-based grouping was fitting one repository or a real
    structural signal): the same code produced a coherent 9-group summary
    with no vocabulary change, correctly isolated the shared service as
    its own single-entity group distinct from its three unrelated callers,
    and cut a 254KB `reverse_impact` result to ~4.9KB. Across both, the
    agent needed every individual member's name (not just a group's size
    and representative) only when the task itself demanded naming every
    affected symbol, and even then satisfied that need from the
    `reverse_impact` payload it already had for evidence, never through
    per-id `get_entity` calls — so the `entity_ids`-plus-one-representative
    shape stays as designed; logged as a weighed-but-not-acted-on
    hypothesis rather than a confirmed gap, deliberately, per the
    milestone's own "don't put information into a projection merely
    because it's available" principle.

- [x] **Real Repository Pilot v0.1, Phase 1: index a repository Hashira has
      never seen** — the first test of the whole system against a real
      codebase (`oota.app`'s IVR backend: FastAPI + async SQLAlchemy 2.0 +
      Alembic, ~350 files, 22 real commits, multi-channel SMS/WhatsApp
      notification logic) rather than a fixture built to exercise a
      specific adapter capability. Indexing itself passed cleanly: 0
      crashes, 0 errors, 0 duplicate entities, 0 duplicate relationships,
      ~3 seconds, resolving straight through SQLAlchemy 2.0's `Mapped[...]`/
      `mapped_column(...)` style, Postgres-dialect `JSONB`/`UUID` columns,
      and `from __future__ import annotations` deferred typing — none of
      which any existing fixture had exercised.
  - **The one real finding, and it's a good one**: the repository is a
      monorepo (`backend/` and `frontend/` siblings under one Git root),
      and indexing at the Git root instead of the actual Python root
      silently dropped `WRITES`/`READS`/`CONTAINS`/`IMPORTS` entirely and
      cut `CALLS` by two-thirds — with zero errors reported. `docs/
      ADAPTERS.md`'s new "known limitation — analysis root vs. repository
      root" entry has the full account. Deliberately **not fixed this
      pass** — see that entry for why an auto-detection heuristic drawn
      from one pilot repository was rejected for the same reason
      Impact Presentation v0.1 refused to invent a layer vocabulary from
      one fixture. Logged as a roadmap item, workaround applied (index the
      actual Python root), and the resulting graph — 445 entities, 946
      relationships across 9 types — is what Phase 2 uses.
  - **Explicitly not attempted in this pass**: fixing the analysis-root
      detection; a coverage-style warning when a nonzero file set resolves
      to zero cross-file relationships (the concrete future direction
      `ADAPTERS.md` names); a second real-repository pilot, deliberately
      held back the same way a second stress fixture was, until Phase 2
      (a real agent, on this same repository, doing real maintenance
      reasoning — not another indexing pass) has run.

- [x] **Real Repository Pilot v0.1, Phase 2: a real maintenance task, not
      another impact-of-X question** — two fresh agents, same real
      repository, same real task ("convert `NotificationService`'s
      always-send-both-channels design to an SMS-first fallback; find every
      affected area, don't touch code yet"), one with only normal file
      tools, one with those plus the Hashira MCP query CLI. Both reached the
      same correct, complete affected-area list and independently converged
      on the same core design question (what a never-attempted WhatsApp
      channel should mean for `NotificationResult.whatsapp`/`all_succeeded`)
      — a good sign the task itself was well-posed, not an artifact of one
      agent's approach.
  - **The consequential asymmetry wasn't in the final answer, it was in how
      each agent got there.** `reverse_impact` on `send_notifications`
      returned 8 paths, every one rooted in the test file — zero production
      callers, including the actual one (`IvrService.handle_webhook`).
      `coverage.status` was `PARTIAL`, but none of its three disclosed
      limitations (`DYNAMIC_DISPATCH`, `FRAMEWORK_REFLECTION`, `RAW_SQL`)
      describe this miss — the call in question,
      `self._notification_service.send_notifications(...)`, is completely
      static, not dynamic dispatch or reflection. The Hashira-assisted agent
      caught the gap itself (it already knew the caller existed from its own
      grep, got suspicious when `reverse_impact` didn't include it, and used
      `get_relationships` to confirm the edge really was missing rather than
      assume completeness) — good agent judgment, but it means Hashira, for
      this task, was not a net win: baseline used ~10 searches/15 files/2
      `git log` calls/0 scripts/83k tokens/277s and reached the answer via
      grep alone; the Hashira-assisted run used 7 Hashira calls + ~9
      searches/14 files/0 `git` calls/1 throwaway script (to page through
      the ~2400-line raw JSON)/**112k tokens**/236s — more tokens, for a
      graph that had to be independently audited rather than trusted. One
      more honest data point: the agent never touched `summarize_impact` at
      all — the real impact set here (8 paths) was too small to need it,
      a reasonable, evidence-consistent choice, not a gap.
  - **Root cause, traced to `resolve.py`**: `self._notification_service =
      NotificationService(...)` in `IvrService.__init__`, called as
      `self._notification_service.send_notifications(...)` from
      `handle_webhook` — a different method. `SELF` only ever resolved a
      single attribute hop (`self.method()`, `len(suffix) == 1`);
      `LOCAL_INSTANCE` only ever tracked a name assigned *within the same
      function body*. Neither covers assigning a collaborator once in
      `__init__` and calling it from every other method — arguably the most
      common dependency-composition idiom in Python — so the call fell
      straight to `UNRESOLVED`, silently, with no disclosed limitation
      naming this specific gap.
  - **Fixed this pass, deliberately small** — unlike the analysis-root gap,
      this one is bounded and mechanical, and now backed by real evidence of
      real impact (a graph gap an agent had to independently catch and
      audit around, on a task that mattered), which is exactly the bar this
      project has held out for all along. `resolve.py` gains a fourth
      resolution kind, `SELF_ATTRIBUTE`; `extractor.py`'s new
      `_self_attribute_types` populates it once per class, from `__init__`
      alone, via the exact same restricted mechanism `LOCAL_INSTANCE`
      already uses for a same-function local (`self.<attr> = KnownCallable
      (...)` / `self.<attr>: T = KnownCallable(...)`, resolved only against
      `IMPORT`/`MODULE_LOCAL` — never a guess layered on a guess).
      `docs/ADAPTERS.md`'s "Resolution kinds" entry has the full account.
      8 new regression tests (`tests/unit/test_python_resolve.py`,
      `tests/unit/test_python_extractor.py`) hold the four cases named for
      this fix, plus the "don't guess a sibling attribute" negative case;
      253 pre-existing tests still pass unchanged. The acceptance criterion
      was not "the new fixture passes" — it was re-running the actual real
      repository: re-indexed, `CALLS` went 301 → 314, and `reverse_impact`
      on `send_notifications` now includes `IvrService.handle_webhook` with
      a direct, `CERTAIN`-confidence `CALLS` edge.
  - **Explicitly not attempted, on purpose, same reasoning as
      `RETURN_VALUE_PROVENANCE`'s own restraint**: `self.<attr>` assignment
      outside `__init__`; a right-hand side other than a bare `Call` (the
      `self._mcube = mcube_client or MCubeClient()` fallback-default idiom,
      genuinely common in this same real repository, still resolves to
      nothing); multi-hop attribute provenance. Widening any of these is
      bounded future work, not a blocker — the specific, evidence-backed gap
      this pass exists to close is closed.

- [x] **Real Repository Pilot v0.1, Phase 2 continuation: two more
      confirmed resolution gaps, closed the same way** — a follow-up
      benchmark against the same, still-unmodified real repository ran
      `reverse_impact` on a live, previously-buggy config value and got
      back "no production callers, only tests": false, for two distinct,
      independently-confirmed reasons, neither disclosed by any existing
      `LimitationKind`. First, the exact fallback-default idiom
      (`self._settings = settings or get_settings()`) the *previous* round
      had just named as deliberately unresolved turned out to be this
      codebase's dominant composition style, not a rare shape — it broke
      `SELF_ATTRIBUTE` for the one call that mattered. Second, entirely
      separate: an ordinary typed function parameter calling a method on
      itself (`def receive(inbound: IvrWebhookInbound, service:
      Annotated[IvrService, Depends(...)]): inbound.resolve(...)`) — the
      single most common shape in any framework's request-handling code —
      was never resolved by anything, regardless of the fallback question.
      Together these explained the entire missing production call chain
      the benchmark found. `docs/ADAPTERS.md`'s continuation of the same
      entry has the full account.
  - **Both closed, each exactly as narrowly-scoped as the fix before it**:
      `extractor.py`'s `_constructor_call` now also accepts `provided or
      KnownCallable(...)` (only when the `or` chain's last operand is a
      literal call) for both `_local_instance_types` and
      `_self_attribute_types`; a new `_parameter_instance_types` resolves
      a parameter's own annotation (`Annotated[T, ...]` reduced to `T`
      first) through the same IMPORT/MODULE_LOCAL-only restriction,
      deliberately kept separate from `adapters/sqlalchemy`'s own
      narrower, model-only `_typed_parameter_instances`. 12 new regression
      tests; all pre-existing tests (including the full SQLAlchemy adapter
      and FastAPI+SQLAlchemy integration suites) pass unchanged.
  - **The acceptance criterion, again, was the real repository, not a new
      fixture**: re-indexed, `CALLS` edges went 314 → 348 with zero new
      duplicates and zero change to `WRITES`/`READS`/`CONTAINS` (confirming
      no interaction with the untouched SQLAlchemy-specific mechanism).
      Verified individually: `receive_ivr_webhook` now shows real `CALLS`
      edges to `inbound.obd_skip_reason`, `inbound.resolve`, and
      `service.handle_webhook` (Gap 2, fully closed); `NotificationService.
      _send_sms`/`SmsService.send_and_log` now show real `CALLS` edges into
      `MCubeClient.send_sms` via the exact `mcube_client or MCubeClient()`
      idiom (Gap 1, closed for the class-constructor-fallback case it was
      scoped to).
  - **An honest, not-fully-clean result, caught during verification rather
      than glossed over**: the literal example that motivated Gap 1's fix,
      `self._settings = settings or get_settings()`, still does *not*
      resolve after the fix — `get_settings` is a factory *function*
      returning `Settings`, not a class, and Stage 1 cannot tell "imports a
      class" from "imports a function that returns one" from an import
      statement alone without reading the imported module (cross-file,
      ruled out by this stage's own contract). This is not a regression --
      a bare, non-fallback `self._settings = get_settings()` had the exact
      same limitation before either fix existed — and it produces a miss,
      not a wrong answer (`normalizer.py`'s Stage 2 still safely declines
      to promote a call whose resolved name doesn't exist). Logged as a
      newly-precise, currently-undisclosed boundary of the general Python
      `CALLS` resolver (distinct from `RETURN_VALUE_PROVENANCE`, which
      covers this same class/function ambiguity only for the SQLAlchemy
      adapter's narrower field-access mechanism) — deliberately not acted
      on this round; whether it earns its own `LimitationKind` is an open
      question for whenever a real task next depends on the answer.

- [x] **Real Repository Pilot v0.2: function-local imports** — an agent
      given the published MCP server against a real, previously-unseen
      Django/Kafka backend, asked to investigate a notification pipeline
      with no hints about which tools existed, found `reverse_impact`/
      `get_relationships` on the pipeline's core functions returning
      `DEFINES` edges only — zero `CALLS` edges anywhere in the queried
      neighborhood, `coverage.status: PARTIAL` but with no disclosed
      limitation naming why. The agent's own diagnosis, cross-checked
      against a full manual grep pass it ran in parallel: nearly every
      caller in the pipeline imported its dependency *inside* the calling
      function (`def enqueue_push_notification(...): from common.kafka
      import kafka_publisher; kafka_publisher.push_notification(...)`)
      rather than at module level — the codebase's dominant style for
      breaking circular imports, not a rare shape, exactly the same kind of
      finding the `SELF_ATTRIBUTE`/fallback-default/typed-parameter fixes
      above were each built from.
  - **Root cause, traced to `extractor.py`**: `_module_level_imports`
      explicitly refuses to descend into a function or class body when
      collecting import bindings — by design, since it exists to build the
      *module-level* resolution context. But nothing built the equivalent
      *function-level* context: a function-local import never entered
      `ResolutionContext.imports` for any scope at all, so a call through
      the name it bound fell straight to `UNRESOLVED`, silently, with no
      `LimitationKind` describing this specific, extremely common gap.
  - **Fixed, narrowly, same discipline as every resolver fix before it**:
      `_function_local_imports` (new) scans a function's own body — not
      descending into a *nested* def/class, matching `_local_instance_types`'
      own scoping exactly — for import statements, and its bindings are
      merged into that function's own `ResolutionContext.imports` before
      resolving parameters, local instances, or calls within it: shadowing a
      same-named module-level import within that one function (real Python
      scoping), never leaking to a sibling function that imported nothing
      itself, and available to a nested function through the same `ctx`
      threading that already gives nested functions their enclosing scope
      (a real closure property, not a new mechanism). Because an import
      statement names its target exactly — this is not a guess about a
      name's type, unlike every fix in this section's siblings — the fix is
      complete for this syntactic form, not a partial widening. A companion
      fix (`_all_imports`, renamed from `_top_level_imports`) walks the
      *entire* file for `python.import` Observations, not just statements
      outside a function/class body, so a function-local import now also
      produces a real `IMPORTS` relationship — a second, independent gap
      the same root cause created, since an `IMPORTS` edge is a module-level
      fact regardless of which scope triggers it.
  - **The acceptance criterion**: 5 new regression tests
      (`tests/unit/test_python_extractor.py`) covering direct resolution,
      the `IMPORTS` observation, no leakage to a sibling function, shadowing
      a module-level import of the same name, and a function-local import
      feeding `_local_instance_types` so a subsequently-constructed
      instance's own methods resolve too; all 259 pre-existing tests, mypy
      --strict, and ruff stayed green unchanged. `docs/ADAPTERS.md`'s
      "Fixed in a later round — function-local imports" entry has the full
      account.
  - **Deliberately not attempted, same restraint as the rest of this
      module**: a class-body-level import (as opposed to a function/method
      body) is not bound for resolution, only observed for `IMPORTS` — a
      materially rarer idiom than a function-local one, and nothing forced
      the question yet.
  - **Acceptance-test verification surfaced two more real gaps and one real
      crash before the fix could even be confirmed — all closed as part of
      this same milestone, not deferred**, holding the fix to its own
      acceptance test (`KafkaEventPublisher.push_notification` → all real
      callers → their callers) rather than trusting the unit tests alone:
    1. **A full indexing crash, not a partial failure.** Indexing the real
       repository's actual Python root (`backend/`, per `ADAPTERS.md`'s own
       documented monorepo workaround) rather than its Git root — the
       correct root, previously never exercised this way — raised
       `pydantic_core.ValidationError: Entity.name string_too_short` and
       aborted the *entire* run. Root cause: `backend/__init__.py` (a
       Django project's settings package, itself importable by the root
       directory's own name one level up, while everything under it imports
       unprefixed) computed to an empty dotted name (`module_qualified_name`
       relative to itself), and `Entity(name="")` failed validation instead
       of merely being skipped — directly violating this project's own
       adapter contract ("skip the file, don't abort the run"). Fixed in
       `module_qualified_name` (`extractor.py`): when the computed name is
       empty (`file` *is* `import_root/__init__.py`), fall back to the
       import root's own directory name — real and non-fabricated (it is
       genuinely importable as such one level up), not empty. One
       regression test (`test_module_qualified_name_falls_back_to_root_name_for_the_roots_own_init`).
    2. **Module-level singleton instances, the module-scoped twin of the
       already-logged `self._settings = settings or get_settings()`
       ambiguity.** With the crash fixed and indexing succeeding, the
       target entity itself (`push_notification`) still showed *zero*
       reverse-impact paths despite every upstream caller now resolving —
       `common/kafka/__init__.py` defines `kafka_publisher =
       KafkaEventPublisher()` at module scope, and every caller does `from
       common.kafka import kafka_publisher; kafka_publisher.push_notification(...)`.
       Stage 1 (single-file) can only resolve `kafka_publisher` against its
       own literal import target (`common.kafka.kafka_publisher.push_notification`),
       which names no real entity — it has no way to know, from an import
       statement alone, that the imported name is an *instance* of a class
       defined in a different file rather than a class/function/submodule
       itself. Confirmed a common pattern in this codebase, not a one-off
       (Celery app, Django settings, Kafka config/registry, Firebase
       client — all the same module-level-singleton-client idiom). Fixed
       with a new mechanism spanning both stages: `extractor.py`'s new
       `_module_level_instance_assignments`/`_emit_module_instance` detect
       `name = ClassName(...)` at module scope and emit a
       `python.module_instance` observation (`{qualified_name, instance_of}`);
       `normalizer.py`'s new `_retarget_through_module_instance` — Stage 2,
       which has whole-run visibility Stage 1 deliberately never has — then
       re-targets a call that failed its direct qualified-name lookup
       through the *longest* matching known instance prefix before giving
       up. Two regression tests, including one proving a retarget that
       matches no real method on the instance's class stays unresolved,
       never a fabricated nearby match.
    3. **A pre-existing, unrelated bug found incidentally while writing
       test fixtures for (2), in `resolve.py`'s relative-import handling.**
       `_package_of` always drops the importing module's last dotted
       component to find "its own package" — correct for an ordinary module
       (`shop/checkout.py`, qualified name `shop.checkout`, own package
       `shop`), but wrong for a package's own `__init__.py`
       (`common/kafka/__init__.py`, qualified name already `common.kafka` —
       `module_qualified_name` strips the trailing `__init__` by design),
       where dropping a component walks one package too far up: `from
       .publisher import X` written inside `common/kafka/__init__.py`
       silently resolved to `common.publisher.X` instead of
       `common.kafka.publisher.X` — wrong, not merely unresolved, since a
       plausible-looking wrong target can coincidentally exist. (The real
       repository's own `common/kafka/__init__.py` happens to use an
       absolute import, so this specific bug did not affect the acceptance
       test itself — found by a test fixture mirroring the real file
       layout with a relative import instead, and worth fixing regardless
       since it is real and silent.) Fixed by threading a new
       `is_package_init: bool` flag (`_Walker.is_package_init = file.name
       == "__init__.py"`) through every `bindings_for_import_from` call
       site into `_package_of`, which skips its one truncation exactly when
       `is_package_init` is true — every other level and every ordinary
       module is provably unaffected (a dedicated regression test pins the
       default-`False` behavior byte-for-byte). Four regression tests
       across `test_python_resolve.py`/`test_python_extractor.py`.
  - **Re-verified against the real repository after all three fixes**:
      re-indexed cleanly (0 crashes, 349 files, 2529 entities, 5464
      relationships, up from 5365 after the function-local-import fix
      alone), and `reverse_impact` on `KafkaEventPublisher.push_notification`
      — the acceptance test's actual target — went from 0 paths to 131,
      with all five real production entry points (`notify_hub_assignments`,
      `_publish_push_notification`, `notify_tour_assigned`,
      `evaluate_and_maybe_reroute`, and a smoke script) resolving as direct,
      `CERTAIN`-confidence `CALLS` edges, chained further back through their
      own real callers (Celery tasks, DRF views, management commands) —
      matching the transcript this whole milestone was built from,
      end-to-end, not merely in a synthetic fixture. `coverage.status`
      stayed honestly `PARTIAL` throughout, with the same three structural
      limitations (`DYNAMIC_DISPATCH`, `FRAMEWORK_REFLECTION`, `RAW_SQL`)
      correctly still disclosed — none of these three fixes touch what
      those limitations describe.

- [x] **Hashira 0.2 — Resolution Integrity, R1: the import/call resolver
      gap corpus, and import re-export chains** — a deliberate shift in
      method, not just another fix. The three-bug verification pass above
      produced a stronger update than any single bug did: one real query
      (`reverse_impact` on `KafkaEventPublisher.push_notification`) went
      0 → 131 paths *and* surfaced a crash and two independent resolver
      gaps neither synthetic fixtures nor unit tests in isolation had
      caught. The corrected development loop this milestone commits to,
      permanently, not just for this pass: synthetic resolver matrix ↔ a
      read-only real-repository benchmark, in a tight loop, fixing only
      Hashira — the benchmark repository is never modified, treated as an
      adversarial laboratory, not a target for feature work.
  - **The corpus**: `tests/unit/test_python_import_resolution_matrix.py`
    (19 rows) and `tests/unit/test_python_call_resolution_matrix.py` (13
    rows), structured the way `test_sqlalchemy_coverage_matrix.py` already
    proved out for field-access coverage — a table docstring as the
    reviewable audit surface, each row a real test against the whole-run
    graph `normalizer.normalize` produces, not a mechanism tested in
    isolation. Every import shape and call shape found across this
    project's real-repository pilots to date has a permanent row here,
    including the three real bugs from the immediately preceding
    milestone (function-local imports, module-level singleton instances,
    the `__init__.py` relative-import anchor) — a regression in any of
    them now fails a named, greppable test, not just an assertion buried
    in a feature-specific file. One row is deliberately marked as a real,
    still-open gap rather than closed: a return value's own method call
    (`x = repo.get(); x.method()`) is `SUPPORTED` only for SQLAlchemy's own
    narrower field-access mechanism, not the general Python `CALLS`
    resolver — pinned here so a future widening is a deliberate change to
    this file, not a silent drift.
  - **A fourth real bug, found building the corpus, not the benchmark**:
    writing the re-export row (`pkg/__init__.py` doing `from pkg.sub import
    Thing`, then `consumer.py` doing `from pkg import Thing`) surfaced that
    Stage 1's own literal resolution of `consumer.py`'s import target
    (`pkg.Thing`) never matches a real entity, since `pkg.Thing` is not
    where `Thing` is actually defined — Stage 1 never chases another
    file's own import statement, by design (§10's "never touches another
    file"). This is the module-scoped twin of the singleton-instance gap
    the immediately preceding milestone closed, generalized: both are
    cases where a name Stage 1 resolved against its own literal import
    target needs one more hop, resolvable only with the whole-run
    visibility Stage 2 has and Stage 1 deliberately does not.
  - **Closed by generalizing, not duplicating, the singleton-instance
    mechanism.** `normalizer.py`'s `_retarget_through_module_instance`
    (one-shot, module-instances only) became `_resolve_through_aliases`
    (bounded, iterative, checking `by_qualified_name` after each
    substitution) over a merged `alias_targets` map combining every
    `python.module_instance` observation *and* every `python.import`
    observation's own `importer.bound_name -> target` binding. Neither
    kind is a guess: `from a import b` genuinely makes `a.b` the same
    object as wherever `b` really lives, and `x = Cls()` genuinely makes
    `x` an instance of `Cls` — both are deterministic facts about Python's
    own binding semantics that this run's own observations already
    recorded, never fabricated. Applied uniformly to all three lookup
    sites that do an exact qualified-name match (`CALLS`, `IMPORTS`,
    `EXTENDS`), not just the one that motivated it — a chained re-export
    (two hops: `a/__init__.py` → `b/__init__.py` → `consumer.py`) resolves
    correctly, bounded at 5 hops so a pathological or circular chain
    terminates rather than looping. Two new corpus rows
    (`test_supported__re_export_through_a_package_init`,
    `test_supported__chained_re_export_two_hops`) plus the existing
    singleton-instance tests, unmodified, still pass through the
    generalized path.
  - **Re-verified against the read-only real-repository benchmark, not
    assumed safe from the synthetic corpus alone**: re-indexed cleanly (0
    crashes, 2529 entities, 5498 relationships — up from 5464 after the
    function-local-import/singleton-instance fixes alone, from real
    re-export patterns this codebase's own Django apps use), and
    `reverse_impact` on `KafkaEventPublisher.push_notification` went
    131 → 167 paths on the exact same query, with `coverage.status`
    staying honestly `PARTIAL`. The benchmark repository itself was not
    modified in any way, per this milestone's own stated discipline.
  - **Not attempted in this pass**: R2 (deeper call-resolution widenings
    beyond what the corpus already pins as open, e.g. return-value
    provenance for the general resolver), R3 (instance/attribute
    provenance as its own tracked concept, named as the next likely
    source of hidden false negatives), R4 (attribute/field provenance
    outside SQLAlchemy), a second, differently-shaped real-repository
    benchmark alongside this one, and a class-body-level import binding
    (still only observed for `IMPORTS`, not resolved for `CALLS` — same
    restraint as the milestone before this one).

- [x] **Hashira 0.2 — Resolution Integrity, R2: instance & attribute
      provenance, and a severe CLI regression** — scoped deliberately
      narrow, per the milestone's own instruction not to build a general
      provenance engine: four patterns (constructor instance, module
      singleton, typed parameter, return-value instance) and their
      cross-module/attribute-access variants, classified with a compact
      matrix before fixing anything, matching R1's own discipline.
  - **The corpus**: `tests/unit/test_sqlalchemy_provenance_matrix.py` (16
    rows), the R2 companion to R1's import/call matrices — same
    table-docstring-as-audit shape `test_sqlalchemy_coverage_matrix.py`
    already proved out. Classification surfaced three entirely *silent*
    gaps — worse than a disclosed `LimitationKind`, the exact "unresolved
    looks like no relationship" failure mode this project exists to
    refuse — and, per this milestone's own "classify first, fix the
    highest-leverage 1–2, don't solve everything a matrix reveals"
    instruction, two were fixed and one was deliberately left open:
    - **Fixed: function-local import of an instance's own type.** This
      adapter's field-access resolution builds its own, independent
      `ResolutionContext` (never wired to the general `CALLS` resolver's
      own R1 fix for the identical gap), so `def process(): from
      payments.models import Payment; payment = Payment(); payment.status`
      was invisible entirely. `function_local_imports` was promoted from
      `adapters/python/extractor.py` to shared `adapters/python/resolve.py`
      plumbing — the same "a second, independent adapter needs the exact
      same thing" trigger `_python_index.py` was already extracted for —
      and threaded into `_local_class_instances`/`_typed_parameter_instances`'s
      own context.
    - **Fixed: self-attribute chain (constructor-composed dependency).**
      `self._payment = Payment()` in `__init__`, read as
      `self._payment.status` from a *different* method — the exact shape
      `SELF_ATTRIBUTE` (`resolve.py`) already closed for the general
      `CALLS` resolver, with no equivalent here at all. New
      `_self_attribute_class_instances` mirrors `_local_class_instances`'
      own "any known class" broadening, scoped to `__init__` alone.
      Closing this needed one subtlety `resolve_expr` itself does not
      handle: resolving `self._payment` *alone* (the field access's own
      base expression, one hop from `self`) always reports plain `SELF`
      (treating `_payment` as if it were itself a class-level symbol),
      since `SELF_ATTRIBUTE` only ever fires when resolving the *whole*
      `self.<attr>.<method>()` chain as one call target — `collect`'s own
      attribute-handling branch checks `self_attribute_types` directly for
      this one-hop shape instead of routing through `resolve_expr`, kept
      local to this adapter rather than changing the shared function's
      contract.
    - **Deliberately not fixed, pinned as an open-gap row**: a
      module-level singleton *ORM instance* field access (`payment =
      Payment()` at module scope) — a real gap, but a far rarer real-world
      shape than the general resolver's own service/client singleton fix
      (an ORM row is not usually a process-wide singleton) — and
      `getattr(self._payment, "status")`, silently dropped because
      `_check_dynamic_attribute_call`'s own guard requires a bare
      `ast.Name` base, never considering an `ast.Attribute` one regardless
      of whether the literal field name matches.
  - **A severe, independent bug found by the real-repository verification
    step, not the matrix**: re-indexing the read-only Rider benchmark
    after the matrix fixes landed showed the exact same relationship count
    as before them — a red flag, since the benchmark is a Django project,
    and `entities`/`relationships` upserted had *zero* Django-specific
    contribution of any kind (no `INTERFACE` for a route, no model
    tagging, no `EXPOSES`, no field `READS`/`WRITES`) despite `DjangoAdapter`
    being correctly wired into `framework_adapters`. Root cause, in
    `cli/main.py::_run_index`: `compose_normalizers(enrich_fastapi,
    enrich_sqlalchemy)` — **`DjangoAdapter`'s own `enrich_normalized_run`
    was never included.** `DjangoAdapter().enrich()` ran and produced real
    `django.*` observations exactly as documented; the composed normalizer
    that turns observations into actual `Entity`/`Relationship` records
    simply never knew Django's enricher existed, so every one of those
    observations was silently discarded before ever reaching storage —
    **every `hashira index` run against a real Django project, via the
    actual shipped CLI, has been producing a Python-only graph with zero
    Django framework intelligence**, no error, no warning, looking exactly
    like a complete, successful index. `tests/unit/test_cli_main.py`'s own
    CLI-level Django coverage was zero (its `project` fixture is
    deliberately plain Python) — the lower-level `IndexingService` API
    other tests exercise directly was never actually exposed to this gap,
    which is exactly why unit tests alone missed it and only the real,
    read-only benchmark loop caught it. Fixed with one line
    (`compose_normalizers(enrich_django, enrich_fastapi, enrich_sqlalchemy)`),
    plus a new regression test (`django_project` fixture, a real
    `models.Model` + a service reading/writing a field) that fails without
    the fix — confirmed by reverting it and re-running, not assumed.
  - **A second real crash, found only once Django's normalizer actually
    ran for the first time on a large codebase**: `path("", ...)` — an
    empty route matching a urlconf's own root, idiomatic Django every
    `include()`d app typically has one of — crashed the *entire* indexing
    run (`Entity(name="")`, the identical failure shape the R1 milestone's
    `module_qualified_name` fix already closed once, now recurring in
    Django's own URL-route entity minting). Fixed the same way: `name=route
    or "/"` — "/" is the conventional way this route is actually written
    and discussed, not a fabricated label; `route_qn`/`metadata["url_name"]`
    carry the real, unmodified route string regardless.
  - **Re-verified against the real-repository benchmark, twice — once
    per crash, never modifying the benchmark itself**: entities went
    2529 → 2630, relationships 5498 → 5585, and the graph now contains 86
    real `INTERFACE` route entities, 82 view-tagged symbols, and 72
    `EXPOSES` edges that did not exist in any indexing run before this
    milestone, on the same unmodified Django codebase. `payment.status`-style
    field `READS`/`WRITES` remained at zero even after the fix — logged as
    an open question (how many real local-instantiation-style field
    accesses this specific codebase's Django ORM usage actually contains
    is unconfirmed, not assumed to be a further bug) rather than chased
    further in this pass.
  - **Not attempted in this pass**: R3 (dynamic/reflection boundary —
    `getattr`/`setattr`/dynamic dispatch/framework reflection/decorator-
    generated behavior/string-based registries, detect-and-disclose rather
    than resolve), a second, differently-shaped real-repository benchmark,
    and either open gap pinned above.

- [x] **PyPI distribution, 0.1.0a1 then 0.1.0a2** — the first time this
      project's own installability, not just its behavior against a real
      target repository, became the thing under test. `0.1.0a1`: version
      bumped off `0.1.0.dev0` (a dev release pip won't install by default
      once a less-pre-release version could be preferred), `[tool.hatch.
      version]` single-sourced from `src/hashira/__init__.py` instead of
      two hand-kept-in-sync strings, `authors`/a `License` classifier/a
      real `LICENSE` file added (none existed despite `pyproject.toml`
      already declaring `license = "Apache-2.0"`), the `hashira` name
      confirmed unclaimed on PyPI/TestPyPI via the PEP 503 simple index
      (the HTML project page returns a misleading 200 for a bot-detection
      challenge, not proof of non-existence). README rewritten end to
      end — the previous version still said "contracts only... no indexer
      and no CLI yet," false and actively misleading as the text PyPI
      would show. One real first-run bug found and fixed while smoke-
      testing a clean install: `hashira mcp` without the `[mcp]` extra
      surfaced a raw `ModuleNotFoundError` traceback instead of an
      actionable `pip install "hashira[mcp]"` message.
  - **`0.1.0a2`: `hashira index`**, closing the gap `0.1.0a1`'s own release
      testing surfaced — a colleague could `pip install` the package and
      still had no way to build a database without reading
      `application/indexing.py` and writing a script by hand, exactly as
      every one of this project's own real-repository validation rounds
      had done. `_run_index` (`cli/main.py`) is composition only: the same
      `IndexingService` + adapter wiring every scratch script already
      used, turned into `hashira index ./my-project`. Deliberately no
      "which framework does this project use" selection system — every
      adapter (Python, Django, FastAPI, SQLAlchemy, Git) is always
      offered the chance to look, since each already only produces
      entities for constructs it actually recognizes. Root handling stays
      transparent rather than guessing: an explicit root (default `.`),
      with a stderr note (not a silent guess, not a hard error) when the
      given root differs from the Git repository root it's inside — the
      same monorepo/import-root finding from the real-repository pilot,
      now surfaced by the CLI itself instead of only living in
      `docs/ADAPTERS.md`. Idempotent by construction: an existing system
      is looked up by slug and reused, never re-minted, which is what
      keeps identity resolution working across repeat indexing runs.
      Verified end to end from a fresh venv, wheel-only install, against
      a real external fixture repository: `hashira index` then
      `hashira mcp` then real MCP protocol calls, nothing imported from
      the source checkout. 15 new regression tests; `0.1.0a1`'s own tests
      and validation all still pass unchanged.
  - **Explicitly not attempted in either release**: automatic import-root
      detection (same restraint as the original finding — one pilot
      repository's monorepo shape isn't enough evidence to generalize a
      heuristic from); a `--version` CLI flag (not asked for; the version
      is already consistently readable via `import hashira;
      hashira.__version__`); Typer (`docs/ARCHITECTURE.md`'s own stated
      future direction, not adopted here to avoid an unrelated new
      dependency); any change to indexing/resolution/impact behavior
      itself — this was a distribution-only milestone, twice.

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
- [ ] CLI and JSON APIs can query the same domain services (`hashira mcp`
      exists; a general query CLI/JSON API does not yet).
- [x] MCP can expose read-only intelligence without modifying core — MCP
      read surface v0.1 (`src/hashira/mcp/`), calling only
      `hashira.application.*`.
- [x] Tests cover identity, temporal, provenance and graph invariants
      (`tests/unit/`, `tests/contract/`, `tests/integration/`) — 204 tests as
      of the Python adapter landing, including the adversarial identity suite
      against a real fixture repository, not just the resolver in isolation.
- [x] Documentation includes adapter and extension rules — `ADAPTERS.md` now
      has a worked example (the Python adapter) rather than only a plan.
