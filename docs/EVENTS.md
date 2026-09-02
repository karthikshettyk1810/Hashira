# Events

Spec §14. Events are immutable records of meaningful occurrences — the
authoritative *sequence* of system history. See
[IR.md#one-temporal-source-of-truth](IR.md#one-temporal-source-of-truth) for
why they are not used to rebuild graph state.

## Event types (§14)

`REPOSITORY_DISCOVERED`, `CODE_INDEXED`, `COMMIT_CREATED`, `FILE_CHANGED`,
`TEST_EXECUTED`, `TEST_FAILED`, `TEST_PASSED`, `DEPLOYMENT_STARTED`,
`DEPLOYMENT_COMPLETED`, `DEPLOYMENT_FAILED`, `RUNTIME_ERROR`,
`RUNTIME_METRIC`, `INCIDENT_CREATED`, `INCIDENT_RESOLVED`,
`DECISION_RECORDED`, `CONFIGURATION_CHANGED`.

Plus one addition: `ENTITY_IDENTITY_MERGED` — emitted whenever identity
resolution joins two prior entities, so a merge is always visible in history
rather than silently reshaping the graph (§10). See
[IR.md#supersedes-added-to-the-relationship-vocabulary](IR.md#supersedes-added-to-the-relationship-vocabulary).

## Shape (`core/events.py::Event`)

```python
Event(
    id="evt_...",
    system_id="sys_...",
    type=EventType.COMMIT_CREATED,
    timestamp=...,
    actor=Actor(type=ActorType.DEVELOPER, id="...", name="..."),
    source=SourceRef(provider="git", reference="abc123"),
    subject_entity_ids=[...],
    payload={...},
    dedupe_key="...",  # derived automatically if not supplied
)
```

`Event` is frozen (`ImmutableIRModel`) — the append-only guarantee is
structural, not procedural.

## Idempotency (§30)

"Events must be idempotently ingestible." `Event.natural_key()` derives a
digest from `(system_id, type, source.provider, source.reference or
timestamp)` and stores it as `dedupe_key`. An `EventStore.append()`
implementation is expected to key on `dedupe_key`: appending the same
underlying occurrence twice (a replayed webhook, a re-run indexer) must
return the existing event rather than fork history. This is exercised in
`tests/contract/test_ports.py::test_event_append_is_idempotent` against the
in-memory reference implementation; a real store (SQLite/Postgres) is held to
the same test once it exists, via a unique index on `(system_id, dedupe_key)`.

If a producer has a better natural key than "provider + reference" (e.g. a
webhook delivery id that is itself already unique and stable), it should pass
`dedupe_key` explicitly rather than rely on the derived one.

## Actors (§16, §17)

Every event and change carries an `Actor` — `DEVELOPER`, `AGENT`, `SYSTEM`,
`INTEGRATION`, or `UNKNOWN`. `UNKNOWN` is a legitimate value: recording "we
don't know who" is more honest than guessing, and is what a backfilled or
partially-attributed event should use rather than a fabricated actor.

## Ordering and query

`EventStore.find()` (`ports/repositories.py`) filters by system, type range,
time range and subject entity, and returns results ordered by `timestamp`.
There is no global sequence number in v0.1 — `timestamp` plus the
lexicographically time-ordered `EventID` (a ULID, see `core/ids.py`) is
sufficient for the query patterns in §13 and does not require coordinating a
single writer.
