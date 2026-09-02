"""Shared model configuration and the small value objects reused across System IR.

Two rules from the spec are enforced structurally here rather than by convention:

* §31 — "unknown extension fields should be preserved when possible". Every IR
  record allows extra fields and round-trips them untouched, so a v0.1 reader
  does not destroy data written by a v0.2 producer.
* §12 — "evidence is immutable". Records that represent history are frozen.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .enums import ActorType


def utc_now() -> datetime:
    """Timezone-aware now. Naive datetimes are rejected at the model boundary."""
    return datetime.now(UTC)


class IRModel(BaseModel):
    """Base for every System IR record.

    ``extra="allow"`` is the forward-compatibility contract, not laziness: a
    consumer must ignore fields it does not understand and preserve them on the
    way back out (§31).
    """

    model_config = ConfigDict(
        extra="allow",
        use_enum_values=False,
        validate_assignment=True,
        str_strip_whitespace=True,
        ser_json_timedelta="iso8601",
    )


class ImmutableIRModel(IRModel):
    """An IR record that history depends on and therefore cannot be edited.

    A later observation may supersede one of these, but it may never rewrite it.
    """

    model_config = ConfigDict(
        extra="allow",
        frozen=True,
        use_enum_values=False,
        str_strip_whitespace=True,
    )


class SourceLocation(IRModel):
    """Where a construct was seen. Evidence about an entity, never its identity (§10)."""

    repository: str | None = None
    revision: str | None = Field(
        default=None, description="Commit SHA or equivalent immutable revision id."
    )
    file: str | None = Field(default=None, description="Repository-relative path.")
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)

    def model_post_init(self, _context: Any) -> None:
        start, end = self.line_start, self.line_end
        if start is not None and end is not None and end < start:
            raise ValueError("line_end precedes line_start")


class TechnologyInfo(IRModel):
    """The stack an entity belongs to.

    Adapters populate this; the core never branches on it. Anything more specific
    than these three axes belongs in an adapter extension (§18).
    """

    language: str | None = None
    framework: str | None = None
    runtime: str | None = None


class Actor(IRModel):
    """Who caused something. ``UNKNOWN`` is legitimate and better than a guess."""

    type: ActorType = ActorType.UNKNOWN
    id: str | None = None
    name: str | None = None


class SourceRef(IRModel):
    """The external system a record came from, and its reference there."""

    provider: str = Field(description="e.g. 'git', 'github', 'sentry', 'pytest'.")
    reference: str | None = Field(
        default=None, description="Provider-local identifier: a SHA, a URL, an issue key."
    )
    retrieved_at: datetime | None = None
