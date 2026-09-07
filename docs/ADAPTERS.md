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
(`resolve.py`): `SELF` (`self.foo`, one attribute level only),
`SELF_ATTRIBUTE` (`self.foo.bar()` where `self.foo` was assigned a known
type once in `__init__` — a real-repository pilot finding, see below), and
`LOCAL_INSTANCE` (`x = Cls(); x.method()`, tracked per-function) are the
strongest, followed by `IMPORT` and `MODULE_LOCAL`; anything not covered by
these is `UNRESOLVED` and stays an observation forever, never promoted.

**Fixed during Real Repository Pilot v0.1, Phase 2 — `self.<attr>.<method>()`
calls through a constructor-composed dependency now resolve.** Before
`SELF_ATTRIBUTE` existed, `self._notification_service = NotificationService
(...)` in `__init__`, called as `self._notification_service.send_
notifications(...)` from a different method, was silently `UNRESOLVED` —
`SELF` only ever covered a single attribute hop, and `LOCAL_INSTANCE` only
ever tracked a name assigned within the *same* function body. This is the
single most common way Python composes a class's own dependencies, and a
real agent task against a real, previously-unseen repository caught it: the
agent's own `reverse_impact` on `send_notifications` missed the actual
production caller entirely, and the agent only found it via plain grep,
then had to independently diagnose the miss as a genuine Hashira gap rather
than a real absence — see `docs/ROADMAP.md`'s Phase 2 entry for the full
account, including why this crossed the bar for an immediate fix while
Impact Presentation v0.1's `display_name` question and the analysis-root
gap above did not (bounded, mechanical, and backed by a real task where it
mattered, rather than a synthetic fixture or an ergonomic nice-to-have).
`extractor.py`'s `_self_attribute_types` populates
`ResolutionContext.self_attribute_types` once per class, from `__init__`
alone, via the same restricted mechanism `LOCAL_INSTANCE` already uses —
never a guess layered on a guess, and deliberately not generalized to
attribute assignments outside `__init__` or right-hand sides other than a
bare `Call` (e.g. `self._mcube = mcube_client or MCubeClient()`'s
fallback-default idiom still resolves to nothing, same as an unrelated
`self.foo.bar()` always has).

