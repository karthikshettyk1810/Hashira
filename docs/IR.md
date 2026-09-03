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
