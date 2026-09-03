"""`mcp.server.build_server`: real MCP protocol calls (via `InMemoryTransport`,
no subprocess) against each of the seven tools -- confirming both that a
tool's structured result matches its underlying `application/` call and that
Hashira's epistemic detail (confidence, evidence, knowledge_class) survives
the protocol round trip, per the milestone's own non-negotiable requirement.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.server.mcpserver import MCPServer

from hashira.core import (
    Confidence,
    Entity,
    EntityStatus,
    EntityType,
    Evidence,
    IdentityClaim,
    IdentityClaimKind,
    KnowledgeClass,
    Origin,
    Relationship,
    RelationshipType,
    SourceRef,
    System,
)
from hashira.mcp import build_server
from hashira.ports.repositories import UnitOfWork
from hashira.storage.memory import MemoryDatabase


def _entity(system: System, name: str) -> Entity:
    return Entity(system_id=system.id, type=EntityType.SYMBOL, name=name, qualified_name=name)


def _evidence(system: System) -> Evidence:
    return Evidence(
        system_id=system.id,
        origin=Origin.STATIC_ANALYSIS,
        source=SourceRef(provider="python-ast", reference="app.py"),
        summary="observed",
        locator="app.py:1",
    )


def _rel(
    system: System,
    source: Entity,
    target: Entity,
    rel_type: RelationshipType,
    evidence: Evidence,
) -> Relationship:
    return Relationship(
        system_id=system.id,
        source_entity_id=source.id,
        target_entity_id=target.id,
        type=rel_type,
        origin=Origin.STATIC_ANALYSIS,
        evidence_ids=[evidence.id],
    )


@pytest.fixture
def db() -> MemoryDatabase:
    return MemoryDatabase()


@pytest.fixture
def system(db: MemoryDatabase) -> System:
    system = System(name="Checkout", slug="checkout-mcp")
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()
    return system


@pytest.fixture
def server(db: MemoryDatabase, system: System) -> MCPServer:
    uow_factory: Callable[[], UnitOfWork] = db.unit_of_work
    return build_server(uow_factory, system_id=system.id)


@asynccontextmanager
async def _session(server: MCPServer) -> AsyncIterator[ClientSession]:
    async with (
        InMemoryTransport(server) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


def _run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def test_list_tools_exposes_all_seven(server: MCPServer) -> None:
    async def scenario() -> set[str]:
        async with _session(server) as session:
            result = await session.list_tools()
            return {t.name for t in result.tools}

    names = _run(scenario())

    assert names == {
        "get_entity",
        "get_relationships",
        "query_at_revision",
        "reverse_impact",
        "forward_impact",
        "follow_lineage",
        "search_entities",
    }


def test_get_entity_round_trip(db: MemoryDatabase, system: System, server: MCPServer) -> None:
    entity = _entity(system, "Payment.status")
    with db.unit_of_work() as uow:
        uow.graph.upsert_entities([entity])
        uow.commit()

    async def scenario() -> dict[str, object]:
        async with _session(server) as session:
            result = await session.call_tool("get_entity", {"entity_id": entity.id})
            assert not result.is_error
            assert result.structured_content is not None
            return result.structured_content

    payload = _run(scenario())

    assert payload["found"] is True
    assert payload["entity"]["id"] == entity.id  # type: ignore[index]
    assert payload["entity"]["qualified_name"] == "Payment.status"  # type: ignore[index]


def test_get_entity_unknown_id(server: MCPServer) -> None:
    async def scenario() -> dict[str, object]:
        async with _session(server) as session:
            result = await session.call_tool("get_entity", {"entity_id": "ent_nope"})
            assert result.structured_content is not None
            return result.structured_content

    payload = _run(scenario())

    assert payload == {"found": False, "entity_id": "ent_nope"}


def test_search_then_get_relationships_preserves_evidence(
    db: MemoryDatabase, system: System, server: MCPServer
) -> None:
    status = _entity(system, "Payment.status")
    service = _entity(system, "PaymentService")
    ev = _evidence(system)
    rel = _rel(system, service, status, RelationshipType.WRITES, ev)
    with db.unit_of_work() as uow:
        uow.graph.upsert_entities([status, service])
        uow.graph.upsert_relationships([rel])
        uow.evidence.record([ev])
        uow.commit()

    async def scenario() -> tuple[dict[str, object], dict[str, object]]:
        async with _session(server) as session:
            search_result = await session.call_tool("search_entities", {"query": "Payment.status"})
            assert search_result.structured_content is not None
            rels_result = await session.call_tool(
                "get_relationships", {"entity_id": status.id, "direction": "in"}
            )
            assert rels_result.structured_content is not None
            return search_result.structured_content, rels_result.structured_content

    search_payload, rels_payload = _run(scenario())

    found_ids = {e["id"] for e in search_payload["results"]}  # type: ignore[index]
    assert status.id in found_ids

    relationships = rels_payload["relationships"]  # type: ignore[index]
    assert len(relationships) == 1
    assert relationships[0]["source_entity_id"] == service.id
    assert relationships[0]["confidence"] == "CERTAIN"
    assert relationships[0]["knowledge_class"] == "OBSERVATION"
    assert relationships[0]["evidence_ids"] == [ev.id]


def test_get_relationships_rejects_unknown_type(
    db: MemoryDatabase, system: System, server: MCPServer
) -> None:
    entity = _entity(system, "a")
    with db.unit_of_work() as uow:
        uow.graph.upsert_entities([entity])
        uow.commit()

    async def scenario() -> dict[str, object]:
        async with _session(server) as session:
            result = await session.call_tool(
                "get_relationships", {"entity_id": entity.id, "types": ["NOT_A_TYPE"]}
            )
            assert result.structured_content is not None
            return result.structured_content

    payload = _run(scenario())

    assert "error" in payload


def test_reverse_impact_round_trip(db: MemoryDatabase, system: System, server: MCPServer) -> None:
    route, handler, service = (
        _entity(system, "checkout_route"),
        _entity(system, "checkout_handler"),
        _entity(system, "PaymentService"),
    )
    ev = _evidence(system)
    with db.unit_of_work() as uow:
        uow.graph.upsert_entities([route, handler, service])
        uow.graph.upsert_relationships(
            [
                _rel(system, route, handler, RelationshipType.CALLS, ev),
                _rel(system, handler, service, RelationshipType.CALLS, ev),
            ]
        )
        uow.evidence.record([ev])
        uow.commit()

    async def scenario() -> dict[str, object]:
        async with _session(server) as session:
            result = await session.call_tool("reverse_impact", {"entity_id": service.id})
            assert result.structured_content is not None
            return result.structured_content

    payload = _run(scenario())

    assert payload["direction"] == "reverse"
    assert set(payload["affected_entity_ids"]) == {handler.id, route.id}  # type: ignore[arg-type]
    paths = payload["paths"]
    assert len(paths) == 2  # type: ignore[arg-type]
    route_path = next(p for p in paths if p["endpoint"]["id"] == route.id)  # type: ignore[index]
    assert len(route_path["hops"]) == 2
    assert route_path["hops"][0]["evidence"][0]["id"] == ev.id


def test_reverse_impact_unknown_entity_returns_error(server: MCPServer) -> None:
    async def scenario() -> dict[str, object]:
        async with _session(server) as session:
            result = await session.call_tool("reverse_impact", {"entity_id": "ent_nope"})
            assert result.structured_content is not None
            return result.structured_content

    payload = _run(scenario())

    assert "error" in payload


def test_follow_lineage_round_trip(db: MemoryDatabase, system: System, server: MCPServer) -> None:
    old = Entity(
        system_id=system.id,
        type=EntityType.SYMBOL,
        name="old_name",
        qualified_name="old_name",
        status=EntityStatus.SUPERSEDED,
        identity_claims=[
            IdentityClaim(
                kind=IdentityClaimKind.GIT_RENAME,
                value="rename::old_name",
                origin=Origin.GIT,
                confidence=Confidence.CERTAIN,
            )
        ],
    )
    new = _entity(system, "new_name")
    ev = _evidence(system)
    supersedes = Relationship(
        system_id=system.id,
        source_entity_id=new.id,
        target_entity_id=old.id,
        type=RelationshipType.SUPERSEDES,
        origin=Origin.DERIVED,
        knowledge_class=KnowledgeClass.DERIVATION,
        evidence_ids=[ev.id],
    )
    with db.unit_of_work() as uow:
        uow.graph.upsert_entities([old, new])
        uow.graph.upsert_relationships([supersedes])
        uow.evidence.record([ev])
        uow.commit()

    async def scenario() -> dict[str, object]:
        async with _session(server) as session:
            result = await session.call_tool("follow_lineage", {"entity_id": old.id})
            assert result.structured_content is not None
            return result.structured_content

    payload = _run(scenario())

    assert payload["start"]["id"] == old.id
    assert payload["predecessors"] == []
    successors = payload["successors"]
    assert len(successors) == 1  # type: ignore[arg-type]
    assert successors[0]["successor"]["id"] == new.id  # type: ignore[index]
    assert successors[0]["evidence"][0]["id"] == ev.id  # type: ignore[index]


def test_query_at_revision_round_trip(
    db: MemoryDatabase, system: System, server: MCPServer
) -> None:
    entity = _entity(system, "a").model_copy(
        update={"first_seen_revision": "rev2", "last_seen_revision": "rev2"}
    )
    with db.unit_of_work() as uow:
        uow.graph.upsert_entities([entity])
        uow.commit()

    async def scenario() -> tuple[dict[str, object], dict[str, object]]:
        async with _session(server) as session:
            at_rev1 = await session.call_tool(
                "query_at_revision", {"entity_id": entity.id, "revision": "rev1"}
            )
            at_rev2 = await session.call_tool(
                "query_at_revision", {"entity_id": entity.id, "revision": "rev2"}
            )
            assert at_rev1.structured_content is not None
            assert at_rev2.structured_content is not None
            return at_rev1.structured_content, at_rev2.structured_content

    rev1_payload, rev2_payload = _run(scenario())

    assert rev1_payload == {"found": False, "entity_id": entity.id, "revision": "rev1"}
    assert rev2_payload["found"] is True
    assert rev2_payload["entity"]["id"] == entity.id  # type: ignore[index]
