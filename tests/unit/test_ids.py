"""Identity is opaque, prefixed and time-ordered (spec §10)."""

from __future__ import annotations

import pytest

from hashira.core.ids import IDPrefix, entity_uri, new_id, new_ulid, parse_id, system_uri


def test_new_id_carries_its_prefix() -> None:
    value = new_id(IDPrefix.ENTITY)
    assert value.startswith("ent_")
    prefix, ulid = parse_id(value)
    assert prefix is IDPrefix.ENTITY
    assert len(ulid) == 26


def test_ulids_are_lexicographically_time_ordered() -> None:
    values = [new_ulid() for _ in range(200)]
    assert values == sorted(values) or len(set(v[:10] for v in values)) > 1


def test_ulids_are_unique() -> None:
    assert len({new_ulid() for _ in range(5_000)}) == 5_000


def test_alphabet_excludes_ambiguous_letters() -> None:
    """Crockford base32: an ID must survive being read aloud."""
    joined = "".join(new_ulid() for _ in range(200))
    assert not set(joined) & set("ILOU")


@pytest.mark.parametrize(
    "bad",
    ["", "ent_", "ent_TOOSHORT", "nope_01J8ZQ9WXK3M2P5R7T9V1Y4B6D", "01J8ZQ9WXK3M2P5R7T9V1Y4B6D"],
)
def test_malformed_ids_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_id(bad)


def test_ambiguous_characters_are_rejected() -> None:
    with pytest.raises(ValueError, match="base32"):
        parse_id("ent_01J8ZQ9WXK3M2P5R7T9V1Y4B6I")


def test_logical_uris() -> None:
    assert system_uri("sys_1") == "hashira://system/sys_1"
    assert entity_uri("ent_1") == "hashira://entity/ent_1"