**Fixed one round later — the fallback-default idiom, and typed
parameters calling a method on themselves.** A follow-up real-production
benchmark (`docs/ROADMAP.md`'s continuation of the same entry) ran
`reverse_impact` on a live, previously-buggy config value and got back
"no production callers, only tests" — false, and for two distinct,
verified reasons. First, the exact fallback-default idiom the paragraph
above had just named as deliberately unresolved (`self._settings =
settings or get_settings()`) turned out to be this codebase's dominant
composition style, not a rare shape. Second, and unrelated to the first:
an ordinary typed function parameter calling a method on itself
(`def receive(inbound: IvrWebhookInbound, service: Annotated[IvrService,
Depends(...)]): inbound.resolve(...)`) — arguably the single most common
shape in any framework's request-handling code — was never resolved at
all, by anything; no existing mechanism tracked a parameter's own
annotation. Both closed, each as narrowly as `SELF_ATTRIBUTE` itself:
`extractor.py`'s `_constructor_call` now also accepts `provided or
KnownCallable(...)` (only when the last `or` operand is a literal call)
for both `_local_instance_types` and `_self_attribute_types`; a new
`_parameter_instance_types` resolves a parameter's own annotation
(`Annotated[T, ...]` reduced to `T` first) through the same
IMPORT/MODULE_LOCAL-only restriction, merged into
`ResolutionContext.local_instance_types` — deliberately separate from
`adapters/sqlalchemy`'s own `_typed_parameter_instances`, which exists
only for ORM field reads/writes on known model classes, not general
`CALLS` resolution.

**A third, more fundamental boundary surfaced while verifying the fix,
and deliberately left alone.** `self._mcube = mcube_client or
MCubeClient()` now resolves correctly (`MCubeClient` is a class, so its
own qualified name genuinely is `_mcube`'s type) — verified against the
real repository: `NotificationService`/`SmsService` now show real `CALLS`
edges into `MCubeClient.send_sms`. But the literal example that motivated
the fix, `self._settings = settings or get_settings()`, does *not*
resolve correctly even after this fix, because `get_settings` is a plain
factory *function* returning a `Settings` instance, not a class named
`get_settings` — Stage 1 has no way to tell "imports a class" from
"imports a function that returns one" from an import statement alone, and
resolving a function's own return-type annotation is a cross-file
question this stage's "never touches another file" contract rules out.
This is not a regression: a bare, non-fallback `self._settings =
get_settings()` had exactly the same limitation before either fix
existed, for the same reason `RETURN_VALUE_PROVENANCE` (the SQLAlchemy
adapter's own, narrower, field-access-only version of this same class/
function ambiguity) was disclosed rather than guessed at when this
project last faced this choice. `normalizer.py`'s Stage 2 still safely
declines to promote such a call into a relationship rather than fabricate
one — a miss, not a wrong answer. **Not fixed, and not asked for**: no
`LimitationKind` currently discloses this specific ambiguity for the
general Python `CALLS` resolver (`RETURN_VALUE_PROVENANCE` as it exists
today is scoped only to the SQLAlchemy adapter's field-access mechanism);
whether it deserves one is a real, open question for a future round, not
decided here.

**Fixed in a later round — function-local imports.** A live-agent run
against a real, previously-unseen repository (a Django/Kafka backend) found
`reverse_impact`/`get_relationships` returning `DEFINES` edges only — zero
`CALLS` edges anywhere in the queried neighborhood, `coverage: PARTIAL` with
no limitation naming why. Root cause: `from common.kafka import
kafka_publisher` written *inside* a function body (`def
enqueue_push_notification(...): from common.kafka import kafka_publisher;
kafka_publisher.push_notification(...)`) rather than at module level —
lazy/deferred imports and the standard way to break a circular import in a
large codebase, and, per that investigation, the *dominant* import style at
every call site that mattered. `_module_level_imports` (`extractor.py`)
never descended into a function body at all, so a function-local import
never entered `ResolutionContext.imports`, for that function or any other —
the call fell straight to `UNRESOLVED`, silently, exactly the same failure
shape as the `SELF_ATTRIBUTE`/fallback-default/typed-parameter gaps above,
just one syntactic form earlier: the collaborator's *type* was never the
problem here, its *name* was never bound in the first place.

Closed the same way every gap above was closed — narrowly, syntactically,
never a guess layered on a guess. `extractor.py`'s new
`_function_local_imports` scans a function's own body (not descending into a
*nested* def/class, exactly like `_local_instance_types`' own scoping) for
`import`/`from ... import ...` statements and merges their bindings into
that function's `ResolutionContext.imports` alone — shadowing a same-named
module-level import within that function, matching real Python scoping, and
never leaking to a sibling function that imported nothing itself. Because an
import statement is syntactic ground truth (not a guess about what a name's
type might be), the fix is complete for this form, not a partial widening —
`kafka_publisher.push_notification(...)` now resolves at `IMPORT` tier, the
same confidence a module-level import already gets. A companion fix widened
`_all_imports` (renamed from `_top_level_imports`) to walk the *entire*
file, not just statements outside a function/class body, so a
function-local import also produces its own `python.import` Observation and
a real `IMPORTS` relationship — previously invisible to that edge type too,
independently of the `CALLS`-resolution gap. `tests/unit/
test_python_extractor.py`'s function-local-imports section holds five
regression cases: direct resolution, the `IMPORTS` observation, no leakage
to a sibling function, shadowing a module-level import of the same name, and
feeding a function-local import into `_local_instance_types` so a
subsequently-constructed instance's methods resolve too.

**What this fix does not attempt, deliberately, same discipline as
everywhere else in this module**: a class-body-level import (as opposed to a
function/method body) is not bound for resolution — only its `IMPORTS`
observation is captured via `_all_imports`, since class-body-scoped imports
are a materially rarer idiom than function-local ones and nothing forced
this question yet; multi-level nested functions correctly inherit an outer
function's local imports through the same `ctx` threading a closure would
use, which is real Python behavior, not an extra mechanism built for it.

**Acceptance-test verification against the real repository surfaced three
more real issues before the function-local-import fix above could even be
confirmed — full account in `docs/ROADMAP.md`'s "Real Repository Pilot v0.2"
entry, summarized here:**

1. A full indexing *crash* (not a partial failure) when the import root
   itself is a Python package: `<root>/__init__.py`'s dotted name is empty
   relative to itself, and `Entity(name="")` failed validation instead of
   being skipped. Fixed in `module_qualified_name` — falls back to the
   import root's own directory name, a real, non-fabricated identifier.
2. **Module-level singleton instances** — `kafka_publisher =
   KafkaEventPublisher()` at module scope, imported and called from another
   file — are the module-scoped twin of the `self._settings = settings or
   get_settings()` class/function ambiguity above: Stage 1 cannot know an
   imported name is an *instance*, not a class/function/module, without
   reading its defining file. Closed at Stage 2 instead (which has
   whole-run visibility Stage 1 deliberately never has): a new
   `python.module_instance` observation records `name = ClassName(...)`
   assignments at module scope, and `normalizer.py`'s
   `_retarget_through_module_instance` re-targets a call that failed its
   direct qualified-name lookup through the longest matching known instance
   prefix, never fabricating a match to something that isn't really there.
3. A pre-existing, unrelated bug in `resolve.py`'s `_package_of`, found
   incidentally while writing a test fixture for (2): a relative import
   written inside a package's own `__init__.py` (whose qualified name
   already *is* the package, not a leaf module) resolved one package too
   far up. Fixed by threading a new `is_package_init` flag into
   `bindings_for_import_from`/`_package_of`, opting out of the one
   truncation that assumes the importing file is an ordinary module.

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

**Known limitation — analysis root vs. repository root.** `discovery.py`
computes each file's dotted import name relative to the root it is given
(`import_root_for`, "src/ layout aware") — but that root is always whatever
path the caller of `IndexingService.index()` passed in, which the whole
pipeline implicitly assumes is *both* the Git repository root (what
`GitAdapter` walks) *and* the language's own import root. The first
real-repository pilot (`docs/ROADMAP.md`'s "Real Repository Pilot v0.1"
entry) found a real, unfixtured case where those two roots diverge: a
monorepo with the Git root one level above a `backend/` directory holding
the actual Python application (`frontend/` alongside it). Indexed at the
Git root, every file still gets scanned and zero errors are reported — but
`import_root_for` computes each module's dotted name with a spurious
`backend.` prefix, so every cross-file reference (`from app.foo import
Bar`) fails to resolve against it. The result: `WRITES`, `READS`,
`CONTAINS`, `IMPORTS`, and most `CALLS` edges go silently missing, with
nothing in `IndexingResult.errors` to say so. Indexed at the actual Python
root (`backend/`) instead, the same repository resolves fully.

This is a materially worse failure mode than a crash: `IndexingResult`
reports success, entity/relationship *counts* look plausible, and nothing
distinguishes "this system genuinely has few cross-file relationships"
from "the configured root broke resolution." That is exactly the
distinction `LimitationKind`/`CoverageStatus` (`application/impact.py`)
exist to make explicit elsewhere in this project — this gap is the same
shape, one layer earlier, at indexing time rather than query time.

**Current workaround**: pass the language/application root directly to
`IndexingService.index()`, not the Git repository root, whenever they
differ. **Deliberately not fixed yet**: an "auto-detect the import root"
heuristic was considered and rejected for this pass — real repositories
diverge in too many shapes (`backend/src/myapp/`, multiple sibling
service directories each with their own root, `apps/{api,worker,admin}/`,
namespace packages) for a heuristic chosen from one pilot repository to
generalize, which is exactly the mistake `Impact Presentation v0.1`
(`docs/IR.md`) already refused to make for grouping. The right fix is to
model analysis roots/import roots as an explicit concept the caller
configures, and to have indexing itself surface a coverage-style warning
when a nonzero file set resolves to zero cross-file relationships — future
work, not attempted here.

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

## FastAPI adapter

`src/hashira/adapters/fastapi/` — a second `FrameworkAdapter`, built to
answer an architectural question rather than to add feature count: can two
materially different frameworks produce equivalent concepts in the same
System IR without contaminating core or the Python adapter? See
`adapter.py`'s module docstring for the exact reuse boundary; summarized
here.

**Same reuse discipline as Django, applied to a materially different
idiom.** Django detects models/views via *resolved inheritance*; FastAPI has
no base-class vocabulary to key off — its framework surface is
*decorators and call expressions* (`@app.get(...)`, `Depends(...)`,
`app.include_router(...)`). Each is resolved the same way Django resolves a
base class: through `adapters.python.resolve.resolve_expr`, never a
name-suffix guess (`known_symbols.py` is this adapter's `known_bases.py` —
a curated allowlist of fully-qualified names: `fastapi.FastAPI`,
`fastapi.APIRouter`, `fastapi.Depends`, `pydantic.BaseModel`). A variable
happening to be named `app` that is not actually a `FastAPI()` instance is
not detected, precisely mirroring Django's "a class named `PaymentModel`
that extends nothing Django-related is not a model."

**A shared module came out of proving this, not a framework concept.**
`adapters/_python_index.py` (`PythonIndex`, `PythonTreeCache`) is Django's
own `_Index`/`_TreeCache`, extracted once a second adapter needed exactly
the same plumbing over Python's observations — same category as
`_dedup.py`/`_identity_claims.py`, and extracted for the same reason (two
independent implementations proved it was genuinely shared, not assumed to
be). Route/handler/dependency semantics stayed entirely separate per
adapter, on purpose — see "Adapters discover abstractions; core should not
predict them" below.

**Genuinely new parsing, done by this adapter and nowhere else**: app/router
recognition (`app = FastAPI()`), route registration
(`@app.get("/checkout/")` — Python's own extractor already records a
decorator's *unparsed source text* on `python.symbol` observations, but not
what it *means*), router wiring (`include_router(..., prefix=...)`, needed
to compose a route's full path across files, one hop deep — see
`_composed_prefix`'s docstring for the documented nesting limit), dependency
injection (`Depends(...)` as a parameter default), and basic request/
response model association (a parameter or return annotation resolving to a
class whose `python.inheritance` observations confirm `pydantic.BaseModel`).

**`DEPENDS_ON` is not `CALLS`, deliberately.** `Depends(get_payment_service)`
is dependency-injection semantics, not "this syntax happens to contain a
call expression" — kept as its own relationship type, separate from the
`CALLS` edges Python's own normalizer already produces for a handler's
actual function body. The design discussion this was built from was explicit
that FastAPI's framework semantics are stronger than the syntax here.

**A route handler is tagged, not duplicated** (`normalizer.py`, same rule as
Django's model/view tagging): it stays `EntityType.SYMBOL` with
`metadata.framework`/`fastapi_kind` set. A route itself gets a freshly
minted `INTERFACE` entity, exactly like Django's `django.url_route` —
`/checkout/ [POST]` has independent system meaning and no Python-symbol
identity of its own.

**Request/response model association stays out of the graph as edges, on
purpose.** Neither `CONSUMES`/`PRODUCES` (already reserved for message-queue
direction elsewhere in this codebase — see `docs/ARCHITECTURE.md`'s Celery
example) nor `DEPENDS_ON` (reserved for `Depends()`) was the right fit, and
inventing a new relationship type for two adapters' first pass at this is
exactly the premature taxonomy the next paragraph warns against. The association is
recorded as metadata on the handler entity instead
(`request_model_qualified_name`/`response_model_qualified_name`), and the
model class itself is tagged `fastapi_kind="schema"`.

**Adapters discover abstractions; core should not predict them.** Django and
FastAPI now independently need the same shape of concept — HTTP method,
route, handler — and both independently reached for `EntityType.INTERFACE`
+ `EXPOSES` to represent it, without either adapter importing from the
other. That is evidence a framework-neutral "route" concept might eventually
deserve a typed representation in core. It is deliberately *not promoted*
yet — two adapters converging once is a signal worth watching, not proof;
see `tests/integration/test_cross_framework_equivalence.py` for where this
convergence is actually checked, and ROADMAP.md's entry on this milestone
for the fuller version of this argument.

**Verified end to end**, not just unit-tested in isolation
(`tests/integration/test_fastapi_identity.py`, against
`tests/fixtures/fastapi_checkout/`): the same boring-reindex and
cross-backend (SQLite/memory) checks every other adapter here is held to.
**And verified *across* frameworks**
(`tests/integration/test_cross_framework_equivalence.py`): the same
framework-agnostic reverse-impact query, run against `django_basic` and
`fastapi_checkout` independently, reaches the equivalent architectural role
in both graphs — the query function itself imports nothing from either
adapter.

**Not attempted in this pass**, documented rather than silently missing:
SQLAlchemy or any persistence layer (a `DataAdapter`'s job, not a framework
enricher's — see below), field-level access on Pydantic models (there is no
FastAPI/Pydantic equivalent of Django's `_extract_field_accesses` — the
association is handler-to-model, not model-field-to-handler), class-based
endpoints (function-based handlers only, matching the milestone's explicit
scope), and multi-hop router nesting (a router included into another router
that is itself included into an app composes only one level of prefix).

## SQLAlchemy adapter

`src/hashira/adapters/sqlalchemy/` — the first `DataAdapter` (`ports/adapters.py`),
a kind distinct from `FrameworkAdapter` on purpose: it must never care
whether the code it enriches belongs to FastAPI, Django, a CLI, or nothing
at all. See `adapter.py`'s module docstring for the exact reuse boundary;
summarized here.

**Detection has to work through two real-world declarative styles**, unlike
Django's single fixed base class. SQLAlchemy 2.0's class-based root
(`class Base(DeclarativeBase): pass`) resolves through Python's own
`python.inheritance` observations, since `DeclarativeBase` is always reached
by import. The pre-2.0 factory style, still extremely common
(`Base = declarative_base()`), assigns a plain module-level variable Python's
extractor never tracks — this adapter finds it itself, the same way
`adapters.fastapi.adapter` finds `app = FastAPI()`. Both styles feed one
`known_bases` set, expanded to a fixpoint so a multi-level hierarchy (a
project's own `TimestampedBase(Base)` mixin) resolves regardless of which
style introduced its root — the fixpoint has to re-resolve every class's own
bases itself rather than trusting `python.inheritance` alone, since that data
cannot see past a locally-assigned `Base` variable at any depth. A class
extending a known base is only a genuine *model* if its own body declares
`__tablename__`; `Base` and any mixin without one are correctly never
classified as tables.

**The one place a framework enricher mints two linked entities instead of
tagging one.** Every other enricher here tags an existing Python entity in
place, because the framework construct *is* that Python construct. An ORM
class and the table it persists to are not: one is source structure, the
other a runtime/data structure with independent identity. So the model class
is still tagged (`metadata.framework`/`sqlalchemy_kind`, the same rule as
everywhere else), *and* a fresh `EntityType.DATA_ENTITY` is minted for the
table, connected by `RelationshipType.MAPS_TO` — checked against the
existing relationship vocabulary first and added only once nothing honestly
fit (`docs/IR.md`'s entry on this milestone has the full reasoning). Columns
are `EntityType.SYMBOL` entities `CONTAINS`-related to their table
(mirroring Django's model-field pattern exactly); a `ForeignKey("table.column")`
argument becomes `RelationshipType.REFERENCES` between two column entities,
resolved by a direct qualified-name lookup (a column's own qualified name and
a `ForeignKey` string argument share the same `"table.column"` format by
construction — no extra resolution step needed).

**Basic read/write evidence, kept as an independent copy of Django's
pattern, not shared.** `_extract_field_accesses` mirrors Django's
`LOCAL_INSTANCE`-scoped detection (`payment = Payment()`, then
`payment.status = ...` / `... payment.status`) line for line — and stays
that way deliberately. The design discussion this was built from was
explicit: do not refactor Django's model handling into shared plumbing until
a second adapter's *independent* needs prove what is actually common. What
*did* prove common and got extracted (`adapters/_python_index.py`: the
`PythonIndex`/`PythonTreeCache`/`find_class_node` trio) is purely
plumbing over Python's own observations — never framework- or data-semantic
logic. Field-access detection stayed unshared because nothing yet forced the
question; see "watching for real convergence" below.

**Composing with a `FrameworkAdapter` needed one genuinely new, generic
piece.** `IndexingService` takes exactly one `Normalizer`, but two enrichers
(FastAPI + SQLAlchemy) both need to turn their `*.` observations into
entities on top of Python's. Naively running each enricher's own
`normalize()` independently and merging the results does not work: every
`normalize()` calls `adapters.python.normalizer.normalize` itself, and
`Entity.id` is a fresh ULID every time an `Entity` is constructed — so two
enrichers would mint *different* ids for the same underlying Python class,
and downstream relationship reconciliation (which only dedupes against
storage, not within one run's own candidates) would silently double every
plain `CALLS`/`IMPORTS`/`DEFINES` edge Python's own normalizer produces.
`adapters/_compose.py::compose_normalizers` fixes this the same way
`IndexingService` avoids re-deriving language-level structure per framework
adapter: run Python's normalizer exactly once, then chain each enricher's
new `enrich_normalized_run` (added alongside each existing `normalize`,
which is now a one-line wrapper around it) onto the *same* entity pool.
Deliberately generic — nothing in `_compose.py` names FastAPI or SQLAlchemy;
`tests/integration/test_fastapi_sqlalchemy_together.py` is what exercises it
today, and its own `test_no_duplicate_relationship_rows_from_composing_two_enrichers`
proves the specific bug this closes.

**Verified end to end, twice.** First against a plain, framework-free
fixture (`tests/fixtures/sqlalchemy_basic/`,
`tests/integration/test_sqlalchemy_identity.py`) — proving the adapter means
the same thing with *no* `FrameworkAdapter` configured at all — answering
this milestone's own worked example, *"what is affected if `payments.status`
changes?"*, entirely from the graph. Then the exact same, completely
unmodified `SQLAlchemyAdapter` is plugged into the FastAPI fixture
(`tests/fixtures/fastapi_checkout/payments/db_models.py`, added for this
pass; `payments/services.py` now genuinely reads/writes a SQLAlchemy
`Payment.status` instead of a bare local variable) alongside `FastAPIAdapter`
in one `IndexingService` run
(`tests/integration/test_fastapi_sqlalchemy_together.py`), producing one
fully-connected graph from the HTTP route down to the database column with
no FastAPI-SQLAlchemy-specific code anywhere.

**A column renamed within an unchanged file, closed in a later pass, with
its own mechanism.** Git's rename detection is file-level; it has nothing
to detect when a declaration changes but the file never moves.
`_detect_declaration_renames` re-parses a `MODIFIED` file's content *before*
the current revision (`GitAdapter` attaches it to `git.file_change` when
available) with this adapter's own column extraction, and compares old
columns to new. Only an unambiguous 1:1 disappearance/appearance in the
same table, with the same type family, is reported as a
`sqlalchemy.declaration_rename` fact —
`identity/declaration_evidence.py::attach_declaration_lineage_evidence` is
what turns that into a `DECLARATION_LINEAGE` claim (`docs/IR.md`'s entry on
that milestone has the full story, including a real, pre-existing bug in
relationship reconciliation this surfaced). A name change alongside a
type-family change is treated as insufficient evidence, not weaker
evidence — this adapter never guesses from a name alone.

**Watching for real convergence, not refactoring speculatively.** Django's
model/field handling and SQLAlchemy's model/column handling now both exist,
independently, and both reach for the same shape of concept — a data
entity, contained fields, read/write evidence. That is exactly the kind of
signal worth watching (`docs/ROADMAP.md`'s entry on this milestone says
more), but it is *not* acted on here: Django's adapter was not touched, and
`_extract_field_accesses` stays duplicated rather than shared. *Adapters
discover abstractions; core should not predict them* — the same principle
the FastAPI milestone established for `EntityType.INTERFACE`/`EXPOSES`
applies here to data semantics too.

**Not attempted in this pass**, documented rather than silently missing:
query-shape analysis, sessions/transactions, async SQLAlchemy, Alembic
migrations, raw SQL, hybrid properties, `relationship(...)` construct
parsing (SQLAlchemy's own ORM-level association helper — "deep relationship
inference" was explicitly out of scope; only a column's direct
`ForeignKey(...)` argument is read), and multi-hop `ForeignKey` chains
beyond a direct string reference.

## Next adapter target (per the MVP scope decision in ARCHITECTURE.md)

Having proven cross-framework equivalence with a second `FrameworkAdapter`
and layered persistence semantics underneath both with a `DataAdapter`, a
Celery async enricher (`PROCESS`/`MESSAGE_CHANNEL` for async workflows and
queues) is the next natural candidate — together with SQLAlchemy these are
the remaining pieces needed to produce the §40 example end to end in one
stack; Python + Django/FastAPI + SQLAlchemy already produce most of it (see
the worked examples above). A second *language* adapter, to prove IR
portability (§42's "at least two language ecosystems map into the same
semantic model"), stays a later milestone — proving cross-framework and
cross-layer equivalence within one language came first, deliberately.
