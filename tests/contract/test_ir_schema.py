"""System IR is a public contract (spec §31).

The golden file is the review surface: any diff to it in a pull request is a
deliberate decision about a versioned contract, not an accident.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from hashira.core import IR_MODELS, IR_VERSION, full_schema, json_schema, supports_ir_version
from hashira.core.entities import Entity
from hashira.core.enums import EntityType

GOLDEN = pathlib.Path(__file__).resolve().parents[2] / "docs" / "schema" / f"ir-{IR_VERSION}.json"


@pytest.mark.contract
def test_schema_matches_the_golden_file() -> None:
    assert GOLDEN.exists(), "missing golden schema; run scripts/export_schema.py"
    expected = json.loads(GOLDEN.read_text())
    actual = full_schema()
    assert actual == expected, (
        "System IR changed. If this is intended, bump IR_VERSION per docs/IR.md "
        "and regenerate with scripts/export_schema.py."
    )


@pytest.mark.contract
def test_every_public_model_is_exported() -> None:
    for name in IR_MODELS:
        schema = json_schema(name)
        assert schema["title"] == name
        assert "properties" in schema


@pytest.mark.contract
def test_unknown_model_is_not_a_contract() -> None:
    with pytest.raises(KeyError, match="not part of the System IR contract"):
        json_schema("Nonexistent")


@pytest.mark.contract
def test_version_compatibility_window() -> None:
    assert supports_ir_version("0.1.0")
    assert supports_ir_version("0.1.99")
    assert not supports_ir_version("0.2.0")
    assert not supports_ir_version("1.0.0")
    with pytest.raises(ValueError, match="malformed"):
        supports_ir_version("nope")


@pytest.mark.contract
def test_unknown_fields_survive_a_round_trip() -> None:
    """§31: a v0.1 reader must not destroy data a v0.2 producer wrote."""
    payload = {
        "id": "ent_01J8ZQ9WXK3M2P5R7T9V1Y4B6D",
        "system_id": "sys_01J8ZQ9WXK3M2P5R7T9V1Y4B6D",
        "type": "SYMBOL",
        "name": "process",
        "future_field": {"added_in": "0.2.0", "value": [1, 2, 3]},
    }
    entity = Entity.model_validate(payload)
    assert entity.type is EntityType.SYMBOL
    assert entity.model_dump()["future_field"] == payload["future_field"]
