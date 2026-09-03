"""Revision ancestry: the DAG of history a system has been observed at (§13).

The core knows revision *semantics*, not Git. A `Revision` is a normalized
ancestry record -- a natural-key sha plus its parent shas -- independent of
whichever VCS produced it. Today only `hashira.adapters.git.GitAdapter`
populates one (from `CommitInfo.parent_shas`, via
`application.indexing._extract_revisions`), but nothing in this module
imports Git, and nothing about the shape assumes Git specifically.

`sha` is deliberately the same natural, provider-specific string already used
everywhere else in the IR (`Entity.first_seen_revision`,
`Relationship.valid_from_revision`, `Snapshot.revision`) -- this record exists
to attach *ancestry* to that identifier, not to replace it with a second,
opaque one every caller would have to translate through.

`RevisionGraph` answers ancestry questions ("was R2 reached by the time we
got to R1?") from a loaded set of these records, so `application.history`'s
revision-scoped queries never have to shell out to Git (or any other VCS) at
query time.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import Field

from .base import IRModel, utc_now
from .ids import IDPrefix, RevisionID, SystemID, new_id

__all__ = ["Revision", "RevisionGraph"]


class Revision(IRModel):
    """One point in a system's history, as recorded by whatever VCS produced
    it. Ancestry, not content: this is not a commit's diff or its files,
    only the DAG position that lets a later query decide "did revision R
    exist by the time we reached revision S"."""

    id: RevisionID = Field(default_factory=lambda: new_id(IDPrefix.REVISION))
    system_id: SystemID
    sha: str = Field(min_length=1, description="The provider's own revision identifier.")
    parent_shas: tuple[str, ...] = Field(default_factory=tuple)
    provider: str = Field(default="git", description="Which VCS this ancestry came from.")
    authored_at: datetime | None = None
    message: str | None = None
    recorded_at: datetime = Field(default_factory=utc_now)


class RevisionGraph:
    """A loaded, in-memory view of one system's revision DAG, built from
    stored `Revision` records. Ancestry is plain graph reachability over
    `parent_shas` -- no Git call, no timestamp comparison (a commit's clock
    can be wrong; its position in the DAG cannot).

    Deliberately boring, per the design discussion this was built from: no
    persistent index, no caching across instances. At the scale a full
    re-index already operates at (`application.indexing._ALL_ENTITIES_LIMIT`),
    loading every revision for a system once and walking it per query is
    fast enough; a storage-native ancestry index is explicitly deferred.
    """

    def __init__(self, revisions: Sequence[Revision]) -> None:
        self._by_sha: dict[str, Revision] = {r.sha: r for r in revisions}

    def __contains__(self, sha: str) -> bool:
        return sha in self._by_sha

    def parents(self, sha: str) -> tuple[str, ...]:
        revision = self._by_sha.get(sha)
        return revision.parent_shas if revision is not None else ()

    def ancestors(self, sha: str) -> set[str]:
        """Every sha reachable by walking `parent_shas` from `sha`,
        excluding `sha` itself. A sha with no recorded ancestry (e.g. one
        from before Hashira started tracking this system) simply has no
        ancestors rather than raising -- an absent fact is not an error
        here (§12)."""
        seen: set[str] = set()
        frontier = list(self.parents(sha))
        while frontier:
            next_frontier: list[str] = []
            for parent_sha in frontier:
                if parent_sha in seen:
                    continue
                seen.add(parent_sha)
                next_frontier.extend(self.parents(parent_sha))
            frontier = next_frontier
        return seen

    def is_ancestor(self, ancestor_sha: str, descendant_sha: str) -> bool:
        """Whether `ancestor_sha` is a proper ancestor of `descendant_sha`."""
        return ancestor_sha in self.ancestors(descendant_sha)

    def is_ancestor_or_self(self, sha: str, of: str) -> bool:
        """Whether `sha` is `of`, or a proper ancestor of it -- the relation
        a historical query actually wants: "had revision `sha` been reached
        by the time we got to `of`?"."""
        return sha == of or self.is_ancestor(sha, of)

    def ancestry_between(self, since_sha: str | None, until_sha: str) -> list[str]:
        """Shas reachable from `until_sha` but not from `since_sha`
        (everything reachable from `until_sha`, if `since_sha` is None),
        including `until_sha` itself -- the commit range a Git-style
        `since..until` diff covers, expressed over stored ancestry instead
        of a fresh `git log` call. Order is not chronological; sort by
        whatever the caller actually needs (e.g. join back to `Revision`
        records and sort by `authored_at`)."""
        reachable = {until_sha, *self.ancestors(until_sha)}
        if since_sha is not None:
            reachable -= {since_sha, *self.ancestors(since_sha)}
        return sorted(reachable)
