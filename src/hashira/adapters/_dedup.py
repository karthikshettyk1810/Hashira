"""A relationship-deduplication utility shared across normalizers.

Not a public API (the leading underscore is deliberate — this is
implementation plumbing for adapter normalizers, not part of any adapter's
port contract). Pulled out of `adapters.python.normalizer` so a second
normalizer (`adapters.django.normalizer`) can reuse the exact same rule
rather than re-deriving it, or worse, silently doing it differently.
"""

from __future__ import annotations

from ..core.enums import RelationshipType
from ..core.relationships import Relationship

__all__ = ["deduplicate_relationships"]


def deduplicate_relationships(relationships: list[Relationship]) -> list[Relationship]:
    """Multiple call sites (or import statements, or base-class mentions, or
    field accesses...) between the same two entities must not become
    multiple relationship rows: `CALLS` means "does A call B at all", not
    "how many times". Merge every contributing observation's evidence onto
    one relationship instead of losing it or duplicating the edge.

    This matters beyond tidiness: `IndexingService`'s relationship
    reconciliation diffs by `(source, target, type)` against what is already
    current in storage (application/indexing.py) — an un-deduplicated
    candidate set here would make even a perfectly unchanged file look
    different from itself between runs, one call site at a time.
    """
    merged: dict[tuple[str, str, RelationshipType], Relationship] = {}
    for rel in relationships:
        key = (rel.source_entity_id, rel.target_entity_id, rel.type)
        existing = merged.get(key)
        if existing is None:
            merged[key] = rel
            continue
        combined_evidence = list(existing.evidence_ids)
        for evidence_id in rel.evidence_ids:
            if evidence_id not in combined_evidence:
                combined_evidence.append(evidence_id)
        merged[key] = existing.model_copy(update={"evidence_ids": combined_evidence})
    return list(merged.values())
