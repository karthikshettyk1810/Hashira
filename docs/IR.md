# System IR

The canonical intermediate representation of a software system (spec §7).
Current version: `0.1.2` (`hashira.core.IR_VERSION`).

System IR is not an AST. It describes structural reality (what exists),
behavioral reality (what happens), and historical reality (what changed and
what was learned) — §7.

## Contract, not implementation detail

Every model listed in `hashira.core.IR_MODELS` is public. Its JSON Schema is
exported to `docs/schema/ir-<version>.json` by `scripts/export_schema.py`, and
`tests/contract/test_ir_schema.py` fails the build if the live schema drifts
from that committed file without a deliberate regeneration. That file is the
review surface for contract changes — if a pull request touches it, the diff
should be read as "this is a change to a versioned public contract," not as
generated noise.

## Versioning rules (§31)

- Additive changes are preferred and bump the patch version.
- Breaking changes bump the minor version while still `0.x`.
- Adapters declare the IR version range they support
  (`AdapterCapabilities.ir_versions`).
- Old snapshots remain readable: `hashira.core.supports_ir_version()` checks
  compatibility by `(major, minor)`.
- Unknown fields are preserved, not dropped. Every IR model uses
  `extra="allow"` (`core/base.py::IRModel`) and round-trips fields it does not
  recognize — see `test_unknown_fields_survive_a_round_trip`. A 0.1 reader must
  not destroy data a 0.2 producer wrote.
- Do not use a model/provider version as a substitute for an IR version.

Three separate version numbers are tracked deliberately (`core/schema.py`):
`IR_VERSION` (the record shapes), `ADAPTER_CONTRACT_VERSION` (what an adapter
promises to implement), and `EVENT_SCHEMA_VERSION` (the event envelope). They
move independently — an adapter can gain a capability without the event
envelope changing, and vice versa.

## Changelog

- **Adapter contract 0.1.1** (IR itself unchanged at 0.1.2) — added
  `ExtractionResult.events` (additive, defaults to empty — an adapter written
  against 0.1.0 still satisfies 0.1.1) and the `HistoryAdapter` protocol
  (`ports/adapters.py`), for `hashira.adapters.git.GitAdapter`. `GIT_RENAME`
  identity claims (already part of the vocabulary since 0.1.0) are now
  actually produced, by `identity/git_evidence.py`, closing the gap the
  0.1.2 entry below left open for renames specifically.
- **0.1.2** — Added `IdentityClaimKind.DECLARATION_ANCHOR` (file path +
  qualified name + kind, all at once), placed at strong tier in the identity
  ladder. Additive — an existing reader ignores a claim kind it doesn't
  recognize; nothing existing changes shape. Fixes a real bug the Python
  adapter's adversarial test suite caught: without it, re-indexing an
  *unchanged* file resolved as `SUPERSEDES` rather than `MATCHED`, since
  `QUALIFIED_NAME` alone is deliberately corroborating-tier — an ordinary
  `hashira index` would have minted a new entity generation on every run. See
  `identity/resolver.py`'s and `adapters/python/normalizer.py`'s docstrings
  for why this doesn't reopen the "qualified name alone must not merge"
  guard it sits next to.
- **0.1.1** — `IdentityClaim.kind` narrowed from a free string to the closed
  `IdentityClaimKind` enum, so the identity resolution ladder
  (`hashira.identity.resolver`) has a fixed vocabulary to rank rather than
  whatever string an adapter happened to write. Bumped rather than folded
  silently into 0.1.0 because narrowing a field's type is a producer-facing
  change, even pre-release — exercising the version-bump path for real, on
  the first contract change, is the point of having it.
- **0.1.0** — initial frozen contract (§44).

## Deviations from the literal spec text

These are implementation decisions made while turning prose into a schema.
Each one is a judgment call, not an oversight — flagged here so a future
change to the spec document can reconcile deliberately rather than by
surprise.

### Confidence is ordinal, not a float

§11's example relationship literally writes `"confidence": 1.0`, suggesting a
continuous `[0, 1]` float. `hashira.core.Confidence` is instead a three-value
ordinal: `CERTAIN`, `LIKELY`, `SPECULATIVE`.

