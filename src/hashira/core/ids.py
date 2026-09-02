"""Opaque, sortable identity for every System IR record.

Spec §10: identity is not location. A symbol may move file, a service may be
renamed, a table may be migrated. Persistence therefore keys on an opaque ID and
keeps semantic identifiers (qualified names, URIs) as separate, mutable evidence
*about* the entity.

IDs are ULIDs with a type prefix: ``ent_01J8ZQ9WXK3M2P5R7T9V1Y4B6D``. ULID gives
lexicographic ordering by creation time without leaking a database sequence, and
the prefix makes a dangling reference obvious in a log line.
"""

from __future__ import annotations

import os
import time
from enum import StrEnum
from typing import Annotated, Final

from pydantic import StringConstraints

# Crockford base32: no I, L, O or U, so an ID cannot be misread aloud.
_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_LEN: Final = 26
_ULID_PATTERN: Final = r"[0-9A-HJKMNP-TV-Z]{26}"


class IDPrefix(StrEnum):
    """The record kinds that carry an opaque identity."""

    SYSTEM = "sys"
    ENTITY = "ent"
    RELATIONSHIP = "rel"
    EVENT = "evt"
    EVIDENCE = "ev"
    OBSERVATION = "obs"
    INFERENCE = "inf"
    SNAPSHOT = "snp"
    CHANGE = "chg"
    INCIDENT = "inc"
    DEPLOYMENT = "dep"
    INDEX_RUN = "idx"


def _encode_ulid(timestamp_ms: int, randomness: bytes) -> str:
    """Encode 48 bits of time and 80 bits of randomness as 26 base32 chars."""
    value = (timestamp_ms << 80) | int.from_bytes(randomness, "big")
    out = [""] * _ULID_LEN
    for i in range(_ULID_LEN - 1, -1, -1):
        out[i] = _ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(out)


def new_ulid() -> str:
    """A fresh time-ordered ULID."""
    return _encode_ulid(int(time.time() * 1000), os.urandom(10))


def new_id(prefix: IDPrefix) -> str:
    """Mint a prefixed identifier, e.g. ``ent_01J8ZQ9WXK3M2P5R7T9V1Y4B6D``."""
    return f"{prefix.value}_{new_ulid()}"


def parse_id(value: str) -> tuple[IDPrefix, str]:
    """Split a prefixed identifier, raising ``ValueError`` if it is malformed."""
    prefix, _, ulid = value.partition("_")
    if not ulid or len(ulid) != _ULID_LEN:
        raise ValueError(f"not a Hashira id: {value!r}")
    try:
        kind = IDPrefix(prefix)
    except ValueError as exc:
        raise ValueError(f"unknown id prefix {prefix!r} in {value!r}") from exc
    if any(char not in _ALPHABET for char in ulid):
        raise ValueError(f"id {value!r} is not valid Crockford base32")
    return kind, ulid


def _typed(prefix: IDPrefix) -> object:
    return StringConstraints(pattern=rf"^{prefix.value}_{_ULID_PATTERN}$")


SystemID = Annotated[str, _typed(IDPrefix.SYSTEM)]
EntityID = Annotated[str, _typed(IDPrefix.ENTITY)]
RelationshipID = Annotated[str, _typed(IDPrefix.RELATIONSHIP)]
EventID = Annotated[str, _typed(IDPrefix.EVENT)]
EvidenceID = Annotated[str, _typed(IDPrefix.EVIDENCE)]
ObservationID = Annotated[str, _typed(IDPrefix.OBSERVATION)]
InferenceID = Annotated[str, _typed(IDPrefix.INFERENCE)]
SnapshotID = Annotated[str, _typed(IDPrefix.SNAPSHOT)]
ChangeID = Annotated[str, _typed(IDPrefix.CHANGE)]
IncidentID = Annotated[str, _typed(IDPrefix.INCIDENT)]
DeploymentID = Annotated[str, _typed(IDPrefix.DEPLOYMENT)]
IndexRunID = Annotated[str, _typed(IDPrefix.INDEX_RUN)]


def system_uri(system_id: str) -> str:
    """Logical identity for a system (spec §10)."""
    return f"hashira://system/{system_id}"


def entity_uri(entity_id: str) -> str:
    """Logical identity for an entity (spec §10)."""
    return f"hashira://entity/{entity_id}"
