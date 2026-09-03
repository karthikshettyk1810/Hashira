"""Identity resolution: the algorithm behind spec §10's stable entity identity."""

from .declaration_evidence import DeclarationRename, attach_declaration_lineage_evidence
from .git_evidence import DEFAULT_MIN_SIMILARITY, GitRename, attach_rename_evidence
from .resolver import (
    IdentityResolution,
    ResolutionDecision,
    ResolutionOutcome,
    apply,
    merge_into,
    resolve,
)

__all__ = [
    "DEFAULT_MIN_SIMILARITY",
    "DeclarationRename",
    "GitRename",
    "IdentityResolution",
    "ResolutionDecision",
    "ResolutionOutcome",
    "apply",
    "attach_declaration_lineage_evidence",
    "attach_rename_evidence",
    "merge_into",
    "resolve",
]
