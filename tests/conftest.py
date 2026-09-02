"""Shared fixtures. Deterministic by construction — no network, no clock skew."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hashira.core import (
    Entity,
    EntityType,
    Evidence,
    Origin,
    SourceLocation,
    SourceRef,
    System,
    TechnologyInfo,
)


@pytest.fixture
def system() -> System:
    return System(name="Food Platform", slug="food-platform", repositories=["repo_01"])


@pytest.fixture
def evidence(system: System) -> Evidence:
    return Evidence(
        system_id=system.id,
        origin=Origin.STATIC_ANALYSIS,
        source=SourceRef(provider="python-ast", reference="payments/services.py"),
        summary="PaymentService.process calls RazorpayClient.charge",
        locator="payments/services.py:57",
        observed_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    )


@pytest.fixture
def payment_service(system: System) -> Entity:
    return Entity(
        system_id=system.id,
        type=EntityType.SYMBOL,
        name="process",
        qualified_name="payments.services.PaymentService.process",
        source=SourceLocation(
            repository="repo_01",
            revision="abc123",
            file="payments/services.py",
            line_start=42,
            line_end=87,
        ),
        technology=TechnologyInfo(language="python", framework="django", runtime="cpython"),
    )


@pytest.fixture
def razorpay_client(system: System) -> Entity:
    return Entity(
        system_id=system.id,
        type=EntityType.EXTERNAL_SYSTEM,
        name="RazorpayClient",
        qualified_name="payments.clients.RazorpayClient",
    )
