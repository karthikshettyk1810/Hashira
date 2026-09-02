# Hashira

**System Intelligence Runtime** — a durable, evidence-aware, time-aware model of a
software system, for humans and for whichever AI agent you happen to be using.

Hashira is not a coding assistant, a foundation model, or a vector-memory product.
It maintains a **System IR**: a technology-neutral representation of how code,
APIs, data, async workflows, infrastructure, runtime behaviour, incidents and
history relate to one another over time — and exposes that model so an agent can
answer *"what could break if I change this?"* with evidence rather than a guess.

> The pillar underneath intelligent software engineering.

## Status

**v0.1.0.dev0 — contracts only.** This repository currently contains the frozen
core: System IR domain models, storage and adapter ports, and the versioned JSON
Schema. There is no indexer and no CLI yet; that is deliberate (spec §44).

## Design commitments

- **Model-independent.** The core imports no LLM SDK. Ever. Enforced by test.
- **Deterministic before probabilistic.** Parsers, Git, schemas and telemetry
  first; models only to enrich what determinism could not settle.
- **Evidence-first.** Facts, derivations, inferences and hypotheses never
  collapse into one another.
- **Temporal by default.** Relationships are revision-bounded; history is
  readable, not overwritten.
- **Local-first.** SQLite by default, PostgreSQL when you want a shared store.

## Development

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
.venv/bin/pytest
```

## Documentation

| Document | Contents |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layers, dependency direction, merge guardrails |
| [docs/IR.md](docs/IR.md) | System IR contract, versioning, deviations from the spec |
| [docs/ADAPTERS.md](docs/ADAPTERS.md) | How to write an adapter |
| [docs/EVENTS.md](docs/EVENTS.md) | Event model, idempotency, temporal semantics |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Phases and the v0.1 definition of done |

License: Apache-2.0.