Rationale: a `0.9` produced by a deterministic call-graph walker and a `0.9`
produced by a language model are not the same quantity. They cannot be
compared, averaged, or multiplied together in a downstream risk calculation
without smuggling in a false precision. A float schema invites exactly that
arithmetic. Three levels with defined meanings (`core/enums.py::Confidence`)
stay honest: `Confidence.rank` exists only for sorting and threshold checks,
never for arithmetic combination.

Consequence enforced in code: `Inference` with `origin=LLM` cannot carry
`confidence=CERTAIN` (`core/evidence.py::_apply_epistemic_rules`) — an LLM is
not an authority for system facts (§26), and the schema refuses to let one
claim to be.

### One temporal source of truth

§11 gives relationships `valid_from`/`valid_until`. §13 describes snapshots as
point-in-time views. §14 says "derived state may be rebuilt from events plus
source observations." Taken together, that is three mechanisms for answering
"what was true when," which will drift from one another the first time an
edge case hits only one of them.

This build makes **revision-keyed relationship validity the single source of
truth** (`Relationship.valid_from_revision` / `valid_until_revision` in
`core/relationships.py`). A `Snapshot` (`core/snapshots.py`) is a *named label
on a revision*, reproduced by re-deriving from source at that revision — not a
stored copy of the graph and not something replayed from the event log.
Wall-clock `valid_from`/`valid_until` are kept for time-range queries but are
derived from the revision fields, not an independent record.

`Event` (`core/events.py`) remains the authoritative *sequence* of what
happened — useful for "what happened before INC-182?" — but is explicitly not
a rebuild mechanism. Reconstructing graph state from an event log is an
expensive promise that rots quietly; reconstructing it from source at a known
revision is a promise this system can actually keep.

### `SUPERSEDES` added to the relationship vocabulary

§11 lists 25 relationship types. This build adds a 26th: `SUPERSEDES`.

Rationale: §10 requires that "identity changes must be represented as events
or lineage relationships rather than silently overwriting history." When
identity resolution cannot establish with sufficient confidence that a
renamed or relocated construct is the same entity, the correct move is to mint
a *new* entity and record the lineage — not to guess and merge. `SUPERSEDES`
is that lineage edge. `EntityStatus.SUPERSEDED` and
`EventType.ENTITY_IDENTITY_MERGED` exist for the same reason.

### `MAPS_TO` and `REFERENCES` added to the relationship vocabulary

