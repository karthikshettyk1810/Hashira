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
- [ ] **Identity resolution ladder** and its adversarial fixture suite
      (renames, extract-method, module reorgs) — see
      [IR.md's open item](IR.md#identity-resolution-10--status). Load-bearing;
      should land before Phase 2 writes real entities against it.
- [ ] SQLite implementation of every port (local-first default).
- [ ] PostgreSQL implementation of every port (hosted/team store), held to the
      same contract-test suite as SQLite.
- [ ] `UnitOfWork` implementation with the transactional guarantee from §30:
      a failed indexing run must not corrupt the last known-good snapshot.

## Phase 2 — Indexing (Python-deep, per the MVP scope decision)

- [ ] Git adapter (repository discovery, commit/rename history, revisions).
- [ ] Python language adapter (AST/tree-sitter-based symbol and import
      extraction).
- [ ] Django framework adapter (views/URLs → `INTERFACE`, models → `DATA_ENTITY`).
- [ ] Celery framework adapter (tasks → `PROCESS`, queues → `MESSAGE_CHANNEL`).
- [ ] Postgres data adapter, from migrations/schema (§21: "do not make vector
      search the source of truth" applies here too — schema facts come from
      migrations, not inference).
- [ ] Incremental indexing: a changed file invalidates only affected
      observations (§19).
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
- [ ] Core entities/relationships are stable enough for external consumers
      (needs real adapter usage to prove, not just the contract tests).
- [x] Provenance is mandatory for derived knowledge (enforced in
      `core/evidence.py`, `core/relationships.py`).
- [x] Events are immutable and idempotent (enforced in `core/events.py`;
      idempotency also needs a real store to prove at the persistence layer).
- [ ] Snapshots are reproducible (needs the indexer; the model supports it).
- [ ] Incremental indexing works.
- [ ] At least two language ecosystems map into the same semantic model.
- [ ] Postgres persistence can rebuild a graph without vendor lock-in (SQLite
      too, per this build's storage decision).
- [ ] CLI and JSON APIs can query the same domain services.
- [ ] MCP can expose read-only intelligence without modifying core.
- [x] Tests cover identity, temporal, provenance and graph invariants
      (`tests/unit/`, `tests/contract/`) — will grow as the resolution ladder
      and real stores land.
- [ ] Documentation includes adapter and extension rules (this set of docs is
      the start; needs a worked example once an adapter exists).
