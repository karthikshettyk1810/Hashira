"""Identity resolution: the algorithm behind spec §10's stable entity identity."""

from .resolver import (
    IdentityResolution,
    ResolutionDecision,
    ResolutionOutcome,
    apply,
    merge_into,
    resolve,
)

__all__ = [
    "IdentityResolution",
    "ResolutionDecision",
    "ResolutionOutcome",
    "apply",
    "merge_into",
    "resolve",
]
