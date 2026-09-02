#!/usr/bin/env python
"""Write the System IR JSON Schema to docs/schema/ir-<version>.json.

Run this whenever the contract changes deliberately; the contract test compares
against the committed file so the diff shows up in review.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hashira.core import IR_VERSION, full_schema

OUT = pathlib.Path(__file__).resolve().parents[1] / "docs" / "schema" / f"ir-{IR_VERSION}.json"


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(full_schema(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT.relative_to(pathlib.Path.cwd())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
