"""The MCP read surface: `Agent -> MCP -> Application services -> System IR`.

See `server.py`'s module docstring for the architecture rule this package
exists to enforce, and `serialize.py`'s for why every tool's output is
lossless rather than a hand-picked summary.
"""

from __future__ import annotations

from .server import build_server

__all__ = ["build_server"]
