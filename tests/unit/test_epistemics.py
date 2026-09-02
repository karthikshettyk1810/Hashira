"""Facts and guesses must never share a trust category (spec §12, §26)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hashira.core import (
    Confidence,
    Evidence,
    Inference,
    InferenceStatus,
    KnowledgeClass,
    Observation,
    Origin,
    System,
    default_confidence,
    is_deterministic,
)


def test_confidence_is_ordinal_and_comparable() -> None:
    assert Confidence.CERTAIN > Confidence.LIKELY > Confidence.SPECULATIVE
    assert max([Confidence.SPECULATIVE, Confidence.CERTAIN]) is Confidence.CERTAIN


def test_llm_origin_is_not_deterministic() -> None:
    assert is_deterministic(Origin.STATIC_ANALYSIS)
    assert not is_deterministic(Origin.LLM)
    assert default_confidence(Origin.LLM) is Confidence.SPECULATIVE
    assert default_confidence(Origin.GIT) is Confidence.CERTAIN


def test_evidence_is_immutable(evidence: Evidence) -> None:
    with pytest.raises(ValidationError):
        evidence.summary = "rewritten history"


def test_observation_rejects_model_origin(system: System) -> None:
    """A model's output is an Inference. There is no path making it an Observation."""
    with pytest.raises(ValidationError, match="cannot produce an Observation"):
        Observation(
            system_id=system.id,
            adapter="python@0.1.0",
            origin=Origin.LLM,
            kind="python.function",
        )


def test_llm_inference_must_name_its_model(system: System) -> None:
    with pytest.raises(ValidationError, match="provider and model"):
        Inference(
            system_id=system.id,
            knowledge_class=KnowledgeClass.INFERENCE,
            statement="PaymentService belongs to checkout",
            origin=Origin.LLM,
        )


def test_llm_cannot_assert_certainty(system: System) -> None:
    with pytest.raises(ValidationError, match="cannot assert CERTAIN"):
        Inference(
            system_id=system.id,
            knowledge_class=KnowledgeClass.INFERENCE,
            statement="PaymentService belongs to checkout",
            origin=Origin.LLM,
            provider="anthropic",
            model="claude-opus-5",
            confidence=Confidence.CERTAIN,
        )


def test_supported_status_requires_evidence(system: System) -> None:
    with pytest.raises(ValidationError, match="requires evidence"):
        Inference(
            system_id=system.id,
            knowledge_class=KnowledgeClass.DERIVATION,
            statement="PaymentService participates in payment processing",
            origin=Origin.DERIVED,
            status=InferenceStatus.SUPPORTED,
        )


def test_an_observation_class_is_not_an_inference(system: System) -> None:
    with pytest.raises(ValidationError, match="not an Inference"):
        Inference(
            system_id=system.id,
            knowledge_class=KnowledgeClass.OBSERVATION,
            statement="the file exists",
            origin=Origin.PARSER,
        )


def test_speculative_hypothesis_is_not_actionable(system: System, evidence: Evidence) -> None:
    hypothesis = Inference(
        system_id=system.id,
        knowledge_class=KnowledgeClass.HYPOTHESIS,
        statement="the transaction change caused the checkout failures",
        origin=Origin.LLM,
        provider="anthropic",
        model="claude-opus-5",
        evidence_ids=[evidence.id],
    )
    assert hypothesis.confidence is Confidence.SPECULATIVE
    assert not hypothesis.is_actionable

    hypothesis.status = InferenceStatus.CONFIRMED
    hypothesis.confidence = Confidence.LIKELY
    assert hypothesis.is_actionable
