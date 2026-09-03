"""The one place Hashira shells out to `git`. Nothing else may.

Git is the canonical implementation of the thing we're interrogating — its
history, its rename heuristics, its DAG. Reimplementing any of that in Python
would just be a second, worse copy of logic `git` already gets right. So this
module is a thin, controlled boundary around the real binary, not a
reimplementation: one function that runs a command and returns its output,
one exception type for when that fails. Everything above this line
(`repository.py`, `adapter.py`) talks to *that*, never to `subprocess`
directly — so if this ever needs to become a `pygit2` binding or a gRPC call
to some Git service, it changes in one place.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

__all__ = ["GitCommandError", "GitNotFoundError", "run_git"]


class GitNotFoundError(RuntimeError):
    """The `git` executable itself is not on PATH."""


class GitCommandError(RuntimeError):
    """`git` ran and exited non-zero. Carries the real stderr, not a guess."""

    def __init__(self, command: list[str], returncode: int, stderr: str) -> None:
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"git {' '.join(command)} exited {returncode}: {stderr.strip()}")


def run_git(args: list[str], *, cwd: Path, timeout: float = 30.0) -> str:
    """Run one git command and return its stdout, decoded as UTF-8.

    ``core.quotepath=false`` is forced on every call so a non-ASCII filename
    comes back as itself, not a C-style octal escape — one less parsing
    surprise for every caller of this function.
    """
    # Checked explicitly, not left to subprocess: a missing `cwd` and a
    # missing `git` executable both surface as FileNotFoundError from
    # subprocess.run, and conflating "your path doesn't exist" with "git
    # isn't installed" would be a genuinely confusing error to debug.
    if not cwd.is_dir():
        raise NotADirectoryError(f"not a directory: {cwd}")

    full_args = ["git", "-c", "core.quotepath=false", *args]
    try:
        result = subprocess.run(
            full_args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitNotFoundError("the `git` executable was not found on PATH") from exc

    if result.returncode != 0:
        raise GitCommandError(full_args, result.returncode, result.stderr)
    return result.stdout
