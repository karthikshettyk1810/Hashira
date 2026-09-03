"""GitRepository against a real `git` binary and real temporary repositories.

Git's plumbing output has enough edge cases (root commits, merges, rename
scores) that mocking it would just be testing our own assumptions about the
format. A real repo, built fresh per test, is the only trustworthy fixture.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hashira.adapters.git.repository import GitRepository, is_git_repository
from hashira.adapters.git.runner import GitCommandError, GitNotFoundError, run_git


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    return root


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _write(root: Path, relpath: str, content: str) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


# --- runner --------------------------------------------------------------


def test_run_git_raises_on_nonzero_exit(tmp_path: Path) -> None:
    with pytest.raises(GitCommandError):
        run_git(["rev-parse", "--verify", "not-a-real-ref"], cwd=tmp_path)


def test_run_git_raises_clearly_on_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        run_git(["status"], cwd=tmp_path / "does-not-exist")


def test_git_not_found_error_is_distinct_from_command_error() -> None:
    assert issubclass(GitNotFoundError, RuntimeError)
    assert not issubclass(GitNotFoundError, GitCommandError)


# --- is_git_repository -----------------------------------------------------


def test_is_git_repository_true_for_a_real_repo(repo: Path) -> None:
    assert is_git_repository(repo)


def test_is_git_repository_false_for_a_plain_directory(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert not is_git_repository(plain)


def test_is_git_repository_false_for_a_missing_path(tmp_path: Path) -> None:
    assert not is_git_repository(tmp_path / "does-not-exist")


# --- commits ---------------------------------------------------------------


def test_root_commit_has_no_parents(repo: Path) -> None:
    _write(repo, "a.py", "x = 1\n")
    sha = _commit(repo, "initial")
    info = GitRepository(repo).commit_info(sha)
    assert info.parent_shas == ()
    assert info.is_root
    assert not info.is_merge
    assert info.message == "initial"
    assert info.author_email == "test@example.com"


def test_commits_are_topologically_ordered(repo: Path) -> None:
    _write(repo, "a.py", "x = 1\n")
    first = _commit(repo, "first")
    _write(repo, "a.py", "x = 2\n")
    second = _commit(repo, "second")
    _write(repo, "a.py", "x = 3\n")
    third = _commit(repo, "third")

    commits = GitRepository(repo).commits()
    assert [c.sha for c in commits] == [first, second, third]


def test_commits_since_excludes_already_reachable(repo: Path) -> None:
    _write(repo, "a.py", "x = 1\n")
    first = _commit(repo, "first")
    _write(repo, "a.py", "x = 2\n")
    second = _commit(repo, "second")

    commits = GitRepository(repo).commits(since=first)
    assert [c.sha for c in commits] == [second]


def test_merge_commit_has_two_parents_in_first_parent_order(repo: Path) -> None:
    _write(repo, "a.py", "x = 1\n")
    base = _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "b.py", "y = 1\n")
    feature_tip = _commit(repo, "feature work")
    _git(repo, "checkout", "-q", "-")
    _git(repo, "merge", "-q", "--no-ff", "feature", "-m", "merge feature")
    merge_sha = GitRepository(repo).current_revision()

    info = GitRepository(repo).commit_info(merge_sha)
    assert info.is_merge
    assert info.parent_shas == (base, feature_tip) or info.parent_shas[0] == base


def test_commit_message_containing_pipe_is_still_parsed(repo: Path) -> None:
    _write(repo, "a.py", "x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fix: a | b | c")
    sha = GitRepository(repo).current_revision()
    info = GitRepository(repo).commit_info(sha)
    assert info.message == "fix: a | b | c"


# --- changed paths -----------------------------------------------------


def test_added_file_is_reported(repo: Path) -> None:
    first = None
    _write(repo, "a.py", "x = 1\n")
    first = _commit(repo, "first")
    _write(repo, "b.py", "y = 1\n")
    second = _commit(repo, "second")

    changes = GitRepository(repo).changed_paths(first, second)
    assert len(changes) == 1
    assert changes[0].status == "ADDED"
    assert changes[0].path == "b.py"
    assert changes[0].old_path is None


def test_exact_rename_is_detected_with_full_similarity(repo: Path) -> None:
    content = "class PaymentService:\n    def process(self, amount):\n        return amount\n"
    _write(repo, "payments.py", content)
    first = _commit(repo, "first")
    _git(repo, "mv", "payments.py", "billing.py")
    second = _commit(repo, "rename")

    changes = GitRepository(repo).changed_paths(first, second)
    assert len(changes) == 1
    change = changes[0]
    assert change.status == "RENAMED"
    assert change.old_path == "payments.py"
    assert change.path == "billing.py"
    assert change.similarity == 1.0


def test_rename_with_modest_edit_reports_partial_similarity(repo: Path) -> None:
    original = "\n".join(f"def f{i}(): return {i}" for i in range(20))
    _write(repo, "payments.py", original + "\n")
    first = _commit(repo, "first")
    _git(repo, "mv", "payments.py", "billing.py")
    edited = original.replace("def f0(): return 0", "def f0(): return 999")
    _write(repo, "billing.py", edited + "\n")
    second = _commit(repo, "rename and tweak")

    changes = GitRepository(repo).changed_paths(first, second)
    assert len(changes) == 1
    assert changes[0].status == "RENAMED"
    assert changes[0].similarity is not None
    assert 0.5 < changes[0].similarity < 1.0


def test_rename_with_total_rewrite_is_not_detected_as_a_rename(repo: Path) -> None:
    """The adversarial case: git mv plus a complete content rewrite should
    not be classified as a rename at all -- it should read as a plain
    delete + add, since the content shares nothing with the original."""
    _write(
        repo,
        "payments.py",
        "class PaymentService:\n"
        "    def process(self, amount):\n"
        "        return amount\n"
        "    def validate(self, amount):\n"
        "        return amount > 0\n"
        "    def refund(self, amount):\n"
        "        return -amount\n",
    )
    first = _commit(repo, "first")
    _git(repo, "mv", "payments.py", "billing.py")
    _write(repo, "billing.py", "x = 1\ny = 2\nz = 3\n")
    second = _commit(repo, "total rewrite disguised as rename")

    changes = GitRepository(repo).changed_paths(first, second)
    statuses = {(c.status, c.path) for c in changes}
    assert statuses == {("ADDED", "billing.py"), ("DELETED", "payments.py")}


def test_changed_paths_in_commit_for_a_root_commit(repo: Path) -> None:
    _write(repo, "a.py", "x = 1\n")
    _write(repo, "b.py", "y = 1\n")
    sha = _commit(repo, "initial")

    changes = GitRepository(repo).changed_paths_in_commit(sha)
    paths = {c.path for c in changes}
    assert paths == {"a.py", "b.py"}
    assert all(c.status == "ADDED" for c in changes)


def test_changed_paths_in_commit_uses_first_parent_for_a_merge(repo: Path) -> None:
    """v0.1 deliberately does not attempt full merge-diff reasoning -- a
    merge commit's changes are reported relative to its first parent only."""
    _write(repo, "a.py", "x = 1\n")
    _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "b.py", "y = 1\n")
    _commit(repo, "feature work")
    _git(repo, "checkout", "-q", "-")
    _git(repo, "merge", "-q", "--no-ff", "feature", "-m", "merge feature")
    merge_sha = GitRepository(repo).current_revision()

    changes = GitRepository(repo).changed_paths_in_commit(merge_sha)
    # From the first parent's (base's) perspective, b.py is new.
    assert {c.path for c in changes} == {"b.py"}
