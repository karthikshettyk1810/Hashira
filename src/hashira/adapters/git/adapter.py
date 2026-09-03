"""`GitAdapter`: the `HistoryAdapter` port, satisfied.

This adapter's entire job is to report what Git itself already knows — it
never decides what a detected rename *means* for entity identity. That
distinction is the point (see the design discussion this was built from):

```
GitAdapter  ->  "here is evidence that this path moved from A to B"
hashira.identity  ->  "is that evidence enough to say these are the same entity?"
```

Git's own rename detection is a similarity heuristic, not a fact — a 94%
match and a 51% match are not the same claim, and this adapter keeps that
score attached to the observation (`similarity` in the payload) rather than
collapsing it into a boolean "renamed: yes". Whether a given score clears the
bar for treating it as identity evidence at all is `hashira.identity`'s
policy (`identity/git_evidence.py`), not this adapter's — this file only
ever reports what Git said, never a decision about it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ...core.base import Actor, SourceRef
from ...core.enums import ActorType, EventType, Origin
from ...core.events import Event
from ...core.evidence import Evidence, Observation
from ...core.ids import SystemID
from ...ports.adapters import AdapterCapabilities, ExtractionResult
from .repository import CommitInfo, FileChange, GitRepository, is_git_repository
from .runner import GitCommandError, GitNotFoundError

__all__ = ["GitAdapter"]


class GitAdapter:
    """Commit history and file-change/rename evidence for one repository."""

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            name="git",
            version="0.1.0",
            ir_versions=["0.1.2"],
            # No entity_types/relationship_types: this adapter produces
            # Observations and Events only, never Entity/Relationship
            # objects of its own (§38 — an honest empty list here, not
            # aspirational, since nothing normalizes git.commit/
            # git.file_change observations into graph entities yet).
            requires_network=False,
        )

    def detect(self, root: Path) -> bool:
        return is_git_repository(root)

    def extract(
        self,
        root: Path,
        *,
        system_id: SystemID,
        since_revision: str | None,
        until_revision: str | None,
    ) -> ExtractionResult:
        if not is_git_repository(root):
            return ExtractionResult(errors=[f"{root} is not a Git repository"])

        repo = GitRepository(root)
        result = ExtractionResult()
        try:
            until = until_revision or repo.current_revision()
        except (GitCommandError, GitNotFoundError) as exc:
            result.errors.append(f"could not resolve current revision: {exc}")
            return result
        now = datetime.now(UTC)

        # A caller-supplied revision (e.g. a prior snapshot's) is untrusted
        # input from this adapter's point of view — it may not exist (a
        # placeholder like "unknown", a rewritten or pruned commit). Per §30,
        # an adapter tolerates partial failure rather than crashing the whole
        # indexing run: report it and carry on with whatever *is* available.
        try:
            commits = repo.commits(since=since_revision, until=until)
        except GitCommandError as exc:
            result.errors.append(f"could not read commit history: {exc}")
            commits = []

        for commit in commits:
            obs, ev, event = _commit_records(commit, system_id=system_id, now=now)
            result.observations.append(obs)
            result.evidence.append(ev)
            result.events.append(event)

        # A rename is only meaningful evidence relative to a prior indexed
        # state; on a first-ever index (no `since_revision`) there is
        # nothing to diff against, and nothing yet in storage to link to.
        if since_revision is not None and since_revision != until:
            try:
                changes = repo.changed_paths(since_revision, until)
            except GitCommandError as exc:
                result.errors.append(f"could not diff {since_revision}..{until}: {exc}")
                changes = []
            for change in changes:
                obs, ev = _file_change_records(
                    change,
                    system_id=system_id,
                    from_revision=since_revision,
                    to_revision=until,
                    now=now,
                )
                result.observations.append(obs)
                result.evidence.append(ev)

        return result


def _commit_records(
    commit: CommitInfo, *, system_id: SystemID, now: datetime
) -> tuple[Observation, Evidence, Event]:
    source = SourceRef(provider="git", reference=commit.sha, retrieved_at=now)
    evidence = Evidence(
        system_id=system_id,
        origin=Origin.GIT,
        source=source,
        summary=f"commit {commit.sha[:8]}: {commit.message}",
        locator=commit.sha,
        observed_at=commit.committed_at,
    )
    observation = Observation(
        system_id=system_id,
        adapter="git@0.1.0",
        origin=Origin.GIT,
        kind="git.commit",
        payload={
            "sha": commit.sha,
            "parent_shas": list(commit.parent_shas),
            "is_merge": commit.is_merge,
            "is_root": commit.is_root,
            "author_name": commit.author_name,
            "author_email": commit.author_email,
            "authored_at": commit.authored_at.isoformat(),
            "committed_at": commit.committed_at.isoformat(),
            "message": commit.message,
        },
        evidence_ids=[evidence.id],
        revision=commit.sha,
        observed_at=now,
    )
    event = Event(
        system_id=system_id,
        type=EventType.COMMIT_CREATED,
        timestamp=commit.committed_at,
        actor=Actor(type=ActorType.DEVELOPER, name=commit.author_name, id=commit.author_email),
        source=source,
        payload={
            "sha": commit.sha,
            "message": commit.message,
            "parent_shas": list(commit.parent_shas),
        },
    )
    return observation, evidence, event


def _file_change_records(
    change: FileChange,
    *,
    system_id: SystemID,
    from_revision: str,
    to_revision: str,
    now: datetime,
) -> tuple[Observation, Evidence]:
    summary = (
        f"{change.old_path} -> {change.path} ({change.status.lower()}"
        f"{f', {change.similarity:.0%} similar' if change.similarity is not None else ''})"
        if change.old_path
        else f"{change.path} {change.status.lower()}"
    )
    evidence = Evidence(
        system_id=system_id,
        origin=Origin.GIT,
        source=SourceRef(
            provider="git", reference=f"{from_revision}..{to_revision}", retrieved_at=now
        ),
        summary=summary,
        locator=change.path,
        observed_at=now,
    )
    observation = Observation(
        system_id=system_id,
        adapter="git@0.1.0",
        origin=Origin.GIT,
        kind="git.file_change",
        payload={
            "status": change.status,
            "path": change.path,
            "old_path": change.old_path,
            "similarity": change.similarity,
            "from_revision": from_revision,
            "to_revision": to_revision,
        },
        evidence_ids=[evidence.id],
        revision=to_revision,
        observed_at=now,
    )
    return observation, evidence
