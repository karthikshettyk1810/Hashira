# Adapters

Spec §18–§19. An adapter turns one technology into System IR observations. It
does not decide what those observations *mean* at the system level, and it
never mutates core semantics — it reports what it saw.

**Status:** the ports below are frozen (`src/hashira/ports/adapters.py`). No
concrete adapter exists yet in this repository — that is Phase 2 work. This
document describes the contract an adapter will be written against.

## Concepts kept separate (§18)

- **Language adapter** — Python, TypeScript, Java, Go, Rust, C#, etc.
- **Framework adapter** — Django, FastAPI, Spring, Express, Nest, Rails, etc.
- **Infrastructure adapter** — Docker, Kubernetes, Terraform, AWS/GCP/Azure.
- **Tool/integration adapter** — GitHub, GitLab, Sentry, CI systems, observability.

A language-level construct maps onto a universal semantic type; a framework
adapter adds meaning on top of what the language adapter already extracted.
Example mappings from §18:

| Construct | Universal type |
| --- | --- |
| Django view | `INTERFACE` |
| Spring `@RestController` method | `INTERFACE` |
| Express route | `INTERFACE` |
| Celery task | `PROCESS` (or async workflow node) |
| Kafka topic | `MESSAGE_CHANNEL` |
| Postgres table | `DATA_ENTITY` |

## The ports (`src/hashira/ports/adapters.py`)

```python
class Adapter(Protocol):
    def capabilities(self) -> AdapterCapabilities: ...
    def detect(self, root: Path) -> bool: ...


class LanguageAdapter(Adapter, Protocol):
    def owned_files(self, root: Path) -> Iterable[Path]: ...
    def extract(self, root, files, *, system_id, revision) -> ExtractionResult: ...


class FrameworkAdapter(Adapter, Protocol):
    def enrich(self, root, base, *, system_id, revision) -> ExtractionResult: ...


class InfrastructureAdapter(Adapter, Protocol):
    def extract(self, root, *, system_id, revision) -> ExtractionResult: ...


class IntegrationAdapter(Adapter, Protocol):
    def pull(self, *, system_id, since=None) -> ExtractionResult: ...
```

Every extraction method returns an `ExtractionResult`: observations, entities,
relationships and the evidence that justifies them, plus a list of non-fatal
`errors`. A partial failure produces a smaller graph, not an unattributed one
(§30) — an adapter that cannot parse one file should skip it and record the
error, not abort the run.

## `AdapterCapabilities`

An adapter declares up front, before any extraction runs:

- `ir_versions` — which IR versions it produces records against (§31).
- `languages` / `frameworks` it understands.
- `entity_types` / `relationship_types` it can emit — the capability matrix
  referenced in §38 as the countermeasure to "too many supported stacks."
- `requires_network` — true for anything that calls out (GitHub, Sentry, CI).
  Local-first mode (§29) refuses to run these unless explicitly enabled.

This is what lets the CLI say "your Kafka topics didn't appear because no
adapter with `MESSAGE_CHANNEL` capability is installed," instead of a silent
gap in the graph.

## Writing an adapter, once Phase 2 starts

1. Implement `capabilities()` truthfully — under-declaring hides features,
   over-declaring produces silent gaps that look like bugs elsewhere.
2. `detect()` must be cheap. It runs on every repository Hashira looks at,
   before any adapter is committed to; it inspects file trees and manifests,
   not code.
3. Emit `Observation` records for everything before turning them into
   `Entity`/`Relationship` records — see `core/evidence.py::Observation`,
   which rejects a non-deterministic `origin`. An adapter is a source of
   record; anything it is unsure about does not belong in the adapter's
   output as a fact — it is a job for `IntelligenceProvider` (§26) afterward.
4. Every `Entity` and `Relationship` an adapter produces should cite the
   `Evidence` that justifies it. `Relationship` with `knowledge_class ==
   OBSERVATION` refuses to validate without at least one evidence id
   (`core/relationships.py::_check_edge`) — this is enforced, not a style
   guide.
5. Support incremental extraction: `LanguageAdapter.extract()` takes a
   specific `files` sequence, not "the whole repository," so a one-line
   commit does not force a full re-index (§4, §19).

## First adapter target (per the MVP scope decision in ARCHITECTURE.md)

Python language adapter + Django/Celery framework adapter + a
migrations-or-schema-based Postgres data adapter, over Git. This is the set
needed to produce the §40 example end to end in one stack, which is the
actual proof of the cross-domain graph — before a second language adapter is
added to prove IR portability (§42's "at least two language ecosystems map
into the same semantic model").
