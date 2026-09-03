"""Parsed Git primitives: commits, ancestry, and file changes between two
revisions. Everything here is a thin, tested wrapper over `runner.run_git` —
no rename *decision* lives here, only rename *detection*, exactly as Git
itself reports it. What that evidence is worth is `adapter.py`'s and
eventually `hashira.identity`'s call, not this module's.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .runner import GitCommandError, run_git

__all__ = ["CommitInfo", "FileChange", "GitRepository", "is_git_repository"]

_LOG_FORMAT = "%H|%P|%an|%ae|%aI|%cI|%s"


def is_git_repository(path: Path) -> bool:
    """Cheap, side-effect-free check. Must not raise on an ordinary
    non-repository directory — that is the expected, common answer."""
    if not path.is_dir():
        return False
    try:
        output = run_git(["rev-parse", "--is-inside-work-tree"], cwd=path)
    except GitCommandError:
        return False
    return output.strip() == "true"


@dataclass(frozen=True, slots=True)
class CommitInfo:
    """One commit, exactly as Git records it. Parent order matters for a
    merge commit — the first parent is the branch that was merged *into*."""

    sha: str
    parent_shas: tuple[str, ...]
    author_name: str
    author_email: str
    authored_at: datetime
    committed_at: datetime
    message: str

    @property
    def is_merge(self) -> bool:
        return len(self.parent_shas) > 1

    @property
    def is_root(self) -> bool:
        return len(self.parent_shas) == 0


@dataclass(frozen=True, slots=True)
class FileChange:
    """One path's fate between two revisions. `old_path` is set only for
    ``RENAMED``/``COPIED``; `similarity` (0.0-1.0) only for those two, and is
    Git's own heuristic score — evidence to weigh, not a fact to trust
    blindly (see `adapter.py`'s module docstring)."""

    status: str
    """One of ADDED, MODIFIED, DELETED, RENAMED, COPIED, TYPE_CHANGED."""
    path: str
    old_path: str | None = None
    similarity: float | None = None


_STATUS_NAMES = {
    "A": "ADDED",
    "M": "MODIFIED",
    "D": "DELETED",
    "R": "RENAMED",
    "C": "COPIED",
    "T": "TYPE_CHANGED",
}


def _parse_commit_line(line: str) -> CommitInfo:
    sha, parents, author_name, author_email, authored_at, committed_at, message = line.split("|", 6)
    return CommitInfo(
        sha=sha,
        parent_shas=tuple(parents.split()) if parents else (),
        author_name=author_name,
        author_email=author_email,
        authored_at=datetime.fromisoformat(authored_at),
        committed_at=datetime.fromisoformat(committed_at),
        message=message,
    )


def _parse_name_status_z(raw: str) -> list[FileChange]:
    """Parse `git diff -z --name-status` output: NUL-separated tokens, a
    rename/copy status carrying a 3-digit similarity score and two path
    tokens, everything else one status token and one path token."""
    tokens = raw.split("\x00")
    changes: list[FileChange] = []
    i = 0
    while i < len(tokens):
        status_field = tokens[i]
        if not status_field:
            i += 1
            continue
        code = status_field[0]
        kind = _STATUS_NAMES.get(code, code)
        if code in ("R", "C"):
            similarity = int(status_field[1:]) / 100.0 if len(status_field) > 1 else None
            old_path, new_path = tokens[i + 1], tokens[i + 2]
            changes.append(
                FileChange(status=kind, path=new_path, old_path=old_path, similarity=similarity)
            )
            i += 3
        else:
            changes.append(FileChange(status=kind, path=tokens[i + 1]))
            i += 2
    return changes


class GitRepository:
    """One repository, rooted at `root`. Every method is a read; nothing
    here ever mutates the repository it inspects."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def current_revision(self) -> str:
        return run_git(["rev-parse", "HEAD"], cwd=self.root).strip()

    def commit_info(self, revision: str) -> CommitInfo:
        output = run_git(["log", "-1", f"--format={_LOG_FORMAT}", revision], cwd=self.root)
        return _parse_commit_line(output.strip())

    def commits(
        self, *, since: str | None = None, until: str = "HEAD", limit: int | None = None
    ) -> Sequence[CommitInfo]:
        """Commits reachable from ``until``, topologically ordered (parents
        before children never appear after their descendants) and excluding
        anything already reachable from ``since``. ``since=None`` means the
        full history up to ``until``.

        Topological order, not commit-timestamp order (§ this adapter's
        design discussion): a commit's timestamp can be wrong or out of
        order; its position in the DAG cannot.
        """
        rev_range = f"{since}..{until}" if since else until
        args = ["log", "--topo-order", "--reverse", f"--format={_LOG_FORMAT}", rev_range]
        if limit is not None:
            args.extend(["-n", str(limit)])
        output = run_git(args, cwd=self.root)
        return [_parse_commit_line(line) for line in output.splitlines() if line]

    def changed_paths(self, from_revision: str, to_revision: str) -> Sequence[FileChange]:
        """Every file's fate between two revisions, with rename/copy
        detection enabled at Git's default similarity threshold. The raw
        similarity score travels with each result — deciding whether it is
        strong enough to act on is not this method's job.
        """
        output = run_git(
            ["diff", "--name-status", "-M", "-C", "-z", from_revision, to_revision],
            cwd=self.root,
        )
        return _parse_name_status_z(output)

    def changed_paths_in_commit(self, revision: str) -> Sequence[FileChange]:
        """Changes introduced by one commit, relative to its first parent
        (or the empty tree, for a root commit). For a merge commit this is
        deliberately the simple, first-parent view — see this adapter's
        design discussion on why v0.1 does not attempt full merge diffing.
        """
        info = self.commit_info(revision)
        if info.is_root:
            output = run_git(
                ["diff", "--name-status", "-M", "-C", "-z", _EMPTY_TREE_SHA, revision],
                cwd=self.root,
            )
            return _parse_name_status_z(output)
        return self.changed_paths(info.parent_shas[0], revision)

    def show(self, revision: str, path: str) -> str | None:
        """A file's exact content at one revision, or `None` if it did not
        exist there (a newly-added file, a typo'd path, a path that only
        exists on another branch). This is the raw material a later,
        declaration-level diff is built from (`adapters/sqlalchemy/adapter.py`'s
        old-vs-new column comparison) — this method reports content only,
        never an interpretation of what changed within it.
        """
        try:
            return run_git(["show", f"{revision}:{path}"], cwd=self.root)
        except GitCommandError:
            return None


#: The well-known empty-tree object id, valid in every Git repository —
#: needed to diff a root commit (one with no parent) against "nothing".
_EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
