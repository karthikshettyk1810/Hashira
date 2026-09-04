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
