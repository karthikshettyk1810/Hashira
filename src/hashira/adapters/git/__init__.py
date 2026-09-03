"""The Git history adapter: commit and rename evidence, no identity decisions.

See `adapter.py`'s module docstring for the boundary between what Git
*reports* and what `hashira.identity` *decides*.
"""

from .adapter import GitAdapter
from .repository import CommitInfo, FileChange, GitRepository, is_git_repository

__all__ = ["CommitInfo", "FileChange", "GitAdapter", "GitRepository", "is_git_repository"]
