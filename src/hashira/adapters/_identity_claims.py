"""Shared identity-claim builders for normalizers.

Not a public API — see `_dedup.py` for the same rationale. Every candidate
entity any normalizer mints should carry the same two claims, built the same
way: a `QUALIFIED_NAME` claim and a `DECLARATION_ANCHOR` claim (file path +
qualified name + kind, all at once — see `IdentityClaimKind.DECLARATION_ANCHOR`'s
docstring in `core/enums.py` for why that pair is what makes an unchanged
re-index resolve as `MATCHED` rather than manufacturing a new entity
generation). Two normalizers computing this differently would make identity
behavior depend on which adapter produced a candidate, which defeats the
point of a single, adapter-agnostic identity ladder.
"""

from __future__ import annotations

from ..core.entities import IdentityClaim
from ..core.enums import Confidence, IdentityClaimKind, Origin

__all__ = ["declaration_anchor_claim", "qualified_name_claim"]


def qualified_name_claim(qualified_name: str, *, origin: Origin = Origin.PARSER) -> IdentityClaim:
    return IdentityClaim(
        kind=IdentityClaimKind.QUALIFIED_NAME,
        value=qualified_name,
        origin=origin,
        confidence=Confidence.CERTAIN,
    )


def declaration_anchor_claim(
    file: str, qualified_name: str, kind: str, *, origin: Origin = Origin.PARSER
) -> IdentityClaim:
    return IdentityClaim(
        kind=IdentityClaimKind.DECLARATION_ANCHOR,
        value=f"{file}::{qualified_name}::{kind}",
        origin=origin,
        confidence=Confidence.CERTAIN,
    )
