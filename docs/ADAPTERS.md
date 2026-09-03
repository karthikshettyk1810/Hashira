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

**What this adapter can offer on its own, and where Git now picks up the
rest** — found by the adversarial identity suite rather than assumed up
front: on its own, `QUALIFIED_NAME` and `DECLARATION_ANCHOR` (file +
qualified name + kind) are the only identity signals this adapter can
honestly produce. `DECLARATION_ANCHOR` is strong enough to keep an unchanged
symbol's identity stable across re-indexes (IR 0.1.2), so an ordinary
re-index no longer manufactures a new entity generation every run. A
**rename**, though, changes the file and/or the qualified name — the anchor
changes right along with it, so there is nothing left in this adapter's own
output to match the old entity on. That gap is now closed, but not by this
adapter: `hashira.adapters.git.GitAdapter` supplies `GIT_RENAME` evidence
(see the "Git adapter" section below), and `identity/git_evidence.py`
attaches it before resolution runs, *when a `HistoryAdapter` is configured*.
Without one, the old behavior stands exactly as before: `NEW` with no
lineage, the old entity orphaned. Safe either way — nothing silently merges
or vanishes.

## Git adapter

`src/hashira/adapters/git/` — the `HistoryAdapter` port, satisfied. Reports
commit history and file-change/rename evidence; never decides what a
detected rename *means* for identity (that split — "Git provides evidence,
the resolver decides" — is the adapter's entire design point, see
`adapter.py`'s module docstring).

**Implementation choice: shell out to the real `git` binary**, through one
controlled boundary (`runner.py`), rather than a Python Git library. Git is
the canonical implementation of the thing being interrogated — its rename
heuristic, its DAG, its ancestry — and reimplementing any of that would just
be a second, worse copy of logic `git` already gets right. Every other file
in this package (`repository.py`, `adapter.py`) talks to `runner.run_git`,
never to `subprocess` directly.

**Revision is a DAG, not a timeline.** `GitRepository.commits()` orders by
topology (`git log --topo-order`), not by commit timestamp — a timestamp can
be wrong or out of order; a commit's position in the DAG cannot. Merge
commits carry every parent (`CommitInfo.parent_shas`, first parent first);
v0.1 deliberately does not attempt merge-aware indexing semantics beyond
that — `changed_paths_in_commit` reports a merge commit's changes relative
to its first parent only, matching how `git log`'s default view already
simplifies merges for humans.

**Rename detection is Git's own heuristic, kept as a score, not a boolean.**
`FileChange.similarity` (0.0–1.0) travels all the way from `git diff -M`'s
output to the `git.file_change` Observation's payload. Whether a given score
is trustworthy enough to influence identity is `identity/git_evidence.py`'s
call (a stricter 90% bar than Git's own 50% default), not this adapter's —
see that module and the "Python adapter" section above for the full chain.

**Tested against real temporary repositories**, not mocked Git output
(`tests/unit/test_git_repository.py`, `tests/unit/test_git_adapter.py`) —
root commits, merge commits, exact renames, partial-similarity renames, and
the adversarial case (a `git mv` plus a total content rewrite, which Git's
own default threshold already refuses to call a rename).

## Django adapter

`src/hashira/adapters/django/` — the `FrameworkAdapter` port, satisfied, and
the first proof that the graph is genuinely cross-domain rather than a code
graph with extra labels. See `adapter.py`'s module docstring for the exact
reuse boundary; summarized here:

**Reused from Python, never re-derived**: classes, functions, methods,
imports, and — critically — *resolved inheritance*. A class is a Django
model only because a `python.inheritance` observation the Python adapter
already produced resolves its base to `django.db.models.Model`; this adapter
never re-parses a class definition to figure that out itself.

**Genuinely new parsing, done by this adapter and nowhere else**: model
fields (`status = models.CharField(...)`, a class-body attribute assignment
Python's extractor has no reason to track generally), URL patterns
(`urlpatterns = [path(...), ...]`, a plain list literal with no
Python-symbol shape at all), and field access (`payment.status = ...`,
attribute reads/writes — Python's extractor only tracks calls). All three
reuse `adapters.python.resolve.resolve_expr` for name resolution and seed
their resolution context from Python's own `python.import` observations —
the *mechanism* is shared, only the *target pattern* (field declarations,
URL literals, attribute access) is Django-specific.

**Detection is evidence-based, never name-based** (`known_bases.py`): a
curated, closed allowlist of fully-qualified Django/DRF base class names,
matched against *resolved* inheritance — the same discipline the identity
ladder applies to entity identity applies here to framework detection. A
class named `PaymentModel` that extends nothing Django-related is not a
model; a class extending some unrelated `Model` imported from elsewhere is
not a model either (both are tested explicitly).

**A model/view class is tagged, not duplicated or reclassified**
(`normalizer.py`): it stays `EntityType.SYMBOL`, with
`metadata.framework`/`django_kind` set — the class is still fundamentally
"a Python class" (core/base.py's own rule: technology detail belongs in
metadata, never a reshaped core envelope). Model fields and URL routes,
which have no Python-symbol counterpart at all, get freshly minted
`SYMBOL`/`INTERFACE` entities carrying the exact same `QUALIFIED_NAME` +
`DECLARATION_ANCHOR` identity-claim shape Python's own entities carry, via
two small utilities pulled out for both normalizers to share:
`adapters/_dedup.py` and `adapters/_identity_claims.py`.

**Two-stage, same as Python** (`adapters/python/normalizer.py`'s pattern):
`adapter.py` (Stage 1) only ever produces `Observation`s — never
`Entity`/`Relationship` objects directly. `normalizer.py` (Stage 2) is what
turns `django.model_field` into a `SYMBOL` entity `CONTAINS`-related to its
model, and `django.url_route` into an `INTERFACE` that `EXPOSES` the view
Python already resolved — and it runs *after* Python's own normalizer,
deliberately, since linking a route to a view requires that view to already
exist as a candidate with a real qualified name to look up.

**Verified end to end**, not just unit-tested in isolation
(`tests/integration/test_django_identity.py`, against
`tests/fixtures/django_basic/`): the exact worked example from the design
discussion — *"what is affected if `Payment.status` changes?"*, answered by
walking the graph backward through non-structural edges — plus the same
boring-reindex and cross-backend (SQLite/memory) checks every other adapter
in this repository is held to.

**Not attempted in this pass**, documented rather than silently missing:
function-based views, abstract model inheritance chains, `self.attr` field
access (only local-variable instances are tracked, matching the Python
resolver's own scope limit), and resolving `include()`'d URL confs across
files.

## Second adapter target (per the MVP scope decision in ARCHITECTURE.md)

A Celery async enricher + a migrations-or-schema-based Postgres data
adapter. This is the remaining piece needed to produce the §40 example end
to end in one stack — Python + Django already produce most of it (see the
worked example above); Celery/Postgres are what turn `PROCESS`/
`MESSAGE_CHANNEL`/`DATA_ENTITY` (async workflows, queues, the data layer
below the ORM) from unmodeled into framework-aware semantics §18
describes — before a second *language* adapter is added to prove IR
portability (§42's "at least two language ecosystems map into the same
semantic model").
