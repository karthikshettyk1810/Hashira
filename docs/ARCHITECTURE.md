# Architecture

Spec references are to `Hashira_System_Design_Specification_v0.1.pdf`.

## Layers (§6)

1. **Ingestion** — discovers and extracts observations from technologies.
2. **Normalization** — maps observations into the universal System IR vocabulary.
3. **Persistence** — stores entities, relationships, events, evidence, snapshots, state.
4. **Knowledge** — derives higher-level structure from deterministic observations.
5. **Intelligence** — semantic classification, impact analysis, risk, causal hypotheses.
6. **Agent interface** — CLI, API, MCP.
7. **Execution** *(future)* — controlled code changes and operations.
8. **Verification** *(future)* — tests, policy, regression, runtime validation.
9. **Feedback** *(future)* — verified outcomes feed back into system history.

This repository currently implements layers 2–3 as **contracts only**: the
domain models in `src/hashira/core/` and the ports in `src/hashira/ports/`.
Nothing above or below them exists yet — see [ROADMAP.md](ROADMAP.md).

## Dependency direction

```
CLI/API → application services → intelligence/knowledge → core domain → ports → infrastructure/adapters
```

`src/hashira/core/` may only import the standard library and `pydantic`. It may
never import an LLM SDK, a web framework, a database driver, or a specific agent
runtime (§6). This is enforced, not just documented — see
`tests/contract/test_core_purity.py`, which walks the AST of every file in
`core/` and fails the build on a forbidden import or an outward reference into
`ports`, `storage`, `adapters`, `ingestion`, `application` or `cli`.

`src/hashira/ports/` defines the Protocols that the application layer uses to
reach persistence, adapters and AI providers. A port may import `core` (it needs
the types) but nothing concrete: no `sqlite3`, no `psycopg`, no HTTP client.
Concrete implementations live in `storage/`, `adapters/`, and wherever the
intelligence provider ends up, and are held to the port's contract by shared
test suites (see `tests/contract/test_ports.py` for the pattern — the in-memory
doubles there will be joined by a SQLite-backed and Postgres-backed suite run
against the *same* test functions once those stores exist).

## Why the core looks the way it does

- **Opaque IDs, semantic identity kept separate** (`core/ids.py`, `core/entities.py`).
  A file path or qualified name is evidence about an entity, never its identity (§10).
- **A closed vocabulary** (`core/enums.py`). Entity types, relationship types and
  the epistemic classes are a fixed, small set. A technology-specific concept
  belongs in `metadata`, not in a new enum member (§4, §38).
- **Confidence is ordinal, not a float** (`core/enums.py::Confidence`). See
  [IR.md](IR.md#confidence-is-ordinal-not-a-float) for why this deviates from §11's
  literal `"confidence": 1.0` example.
- **Evidence is frozen; inferences are not** (`core/evidence.py`). A conclusion's
  status may change as the world reveals itself; the evidence it was built on
  never does (§12).
- **One temporal source of truth** (`core/relationships.py`). See
  [IR.md](IR.md#one-temporal-source-of-truth) for why snapshots are cut points
  over revision-keyed validity rather than a parallel timeline.
- **A `UnitOfWork` port, not a session leak** (`ports/repositories.py`). A failed
  indexing run must not corrupt the last known-good snapshot (§30); that is a
  transactional guarantee the port makes explicit rather than leaving to whoever
  calls it.

## Implementation guardrails (§41)

Before merging a change to `core/` or `ports/`, answer:

1. Does this belong in the universal model or an adapter?
2. Is this a fact, derivation, inference or hypothesis?
3. What is the evidence?
4. Does it need temporal validity?
5. Can the representation survive a technology change?
6. Can an older snapshot still be read?
7. Does this introduce an LLM dependency into the core?
8. Can this be tested with a deterministic fixture?
9. Does incremental indexing remain possible?
10. Does the change make future agent execution safer or more explainable?

If the answer to any of these is unclear, the feature does not go into `core/`
yet — put it behind an adapter or an extension field until it is.

## MVP scope for this build

Per the working decision behind this implementation (not a change to the spec's
long-term ecosystem list in §34): the first vertical slice is **Python-deep and
cross-domain** rather than **multi-language and code-graph-only**. The target
demo is the §40 example end to end —

```
POST /checkout → CheckoutController → CheckoutService → PaymentService
  → CALLS → RazorpayClient
  → WRITES → Payment (Postgres, from migrations)
  → TRIGGERS → PaymentWebhookTask → CONSUMES → payment-webhook queue (Celery)
```

— using Django/Celery/SQLAlchemy-or-Postgres-migrations and Git history, before
a second language is added. The rationale: a call graph across two languages is
a commodity (Sourcegraph/SCIP already do it well); a cross-domain graph — code,
data, async workflow, history, evidence — in one stack is the actual
differentiator described in §38's countermeasure table, and is what the MVP
success criterion in §35 is asking for.

## Storage default

§21 recommends PostgreSQL as the authoritative store. This build makes
**SQLite the local-first default** and Postgres an alternative implementation of
the same `GraphRepository`/`EventStore`/etc. ports, selected by configuration.
Rationale: §4 states "local-first where practical" and the package targets
PyPI (§0) — `pip install hashira && hashira index` should work with zero
infrastructure. Both stores implement the ports defined in
`src/hashira/ports/repositories.py`; neither is permitted to leak a dialect
above that boundary.
