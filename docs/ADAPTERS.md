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

## Python adapter

`src/hashira/adapters/python/` — the first adapter, and so far the only one.
Understands plain Python only; no framework knowledge (§18's separation
holds structurally: nothing here imports Django, FastAPI, or anything else).

**Parser choice: stdlib `ast`, not tree-sitter.** `ast` alone gives modules,
classes, functions, methods, async functions, imports, calls, inheritance,
decorators and precise source locations — everything on the extraction list
below — without a second parser dependency. Tree-sitter earns its place once
Hashira is indexing several languages through one grammar-agnostic interface;
adding it for Python alone, before that need exists, would be solving a
problem this project doesn't have yet.

**Pipeline** (also see `hashira.application.indexing`'s module docstring):

```
discovery.py   -> which files, and each file's import root (src/ layout aware)
extractor.py   -> one file's AST -> Observation[] + Evidence[]   (Stage 1)
resolve.py     -> pure, syntax-only "what might this name refer to?"
normalizer.py  -> the whole run's Observations -> candidate Entity/Relationship (Stage 2)
adapter.py     -> PythonAdapter: the LanguageAdapter port, satisfied
```

Stage 1 (one file) and Stage 2 (the whole run) are deliberately separate.
Stage 1 can only guess at what a call target is — it has no visibility into
other files. Stage 2 is what actually *checks* a guess against every entity
this run produced, before letting it become a `Relationship`. A guess Stage 2
cannot confirm stays exactly what it was: an `Observation`, never a fabricated
entity or a fabricated edge (§10's "uncertainty is data, not failure",
enforced as a hard adapter rule, not a suggestion — see `resolve.py`'s and
`normalizer.py`'s docstrings for the full reasoning).

**Resolution kinds**, ranked by how much a single one is worth trusting
(`resolve.py`): `SELF` (`self.foo`, one attribute level only) and
`LOCAL_INSTANCE` (`x = Cls(); x.method()`, tracked per-function) are the
strongest, followed by `IMPORT` and `MODULE_LOCAL`; anything not covered by
these is `UNRESOLVED` and stays an observation forever, never promoted.

**A known, deliberate limitation**, found by the adversarial identity suite
(`tests/integration/test_python_indexing.py`) rather than assumed up front:
without a Git adapter, `QUALIFIED_NAME` and `DECLARATION_ANCHOR` (file +
qualified name + kind) are the only identity signals this adapter can
honestly offer. `DECLARATION_ANCHOR` is strong enough to keep an unchanged
symbol's identity stable across re-indexes (IR 0.1.2 — see docs/IR.md's
changelog), which closed the worst of what the suite first found: an
ordinary re-index no longer manufactures a new entity generation every run.
What it still cannot do, honestly: **a rename**. The moment the file or the
qualified name changes, so does the anchor — there is nothing left to match
the old entity on, so the renamed symbol resolves as plain `NEW` with no
lineage, and the old entity is left orphaned (documented in
`adapters/python/normalizer.py` and `application/indexing.py`). It is safe
(nothing silently merges or vanishes) but is exactly why the Git/History
adapter is next, not a nice-to-have.

## Second adapter target (per the MVP scope decision in ARCHITECTURE.md)

Git adapter (rename evidence, closing the gap above) + Django/Celery
framework enrichers + a migrations-or-schema-based Postgres data adapter.
This is the set needed to produce the §40 example end to end in one stack —
the Python adapter alone already produces most of it (see the worked example
in `tests/integration/test_python_indexing.py`); Django/Celery/Postgres are
what turn `INTERFACE`/`DATA_ENTITY`/`MESSAGE_CHANNEL` from "the Python
adapter's plain SYMBOL/MODULE types" into the framework-aware semantics §18
describes — before a second *language* adapter is added to prove IR
portability (§42's "at least two language ecosystems map into the same
semantic model").
