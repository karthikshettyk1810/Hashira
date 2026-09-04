"""`PythonAdapter`'s own top-level capabilities -- distinct from
`test_python_extractor.py`'s (what it actually parses out of source)."""

from __future__ import annotations

from hashira.adapters.python import PythonAdapter
from hashira.ports.adapters import LimitationKind, LimitationScope


def test_capabilities_state_dynamic_dispatch_as_a_known_limitation() -> None:
    """A call whose target is resolved at runtime (`getattr(obj,
    method_name)(...)`) is a real, undisclosed gap a live agent experiment
    found twice (`docs/IR.md`'s "field-access coverage audit" entry) --
    declared here, unconditionally, rather than left silent. `PythonAdapter`
    is the right owner: the gap applies to any call, not only ones
    touching a mapped field."""
    limitations = PythonAdapter().capabilities().known_limitations
    assert any(
        limitation.kind is LimitationKind.DYNAMIC_DISPATCH
        and limitation.scope is LimitationScope.CALL_RESOLUTION
        for limitation in limitations
    )
