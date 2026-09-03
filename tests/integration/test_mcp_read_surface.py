"""MCP read surface v0.1's own marquee test: a real MCP protocol client,
talking to `hashira.mcp.build_server` over `InMemoryTransport` (no
subprocess, no external client -- "introduces unrelated variables" was the
milestone's own instruction), against the combined FastAPI + SQLAlchemy +
Git fixture every framework/data/history milestone before this one already
proved out.

Sequence, matching the milestone's own words: search Payment -> find
Payment.status -> reverse_impact -> service -> route handler -> route. Then
take an entity from an older revision -> follow_lineage -> new entity id ->
reverse_impact at the later revision. Every assertion below checks that
Hashira's own evidence and epistemic detail (confidence, evidence records,
identity claims, `resolved_from`) survived the full round trip through the
wire protocol -- not just that some JSON came back.

This proves exactly what the milestone set out to prove: an external agent
can discover a system concept, inspect why Hashira believes it exists,
understand its impact, travel through history, and continue reasoning about
the evolved concept -- without knowing anything about Django, FastAPI,
SQLAlchemy, Git internals, SQLite, or Hashira's own Python implementation.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.server.mcpserver import MCPServer

from hashira.adapters._compose import compose_normalizers
from hashira.adapters.fastapi import FastAPIAdapter
from hashira.adapters.fastapi.normalizer import enrich_normalized_run as enrich_fastapi
from hashira.adapters.git import GitAdapter, GitRepository
from hashira.adapters.python import PythonAdapter
from hashira.adapters.sqlalchemy import SQLAlchemyAdapter
from hashira.adapters.sqlalchemy.normalizer import enrich_normalized_run as enrich_sqlalchemy
from hashira.application import IndexingService, query_at_revision
from hashira.core import IdentityClaimKind, System
from hashira.mcp import build_server
from hashira.storage.memory import MemoryDatabase

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fastapi_checkout"

_EXPECTED_AFFECTED = {
    "payments.services.PaymentService.process",
    "payments.routers.checkout",
    "payments.routers.router:POST /payments/checkout/",
    "payments.tests.test_checkout.TestCheckout.test_process_marks_payment_captured",
}


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return GitRepository(root).current_revision()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    shutil.copytree(FIXTURE, root)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    return root


@pytest.fixture
def db() -> MemoryDatabase:
    return MemoryDatabase()


@pytest.fixture
def system(db: MemoryDatabase) -> System:
    system = System(name="Checkout", slug="checkout-mcp-marquee")
    with db.unit_of_work() as uow:
        uow.systems.save(system)
        uow.commit()
    return system


def _indexing_service(db: MemoryDatabase) -> IndexingService:
    normalize = compose_normalizers(enrich_fastapi, enrich_sqlalchemy)
    return IndexingService(
        db.unit_of_work,
        [PythonAdapter()],
        normalize,
        framework_adapters=[FastAPIAdapter()],
        data_adapters=[SQLAlchemyAdapter()],
        history_adapters=[GitAdapter()],
    )


@asynccontextmanager
async def _session(server: MCPServer) -> AsyncIterator[ClientSession]:
    async with (
        InMemoryTransport(server) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


async def _call(server: MCPServer, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    async with _session(server) as session:
        result = await session.call_tool(tool, args)
        assert not result.is_error, result.content
        assert result.structured_content is not None
        return result.structured_content


def test_agent_discovers_impact_and_lineage_through_the_wire_protocol(
    repo: Path, db: MemoryDatabase, system: System
) -> None:
    indexer = _indexing_service(db)
    server = build_server(db.unit_of_work, system_id=system.id)

    # --- A: real history, real code, indexed once -----------------------
    sha_a = _commit(repo, "A: checkout flow writes Payment.status")
    result_a = indexer.index(repo, system_id=system.id, revision=sha_a)
    assert result_a.errors == []

    with db.unit_of_work() as uow:
        status_at_a = query_at_revision(uow, system_id=system.id, revision=sha_a).entity(
            "payments.status"
        )
    assert status_at_a is not None

    # --- search Payment -> find Payment.status, over the wire -----------
    search_payload = asyncio.run(_call(server, "search_entities", {"query": "Payment"}))
    search_hits = {e["qualified_name"]: e["id"] for e in search_payload["results"]}
    assert "payments.status" in search_hits
    status_id = search_hits["payments.status"]
    assert status_id == status_at_a.id

    # --- reverse_impact -> service -> route handler -> route ------------
    impact_payload = asyncio.run(
        _call(server, "reverse_impact", {"entity_id": status_id, "revision": sha_a})
    )
    assert impact_payload["direction"] == "reverse"
    assert impact_payload["resolved_from"] is None
    affected = {e["qualified_name"]: e for e in _entities_by_id(server, impact_payload)}
    assert set(affected) == _EXPECTED_AFFECTED

    # Evidence -- real Evidence records, not just a relationship label --
    # survived serialization across the wire, for every hop of every path.
    for path in impact_payload["paths"]:
        for hop in path["hops"]:
            assert hop["relationship"]["knowledge_class"] == "OBSERVATION"
            assert hop["evidence"], "expected at least one Evidence record per hop"
            for ev in hop["evidence"]:
                assert ev["locator"]
                assert ev["source"]["provider"]

    # get_entity confirms the same id independently, with full context.
    entity_payload = asyncio.run(_call(server, "get_entity", {"entity_id": status_id}))
    assert entity_payload["found"] is True
    assert entity_payload["entity"]["qualified_name"] == "payments.status"

    # query_at_revision reproduces the historical primitive for one node.
    qar_payload = asyncio.run(
        _call(server, "query_at_revision", {"entity_id": status_id, "revision": sha_a})
    )
    assert qar_payload["found"] is True
    assert qar_payload["entity"]["id"] == status_id

    # --- B: only the model renames -- a declaration-level rename, no ----
    # file move for Git to report -- so lineage here rests on
    # DECLARATION_LINEAGE identity claims, not GIT_RENAME evidence.
    db_models_py = repo / "payments" / "db_models.py"
    db_models_py.write_text(db_models_py.read_text().replace("status", "state"))
    sha_b = _commit(repo, "B: rename the status column to state")
    result_b = indexer.index(repo, system_id=system.id, revision=sha_b)
    assert result_b.errors == []

    with db.unit_of_work() as uow:
        state_at_b = query_at_revision(uow, system_id=system.id, revision=sha_b).entity(
            "payments.state"
        )
    assert state_at_b is not None

    # --- old entity id -> follow_lineage -> new entity id, over the wire -
    lineage_payload = asyncio.run(_call(server, "follow_lineage", {"entity_id": status_id}))
    assert lineage_payload["start"]["id"] == status_id
    assert lineage_payload["predecessors"] == []
    successors = lineage_payload["successors"]
    assert len(successors) == 1
    successor = successors[0]
    assert successor["successor"]["id"] == state_at_b.id
    assert successor["relationship"]["type"] == "SUPERSEDES"
    # The rationale Hashira actually reasoned with survives the round trip.
    assert successor["relationship"]["metadata"]["rationale"]
    # And the successor entity itself still carries the DECLARATION_LINEAGE
    # claim that justified the lineage -- an agent reading this over MCP
    # can see *why* Hashira believes this is the same construct, not just
    # that it says so.
    claim_kinds = {c["kind"] for c in successor["successor"]["identity_claims"]}
    assert IdentityClaimKind.DECLARATION_LINEAGE.value in claim_kinds

    new_id = successor["successor"]["id"]

    # --- reverse_impact at the later revision, continuing from the new id
    impact_at_b = asyncio.run(
        _call(server, "reverse_impact", {"entity_id": new_id, "revision": sha_b})
    )
    assert impact_at_b["resolved_from"] is None  # asked by the current id directly
    assert impact_at_b["affected_entity_ids"] == []  # services.py has not caught up yet

    # Asking about the *old* id at the later revision still resolves,
    # via lineage, to the current entity -- an agent that only remembers
    # the id from step A can keep reasoning at B without knowing a rename
    # happened at all.
    impact_via_old_id = asyncio.run(
        _call(server, "reverse_impact", {"entity_id": status_id, "revision": sha_b})
    )
    assert impact_via_old_id["resolved_from"] == status_id
    assert impact_via_old_id["start"]["id"] == new_id


def _entities_by_id(server: MCPServer, impact_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every `endpoint` entity that reverse_impact's paths actually
    reached, deduplicated by id -- read straight from the already-lossless
    impact payload rather than issuing more calls."""
    seen: dict[str, dict[str, Any]] = {}
    for path in impact_payload["paths"]:
        endpoint = path["endpoint"]
        seen[endpoint["id"]] = endpoint
    return list(seen.values())
