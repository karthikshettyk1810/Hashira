"""GitAdapter: commits and rename evidence become Observations/Events, and
nothing about identity gets decided here."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hashira.adapters.git import GitAdapter, GitRepository
from hashira.core.enums import EventType, Origin
from hashira.core.ids import IDPrefix, new_id
from hashira.ports import HistoryAdapter


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


def _write(root: Path, relpath: str, content: str) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return GitRepository(root).current_revision()


@pytest.fixture
def system_id() -> str:
    return new_id(IDPrefix.SYSTEM)


def test_satisfies_the_history_adapter_port() -> None:
    assert isinstance(GitAdapter(), HistoryAdapter)


def test_detect_true_for_a_repo_false_otherwise(repo: Path, tmp_path: Path) -> None:
    adapter = GitAdapter()
    assert adapter.detect(repo)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert not adapter.detect(plain)


def test_extract_on_a_non_repository_reports_an_error_not_a_crash(
    tmp_path: Path, system_id: str
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = GitAdapter().extract(
        plain, system_id=system_id, since_revision=None, until_revision=None
    )
    assert result.errors
    assert result.observations == []
    assert result.events == []


def test_full_history_produces_one_commit_observation_and_event_each(
    repo: Path, system_id: str
) -> None:
    _write(repo, "a.py", "x = 1\n")
    _commit(repo, "first")
    _write(repo, "a.py", "x = 2\n")
    _commit(repo, "second")

    result = GitAdapter().extract(
        repo, system_id=system_id, since_revision=None, until_revision=None
    )
    commit_obs = [o for o in result.observations if o.kind == "git.commit"]
    assert len(commit_obs) == 2
    assert len(result.events) == 2
    assert [e.type for e in result.events] == [EventType.COMMIT_CREATED, EventType.COMMIT_CREATED]
    assert all(o.origin is Origin.GIT for o in commit_obs)
    assert commit_obs[0].payload["message"] == "first"
    assert commit_obs[1].payload["message"] == "second"


def test_commit_events_carry_the_real_author_and_are_idempotently_keyed(
    repo: Path, system_id: str
) -> None:
    _write(repo, "a.py", "x = 1\n")
    sha = _commit(repo, "first")

    result = GitAdapter().extract(
        repo, system_id=system_id, since_revision=None, until_revision=None
    )
    event = result.events[0]
    assert event.actor.name == "Test"
    assert event.actor.id == "test@example.com"
    assert event.source.provider == "git"
    assert event.source.reference == sha
    # Re-extracting the same range must derive the same dedupe key, so an
    # EventStore can safely no-op a replay rather than forking history.
    result2 = GitAdapter().extract(
        repo, system_id=system_id, since_revision=None, until_revision=None
    )
    assert result.events[0].dedupe_key == result2.events[0].dedupe_key


def test_no_since_revision_skips_file_change_detection(repo: Path, system_id: str) -> None:
    """Nothing was indexed before, so there is nothing to diff against or
    link to -- a first-ever index reports commits only."""
    _write(repo, "a.py", "x = 1\n")
    _commit(repo, "first")

    result = GitAdapter().extract(
        repo, system_id=system_id, since_revision=None, until_revision=None
    )
    assert [o for o in result.observations if o.kind == "git.file_change"] == []


def test_rename_between_revisions_is_reported_as_a_file_change_observation(
    repo: Path, system_id: str
) -> None:
    _write(repo, "payments.py", "class PaymentService:\n    pass\n")
    first = _commit(repo, "first")
    _git(repo, "mv", "payments.py", "billing.py")
    second = _commit(repo, "rename")

    result = GitAdapter().extract(
        repo, system_id=system_id, since_revision=first, until_revision=second
    )
    file_changes = [o for o in result.observations if o.kind == "git.file_change"]
    assert len(file_changes) == 1
    payload = file_changes[0].payload
    assert payload["status"] == "RENAMED"
    assert payload["old_path"] == "payments.py"
    assert payload["path"] == "billing.py"
    assert payload["similarity"] == 1.0
    assert payload["from_revision"] == first
    assert payload["to_revision"] == second
    assert file_changes[0].origin is Origin.GIT
    assert file_changes[0].evidence_ids


def test_same_since_and_until_skips_file_change_detection(repo: Path, system_id: str) -> None:
    _write(repo, "a.py", "x = 1\n")
    sha = _commit(repo, "first")
    result = GitAdapter().extract(repo, system_id=system_id, since_revision=sha, until_revision=sha)
    assert [o for o in result.observations if o.kind == "git.file_change"] == []
