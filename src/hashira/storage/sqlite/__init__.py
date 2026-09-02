"""The local-first storage backend: one file, zero infrastructure (spec §4, §21)."""

from .database import SqliteDatabase

__all__ = ["SqliteDatabase"]
