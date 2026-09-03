"""The SQLite table layout (spec §21, §22).

Every table follows the same shape: a handful of indexed columns for the
filters the ports actually need, plus one ``data`` column holding the
*entire* record as JSON. This is a deliberate trade against hand-mapping every
domain field to a column:

* §31 requires unknown fields to survive a round trip. A column-per-field
  mapping has to be told about a new field before it can store it; a JSON
  column already stores whatever Pydantic's ``extra="allow"`` accepted, with
  no schema change required.
* §21 recommends exactly this — "PostgreSQL as the authoritative store, with
  JSONB for extensible metadata" — generalized here to the whole record, since
  the record's own versioned shape (docs/IR.md) is already the contract; a
  second, parallel column-level contract would only drift from it.

The indexed columns exist purely so the ports' query parameters (``system_id``,
``type``, ``name``, direction, time ranges, …) can be pushed into SQL rather
than requiring a full scan-and-filter in Python.
"""

from __future__ import annotations

import sqlalchemy as sa

metadata = sa.MetaData()

systems = sa.Table(
    "systems",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("slug", sa.String, nullable=False, unique=True, index=True),
    sa.Column("data", sa.JSON, nullable=False),
)

entities = sa.Table(
    "entities",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("system_id", sa.String, nullable=False, index=True),
    sa.Column("type", sa.String, nullable=False, index=True),
    sa.Column("name", sa.String, nullable=False, index=True),
    sa.Column("qualified_name", sa.String, nullable=True, index=True),
    sa.Column("status", sa.String, nullable=False, index=True),
    sa.Column("data", sa.JSON, nullable=False),
)

relationships = sa.Table(
    "relationships",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("system_id", sa.String, nullable=False, index=True),
    sa.Column("source_entity_id", sa.String, nullable=False, index=True),
    sa.Column("target_entity_id", sa.String, nullable=False, index=True),
    sa.Column("type", sa.String, nullable=False, index=True),
    sa.Column("is_current", sa.Boolean, nullable=False, index=True),
    sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
    sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
    sa.Column("data", sa.JSON, nullable=False),
)

events = sa.Table(
    "events",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("system_id", sa.String, nullable=False, index=True),
    sa.Column("type", sa.String, nullable=False, index=True),
    sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False, index=True),
    sa.Column("dedupe_key", sa.String, nullable=False),
    sa.Column("data", sa.JSON, nullable=False),
    sa.UniqueConstraint("system_id", "dedupe_key", name="uq_events_system_dedupe"),
)

#: Events reference an arbitrary number of subject entities; a side table
#: keeps `EventStore.find(entity_id=...)` a plain indexed join instead of a
#: JSON scan, and stays portable to PostgreSQL without a JSON-path operator.
event_subjects = sa.Table(
    "event_subjects",
    metadata,
    sa.Column("event_id", sa.String, sa.ForeignKey("events.id"), primary_key=True, index=True),
    sa.Column("entity_id", sa.String, primary_key=True, index=True),
)

observations = sa.Table(
    "observations",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("system_id", sa.String, nullable=False, index=True),
    sa.Column("adapter", sa.String, nullable=False, index=True),
    sa.Column("revision", sa.String, nullable=True, index=True),
    sa.Column("data", sa.JSON, nullable=False),
)

#: Evidence is append-only (core/evidence.py::Evidence is frozen); this table
#: has deliberately no update path, matching EvidenceStore's port contract.
evidence = sa.Table(
    "evidence",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("system_id", sa.String, nullable=False, index=True),
    sa.Column("data", sa.JSON, nullable=False),
)

inferences = sa.Table(
    "inferences",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("system_id", sa.String, nullable=False, index=True),
    sa.Column("status", sa.String, nullable=False, index=True),
    sa.Column("data", sa.JSON, nullable=False),
)

inference_subjects = sa.Table(
    "inference_subjects",
    metadata,
    sa.Column(
        "inference_id",
        sa.String,
        sa.ForeignKey("inferences.id"),
        primary_key=True,
        index=True,
    ),
    sa.Column("entity_id", sa.String, primary_key=True, index=True),
)

snapshots = sa.Table(
    "snapshots",
    metadata,
    sa.Column("id", sa.String, primary_key=True),
    sa.Column("system_id", sa.String, nullable=False, index=True),
    sa.Column("status", sa.String, nullable=False, index=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
    sa.Column("data", sa.JSON, nullable=False),
)

#: Revision ancestry (§13). Keyed by `(system_id, sha)` rather than the
#: record's own opaque id -- callers look these up by the natural VCS
#: identifier already used everywhere else (`Entity.first_seen_revision`,
#: `Relationship.valid_from_revision`, `Snapshot.revision`), never by id.
revisions = sa.Table(
    "revisions",
    metadata,
    sa.Column("system_id", sa.String, primary_key=True),
    sa.Column("sha", sa.String, primary_key=True),
    sa.Column("data", sa.JSON, nullable=False),
)