The SQLAlchemy `DataAdapter` milestone (`adapters/sqlalchemy/`) surfaced two
more genuinely new relationships — checked against the existing 26 first,
per the design discussion this was built from ("don't add a new relationship
casually; first check whether an existing one honestly carries the
semantics"). Neither did:

* `MAPS_TO` — an ORM class's declarative binding to the table it persists
  to. `EXTENDS`/`IMPLEMENTS` are about code structure, not this;
  `RELATED_TO` exists precisely to be non-committal, which would discard
  the one thing an impact query actually needs ("what code paths touch the
  `payments` table" has to walk `MAPS_TO` specifically). Unlike every other
  framework construct this codebase enriches (a Django view, a FastAPI
  handler — always tagged onto the existing Python entity, never
  duplicated), an ORM class and its table are not the same conceptual
  thing: one is source structure, the other is a runtime/data structure
  with independent identity (a schema migration can add a column no Python
  attribute mirrors yet). That is why this is the one place in the codebase
  a framework enricher mints a *second*, linked entity instead of tagging
  the first.
* `REFERENCES` — a foreign-key relationship between two columns. A
  relational-database fact independent of any one adapter (SQLAlchemy,
  Django ORM, and a raw-SQL/migrations adapter would all want to report the
  same kind of edge), and distinct from `DEPENDS_ON`, which already spans
  build-time import dependencies and runtime dependency injection
  (`adapters/fastapi/`'s `Depends()` handling) — folding a third, very
  different kind of dependency into it would cost precision on every
  existing query that already walks it.

Both are in `IMPACT_EDGES` (`core/relationships.py`): a table's or column's
identity is exactly the kind of fact an impact query should be able to
reach through, not structural noise like `CONTAINS`/`DEFINES`.

### `KnowledgeClass` has four members, not three

§12's prose introduces "three primary knowledge classes" and then lists four
(OBSERVATION, DERIVATION, INFERENCE, HYPOTHESIS) — a documentation bug in the
source spec. `hashira.core.KnowledgeClass` implements all four, since the
fourth (HYPOTHESIS) is load-bearing throughout §17 (incidents) and §38 (false
causal conclusions).

## Identity resolution (§10) — status

The spec describes identity resolution as a list of signals to consider
(stable symbol ids, qualified names, Git rename history, migration lineage,
user declarations) rather than an algorithm. Each signal is represented as an
`IdentityClaim` (`core/entities.py`) with its own kind (`IdentityClaimKind`),
origin and confidence.

**The ladder is implemented** in `hashira.identity.resolver`
(`src/hashira/identity/`), as pure decision logic with no storage or adapter
dependency — see the module's docstring for the full policy. Summary:

| Signal tier | Kinds | Alone | Corroborated (2+ meaningful kinds) |
| --- | --- | --- | --- |
| Strong | `SYMBOL_ID`, `USER_DECLARED`, `DECLARATION_ANCHOR` | `MATCHED` (merge) | `MATCHED` |
| Corroborating | `GIT_RENAME`, `MIGRATION_LINEAGE`, `QUALIFIED_NAME` | `SUPERSEDES` (lineage) | `MATCHED` |
| Weak | `STRUCTURAL_SIMILARITY` | `NEW` (dropped) | never promotes anything |

`resolve()` returns one of four outcomes — `NEW`, `SUPERSEDES`, `MATCHED`,
`AMBIGUOUS` (two existing entities tie at the top tier; resolution refuses to
guess and `apply()` surfaces a `SPECULATIVE` `HYPOTHESIS`-class `Inference`
instead of picking one). `apply()` turns a decision into the records a caller
persists: a merged `Entity` for `MATCHED`, or a new `Entity` plus a
`SUPERSEDED`-status copy of the old one plus a `SUPERSEDES` `Relationship` for
`SUPERSEDES` — never a silent overwrite, satisfying the entity model's own
invariant that a `SUPERSEDED` entity must retain the claims that justified it.

Test coverage lives in `tests/unit/test_identity.py`: exact-match merges,
corroborated merges, single-signal lineage, weak-signal-alone rejection,
weak-signal-fails-to-promote, cross-type exclusion, ties/`AMBIGUOUS`, and the
`apply()` output shape for all four outcomes.

**Wired in and adversarially tested against a real fixture, not just unit
tests of the ladder in isolation.** `hashira.application.indexing`
(`src/hashira/application/`) runs the resolver for real, and
`tests/integration/test_python_indexing.py` mutates an actual fixture
repository (`tests/fixtures/python_basic/`) — renames, extracted methods,
copy/paste clones, duplicate qualified names — and checks the resulting
graph, not just the resolver's decision. That suite is what caught the gap
`DECLARATION_ANCHOR` (0.1.2, above) fixes: the ladder's tier thresholds
were correct in isolation and still produced a real bug (unconditional
`SUPERSEDES` on every re-index) once a real adapter fed it real candidates —
exactly the kind of thing a unit-test-only suite cannot catch on its own.

**The `GIT_RENAME` gap is now closed, conditionally.** With a
`HistoryAdapter` configured (`hashira.adapters.git.GitAdapter`), a rename
Git detects above a 90% similarity threshold resolves as
`SUPERSEDES`-with-lineage instead of an orphaned `NEW`
(`identity/git_evidence.py`, `tests/integration/test_git_identity.py`).
Without a configured history adapter, or when Git's own detection doesn't
surface a rename at all (or surfaces one below the threshold — the
adversarial "disguised replacement" case), the original behavior stands
unchanged: `NEW`, no lineage, old entity orphaned. Still open: the
`GIT_RENAME` claim only ever reaches corroborating tier, never strong — a
rename alone still cannot outright `MATCH`, deliberately, since a heuristic
similarity score is evidence to weigh, not a fact to trust blindly.

## Revision-scoped queries (§13) — status

["One temporal source of truth"](#one-temporal-source-of-truth) above already
settled the model: revision-keyed relationship validity is the truth, and a
snapshot is a named label on a revision, not a second store. This milestone
did not revise that — it built the two pieces §13's "was this true at
revision X" question actually needed on top of it, and confirmed the model
held up once something depended on it.

**Revision ancestry as a core concept, not a Git object.**
`core/revisions.py::Revision` records one point in history — `system_id`,
`sha`, `parent_shas`, `provider` — keyed by the same natural, provider-issued
sha string every other revision-keyed field in the IR already uses
(`Entity.first_seen_revision`, `Relationship.valid_from_revision`,
`Snapshot.revision`), not a second opaque id every caller would have to
translate through. `RevisionGraph` answers ancestry ("was `sha` reached by
the time we got to `of`?") by walking `parent_shas` — plain graph
reachability, not a `git log` call and not a timestamp comparison (a commit's
clock can be wrong; its DAG position cannot). The core still imports nothing
Git-specific; `hashira.adapters.git.GitAdapter` is simply the one adapter
that populates `Revision` records today, via
`application/indexing.py::_extract_revisions` turning `git.commit`
observations into them and persisting them through a new `RevisionStore`
port, held to the same shared conformance suite (`tests/contract/uow_conformance.py`)
as every other port.

**Historical query, kept deliberately boring.**
`application/history.py::query_at_revision` does not reconstruct anything —
it loads the current, fully-materialized graph and filters it against each
record's own revision-keyed validity using `RevisionGraph`, per record:

- An entity is present at revision `R` if its `first_seen_revision` is `R` or
  an ancestor of it, and it was not superseded at a revision that is `R` or
  an ancestor of it. `Entity` itself never records *when* a supersession
  happened (`identity/resolver.py::apply`'s `SUPERSEDES` branch flips
  `status` but leaves `last_seen_revision` untouched) — the lineage
  `Relationship`'s own `valid_from_revision` is the only record of that
  moment, so the query reads it from there instead.
- A relationship is present at `R` under the same rule applied to
  `valid_from_revision`/`valid_until_revision` — the exact fields
  `_reconcile_relationships` (`application/indexing.py`) already maintains
  for the ordinary, non-historical case.
- `revision=None` means today: not-superseded entities and `is_current`
  edges, the same set an ordinary (non-historical) query already returns.

This is deliberately a second, higher-level mechanism, not a retrofit of
`GraphRepository.find_entities(revision=...)` / `get_relationships(revision=...)`
— those two port methods still raise `NotImplementedError` on both storage
backends, reserved for a future storage-native, indexed implementation that
does not need to load a whole system's graph into memory per query. Getting
the semantics right first, at fixture/demo scale, was the explicit ask.

**A real bug this surfaced**: filtering relationships to "both endpoints
present at this revision" silently dropped every `SUPERSEDES` edge the
moment a query reached or passed the revision it was minted at — because a
lineage edge's entire purpose is to point from a currently-present entity
back at one that, by definition, is not. Fixed by exempting `SUPERSEDES`
from the target-presence check (`application/history.py::_bounded`); found
by the second brutal integration test below, not by inspection.

Proven end-to-end in `tests/integration/test_temporal_queries.py`: a real
Git history (commits A–D) over the Django fixture where a structural edit at
C breaks the `Payment.status` impact chain and a route change at D must not
leak backwards; and a second scenario combining the existing rename-lineage
machinery with revision-scoped querying — the pre-rename entity visible only
before the move, the post-rename entity (with its `GIT_RENAME` claim and
`SUPERSEDES` edge intact) visible only from the move onward, and an
unrelated *disguised* rename in the same history still refused at every
revision, proving the revision-query layer cannot launder a bad merge into
looking legitimate just because time passed.

## Identity resolution (§10) — v0.2: declaration-level lineage

["The `GIT_RENAME` gap is now closed, conditionally"](#identity-resolution-10--status)
above already named the next boundary precisely: Git's rename detection is
file-level, so a construct renamed *within* a file that itself never moved
has nothing for that mechanism to detect. This milestone closes that case
for declarations an adapter can enumerate before and after a change (today,
SQLAlchemy columns) — not by loosening `GIT_RENAME`'s own file-centric
design (asked for explicitly: "don't distort it to accommodate symbol-level
evolution"), but with a new, sibling identity-claim kind.

**`IdentityClaimKind.DECLARATION_LINEAGE`** (`core/enums.py`) sits at
corroborating tier, next to `GIT_RENAME` and `MIGRATION_LINEAGE` — one hit
alone justifies `SUPERSEDES` lineage, never an outright merge, the same
ladder rule every corroborating signal already follows. What is new is
where the evidence comes from: `GitAdapter` reports a `MODIFIED` file's
content *before* the change (`git.file_change.old_content`) as a raw fact,
nothing more; `adapters/sqlalchemy/adapter.py::_detect_declaration_renames`
re-parses that old content with the adapter's own column extraction and
compares it to the current source. Only an unambiguous 1:1 disappearance/
appearance in the same table, with the same type family, becomes a
`sqlalchemy.declaration_rename` fact; `identity/declaration_evidence.py::
attach_declaration_lineage_evidence` is what turns an unambiguous fact into
a claim on both entities, mirroring `git_evidence.py::attach_rename_evidence`'s
shape exactly, one level down (a declaration inside a file, not a file
inside a repository).

**Three cases, one discipline.** A clean rename (same type family) resolves
`SUPERSEDES` with `CERTAIN` or `LIKELY` confidence (matching whether every
detail — not just the family — agreed). A delete-and-recreate with no
correspondence signal, or more than one candidate on either side, produces
no claim at all — the existing, already-correct behavior (an orphaned old
entity, a disconnected `NEW`) is exactly what happens, unchanged. A rename
where the type family *also* changed (`String` becoming `Integer`) is
deliberately treated the same as the second case: the milestone's own
instruction was explicit — "the system should not automatically conclude
'same entity because the name changed'" — so no claim is proposed, not a
weak one. Nothing here is a similarity threshold in disguise: every gate is
a structural fact (exactly one candidate on each side; the type family
agrees or it does not), never a score.

**A real bug this surfaced, in code the temporal-queries and impact-analysis
milestones both already depended on.** `application/indexing.py::
_reconcile_relationships` fetched *every* outgoing edge of an entity to
decide what counts as a stale structural edge to close — including
`SUPERSEDES`, which this run's structural observations never contain (it is
a one-time historical fact minted once, not a recurring one). Without an
explicit exclusion, any existing lineage edge read as "no longer observed"
on the very next, otherwise unrelated re-index and got closed
(`valid_until_revision` set) — silently erasing lineage that
`application.history`'s `_bounded` exemption, and `application.impact`'s
`resolve_identity`, both assume is permanent. The bug had been latent since
the Git-rename milestone; nothing until this one's three-commit killer test
(A: rename → B: an unrelated later change) exercised a re-index *after* a
supersession existed. Fixed by excluding `SUPERSEDES` from reconciliation
entirely, with a regression test added to `test_git_identity.py` (the
original mechanism's own home) proving the fix, not just this milestone's
new one.

Proven end-to-end in `tests/integration/test_identity_evolution.py`, over
the same combined FastAPI + SQLAlchemy + Git fixture the impact-analysis
milestone used: `payments.status` renamed to `payments.state` in one commit
(only the model), the *usage* catching up in a later, separate commit
(only the service) — deliberately not collapsed into one atomic change,
because the gap between "identity survived" and "anything actually uses the
new name yet" is itself the thing worth being able to answer precisely.
`application.impact.reverse_impact`, asked about the pre-rename id at any
later revision, transparently follows the lineage and reports exactly what
was truly reachable at that point — empty, in the gap between the two
commits; the full chain again, once the usage caught up.

## Coverage is not confidence — Impact Analysis v0.2

A real agent experiment run against Hashira's own MCP read surface (§27;
`docs/ROADMAP.md`'s entry on this milestone has the full account) produced
the finding this section exists to keep permanent: `reverse_impact`
returning two paths for `Payment.status`, both `CERTAIN`, was a completely
true statement about the edges Hashira's adapters actually captured — and
a capable agent still had to redo the entire investigation by hand, because
nothing in the result distinguished "this is everything" from "this is
everything we happened to find." A correct traversal is not automatically
a complete answer, and treating the two as the same claim is a product bug,
not merely an internal one.

**Three states, not two, describe any one candidate access site** (a
`payment.status` read, say):

- **FOUND** — a relationship was captured; it appears in `paths` with its
  own, unmodified `Confidence`. Confidence and coverage answer different
  questions: `Confidence` is "how strong is the evidence *for a
  relationship that exists*"; coverage is "how much of the relevant
  surface did this analysis examine at all." A `CERTAIN` `WRITES` edge says
  nothing about whether some *other* write to the same field went
  unobserved.
- **NOT_OBSERVABLE** — the surface is structurally invisible to every
  configured adapter (raw SQL, an unindexed external SDK, a dynamically
  computed attribute name). No adapter has a foothold to even enumerate
  these, so they are reported categorically, as plain-language entries in
  `ImpactCoverage.limitations` (`application/impact.py`), sourced from
  `AdapterCapabilities.known_limitations` (`ports/adapters.py`) — never
  silently absent from a result that otherwise looks complete.
- **recognized-but-unresolved** — an adapter saw something plausibly
  relevant (an attribute name matching a real column) but could not
  determine the accessing object's type through any provenance form it
  supports, and says so, counted in `ImpactCoverage.unresolved_access_count`.
  This is v0.2's honest, *narrower* stand-in for the ideal
  `NOT_FOUND_AFTER_COVERAGE` state (an adapter examining a specific
  accessor and affirmatively confirming no relevant access exists there) —
  Hashira does not yet produce that rigorous a guarantee; what it produces
  instead is a real, adapter-reported count of accesses it recognized as
  ambiguous rather than either resolving or ignoring them. The distinction
  from `NOT_OBSERVABLE` matters and must never collapse: "we looked and
  couldn't tell" is not the same claim as "there is nothing here to look
  at," and conflating them is exactly the bug the agent experiment found.

**Two invariants, now permanent, not just a description of what v0.2
happens to do:**

1. Every `ImpactResult` communicates whether its paths represent complete
   analysis or known analytical limitations. Zero (or few) paths must never
   read as "nothing else exists" without `coverage` saying so explicitly —
   `ImpactResult.coverage` is a required field, not optional, precisely so
   this cannot be silently omitted.
2. Coverage must never modify an individual relationship's `Confidence`. A
   `CERTAIN` edge stays `CERTAIN` inside a result whose `coverage.status`
   is `PARTIAL` — weakening per-edge confidence to stand in for incomplete
   analysis would turn `Confidence` into exactly the score-in-disguise
   ["ordinal, not a float"](#confidence-is-ordinal-not-a-float) was
   designed to refuse. `tests/unit/test_impact.py::
   test_coverage_never_changes_a_hop_s_confidence` is the regression test.

**Typed object provenance, widened by one form.** `_local_model_instances`
(`payment = Payment()`) was the only provenance `adapters/sqlalchemy/
adapter.py` recognized before this milestone. v0.2 adds
`_typed_parameter_instances` (`def process(self, payment: Payment)`) — the
second of what the module docstring calls "typed object provenance," a
general concept of which local instantiation and typed parameters are only
two forms; a return value, an attribute, a collection element, and a
factory call remain unsupported and are named explicitly rather than
guessed past. Proven against the real, load-bearing `fastapi_checkout`
fixture: `PaymentService.mark_refunded(self, payment: Payment)` (new in
this milestone) is now a genuine `WRITES` edge in `reverse_impact`'s
result, where before this change it would have been silently absent —
every existing test asserting an exact affected-entity set had to be
updated to include it, which is itself evidence the fix reaches real code,
not only synthetic unit fixtures.

**Deliberately not attempted: raw SQL parsing.** The agent experiment's
raw-SQL finding (a worker reading a renamed-away column through
`sqlalchemy.text(...)`) was resolved by making the limitation *visible*
(`AdapterCapabilities.known_limitations`), not by building a parser for it.
Building the parser first would have repeated the exact mistake this
milestone exists to correct: each future adapter capability just moves the
point at which an unflagged gap becomes misleading again, unless coverage
semantics exist first to make every remaining gap honest. `payments/
reporting.py` (new in the `fastapi_checkout` fixture) is a real raw-SQL
reader over `payments.status`; `tests/integration/test_mcp_read_surface.py`
asserts that an MCP `reverse_impact` response for that column explicitly
names this limitation, over the real wire protocol, not just internally.
