"""The versioned, machine-readable System IR contract (spec §31).

System IR is a public contract. Consumers — adapters, agents, other people's
tooling — are entitled to a JSON Schema they can validate against and a version
they can reason about. Exporting the schema from the same models the runtime uses
means the contract cannot silently drift from the code.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel

from .changes import Change
from .entities import Entity, System
from .events import Event
from .evidence import Evidence, Inference, Observation
from .incidents import Incident
from .relationships import Relationship
from .snapshots import Snapshot
from .state import Deployment, SystemState

IR_VERSION: Final = "0.1.1"
"""System IR schema version. Additive changes bump the patch; breaking changes
bump the minor while 0.x, and require a migration note in docs/IR.md."""

ADAPTER_CONTRACT_VERSION: Final = "0.1.0"
"""Adapter protocol version. An adapter declares the range it supports (§31)."""

EVENT_SCHEMA_VERSION: Final = "0.1.0"
"""Event payload envelope version."""

#: Every record type that is part of the public contract. A type absent from this
#: map is internal and may change without a version bump.
IR_MODELS: Final[dict[str, type[BaseModel]]] = {
    "System": System,
    "Entity": Entity,
    "Relationship": Relationship,
    "Evidence": Evidence,
    "Observation": Observation,
    "Inference": Inference,
    "Event": Event,
    "Snapshot": Snapshot,
    "Change": Change,
    "Incident": Incident,
    "Deployment": Deployment,
    "SystemState": SystemState,
}


def json_schema(name: str) -> dict[str, Any]:
    """JSON Schema for one IR record type."""
    try:
        model = IR_MODELS[name]
    except KeyError as exc:
        raise KeyError(f"{name!r} is not part of the System IR contract") from exc
    return model.model_json_schema(mode="serialization")


def full_schema() -> dict[str, Any]:
    """The complete versioned IR contract, suitable for writing to a file."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://hashira.dev/schema/ir/{IR_VERSION}.json",
        "title": "Hashira System IR",
        "version": IR_VERSION,
        "adapterContractVersion": ADAPTER_CONTRACT_VERSION,
        "eventSchemaVersion": EVENT_SCHEMA_VERSION,
        "$defs": {name: json_schema(name) for name in sorted(IR_MODELS)},
    }


def supports_ir_version(declared: str) -> bool:
    """Whether this build can read records written against ``declared``.

    While 0.x, the minor is the compatibility boundary: 0.1.7 reads 0.1.0, and
    0.2.0 does not read 0.1.0 without an explicit migration.
    """
    try:
        major, minor, _ = (int(part) for part in declared.split("."))
    except ValueError as exc:
        raise ValueError(f"malformed IR version {declared!r}") from exc
    our_major, our_minor, _ = (int(part) for part in IR_VERSION.split("."))
    return (major, minor) == (our_major, our_minor)
