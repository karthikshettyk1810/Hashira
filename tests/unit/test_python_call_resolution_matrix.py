"""The Python call-resolution gap corpus: every syntactic call shape Hashira
claims to understand -- or explicitly does not, and says so -- enumerated
once, here, as executable documentation. Companion to
`test_python_import_resolution_matrix.py`; together they are Hashira 0.2's
"Resolution Integrity" corpus (see `docs/ROADMAP.md`).

| Pattern                                          | Outcome                        |
|----------------------------------------------------|---------------------------------|
| direct function call, module-local                 | SUPPORTED (MODULE_LOCAL)        |
| imported function call                              | SUPPORTED (IMPORT)              |
| aliased imported function call                      | SUPPORTED (IMPORT via alias)    |
| function-local-imported function call               | SUPPORTED (real bug, fixed)     |
| module-level singleton instance method call         | SUPPORTED (real bug, fixed)     |
| class instance method call (`x = Cls(); x.m()`)     | SUPPORTED (LOCAL_INSTANCE)      |
| `self.method()`                                     | SUPPORTED (SELF)                |
| `self.attr.method()`, constructor-composed          | SUPPORTED (SELF_ATTRIBUTE)      |
| typed parameter method call                         | SUPPORTED (typed parameter)     |
| return-value method call (`x = f(); x.m()`)         | correctly UNRESOLVED, undisclosed |
| constructor call itself (`Cls()`)                   | SUPPORTED                       |
| dynamic dispatch (`getattr(obj, name)()`)           | UNRESOLVED + LIMITATION (DYNAMIC_DISPATCH) |
| call through an unrelated/external object           | correctly UNRESOLVED, no limitation |

The "return-value method call" row is the one gap this table deliberately
does *not* mark as covered: `adapters/sqlalchemy`'s own
`_return_value_instances` resolves this shape for ORM field-access
purposes only (`RETURN_VALUE_PROVENANCE`); the *general* Python `CALLS`
resolver has no equivalent mechanism and no `LimitationKind` disclosing the
gap (`docs/ADAPTERS.md`'s "a third, more fundamental boundary" entry
already named this as open). This table exists partly to keep that honest:
a future widening of the general resolver should flip this one row, not
silently leave it stale.

Individual mechanisms already have focused tests elsewhere
(`test_python_resolve.py`, `test_python_extractor.py`,
`test_python_normalizer.py`); this file holds the *shape of the whole call-
resolution claim* in one place, the way `test_sqlalchemy_coverage_matrix.py`
does for field-access coverage.
"""

from __future__ import annotations

from pathlib import Path

from hashira.adapters.python import PythonAdapter
from hashira.adapters.python.extractor import extract_file
from hashira.adapters.python.normalizer import normalize
from hashira.core.enums import RelationshipType
from hashira.core.ids import IDPrefix, new_id
from hashira.ports.adapters import LimitationKind


def _extract_all(tmp_path: Path, files: dict[str, str], system_id: str) -> list:  # type: ignore[type-arg]
    observations = []
    for relpath, source in files.items():
        path = tmp_path / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    for relpath in files:
        result = extract_file(
            tmp_path / relpath, import_root=tmp_path, system_id=system_id, revision="rev1"
        )
        assert result.errors == []
        observations.extend(result.observations)
    return observations


def _calls(run) -> set[tuple[str, str]]:  # type: ignore[no-untyped-def]
    by_id = {e.id: e.qualified_name for e in run.entities}
    return {
        (by_id[r.source_entity_id], by_id[r.target_entity_id])
        for r in run.relationships
        if r.type is RelationshipType.CALLS
    }


def _unresolved_calls(run) -> list:  # type: ignore[no-untyped-def, type-arg]
    return [o for o in run.unresolved if o.kind == "python.call"]


# --- SUPPORTED: ordinary call shapes ------------------------------------------


def test_supported__direct_module_local_call(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {"app.py": "def helper():\n    pass\n\n\ndef run():\n    helper()\n"},
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert ("app.run", "app.helper") in _calls(run)


def test_supported__imported_function_call(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "lib.py": "def helper():\n    pass\n",
            "app.py": "from lib import helper\n\n\ndef run():\n    helper()\n",
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert ("app.run", "lib.helper") in _calls(run)


def test_supported__aliased_imported_function_call(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "lib.py": "def helper():\n    pass\n",
            "app.py": "from lib import helper as h\n\n\ndef run():\n    h()\n",
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert ("app.run", "lib.helper") in _calls(run)


def test_supported__function_local_imported_call(tmp_path: Path) -> None:
    """The real bug this whole corpus was built from: `from x import y`
    written inside a function body, common for breaking circular imports."""
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "lib.py": "def helper():\n    pass\n",
            "app.py": "def run():\n    from lib import helper\n    helper()\n",
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert ("app.run", "lib.helper") in _calls(run)


def test_supported__module_level_singleton_instance_method_call(tmp_path: Path) -> None:
    """The real bug found while verifying the fix above: `publisher =
    Publisher()` at module scope in one file, called from another."""
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "module_a.py": (
                "class KafkaEventPublisher:\n"
                "    def push_notification(self):\n"
                "        pass\n\n\n"
                "publisher = KafkaEventPublisher()\n"
            ),
            "module_b.py": (
                "from module_a import publisher\n\n\n"
                "def send():\n"
                "    publisher.push_notification()\n"
            ),
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert ("module_b.send", "module_a.KafkaEventPublisher.push_notification") in _calls(run)


def test_supported__class_instance_method_call(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "lib.py": "class Cls:\n    def method(self):\n        pass\n",
            "app.py": "from lib import Cls\n\n\ndef run():\n    x = Cls()\n    x.method()\n",
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    calls = _calls(run)
    assert ("app.run", "lib.Cls") in calls
    assert ("app.run", "lib.Cls.method") in calls


def test_supported__self_method_call(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "app.py": (
                "class Service:\n"
                "    def run(self):\n"
                "        self.helper()\n\n"
                "    def helper(self):\n"
                "        pass\n"
            )
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert ("app.Service.run", "app.Service.helper") in _calls(run)


def test_supported__self_attribute_method_call_constructor_composed(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "notify.py": "class NotificationService:\n    def send(self):\n        pass\n",
            "app.py": (
                "from notify import NotificationService\n\n\n"
                "class IvrService:\n"
                "    def __init__(self):\n"
                "        self._notifier = NotificationService()\n\n"
                "    def handle(self):\n"
                "        self._notifier.send()\n"
            ),
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert ("app.IvrService.handle", "notify.NotificationService.send") in _calls(run)


def test_supported__typed_parameter_method_call(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "lib.py": "class Cls:\n    def method(self):\n        pass\n",
            "app.py": "from lib import Cls\n\n\ndef run(x: Cls):\n    x.method()\n",
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert ("app.run", "lib.Cls.method") in _calls(run)


def test_supported__constructor_call_itself(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "lib.py": "class Cls:\n    pass\n",
            "app.py": "from lib import Cls\n\n\ndef run():\n    Cls()\n",
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert ("app.run", "lib.Cls") in _calls(run)


# --- Correctly unresolved: disclosed limitations ------------------------------


def test_unresolved__dynamic_dispatch_via_getattr(tmp_path: Path) -> None:
    """`getattr(obj, name)(...)` -- the target is resolved at runtime, not
    syntactically. Must never guess a target; must correctly stay
    unresolved. The gap is disclosed structurally, not per-call-site --
    `PythonAdapter.capabilities().known_limitations` declares
    `DYNAMIC_DISPATCH` unconditionally, since it owns `CALLS`-edge
    construction and the gap applies to any call, not only specific ones."""
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {"app.py": "def dispatch(obj, name):\n    getattr(obj, name)()\n"},
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert _calls(run) == set()
    assert len(_unresolved_calls(run)) >= 1

    limitations = PythonAdapter().capabilities().known_limitations
    assert any(lim.kind is LimitationKind.DYNAMIC_DISPATCH for lim in limitations)


def test_unresolved__call_through_an_unrelated_external_object(tmp_path: Path) -> None:
    """An object whose type this run never saw at all (an external SDK's
    client) is a different, deeper gap than any of the SUPPORTED rows
    above -- correctly stays unresolved, with nothing to disclose per call
    site (the structural `DYNAMIC_DISPATCH`/`RAW_SQL` declarations do not
    apply here; this is neither)."""
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "app.py": (
                "import external_sdk\n\n"
                "_client = external_sdk.Client()\n\n\n"
                "def run():\n    _client.charge()\n"
            )
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert _calls(run) == set()


# --- Correctly unresolved: a real, currently undisclosed boundary ------------


def test_unresolved__return_value_method_call_is_a_real_open_gap(tmp_path: Path) -> None:
    """`x = repo.get(); x.method()` -- an annotated return value -- is
    SUPPORTED for SQLAlchemy field-access tracking
    (`_return_value_instances`) but *not* for the general Python `CALLS`
    resolver: no mechanism here resolves a return value's own type at all.
    This test pins that gap so a future widening of the general resolver
    is a deliberate change to this file, not a silent drift."""
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "lib.py": (
                "class Thing:\n    def method(self):\n        pass\n\n\n"
                "class Repo:\n    def get(self) -> Thing:\n        return Thing()\n"
            ),
            "app.py": (
                "from lib import Repo\n\n\n"
                "def run(repo: Repo):\n"
                "    x = repo.get()\n"
                "    x.method()\n"
            ),
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    calls = _calls(run)
    # The call to `repo.get()` itself resolves fine (typed parameter);
    # what stays unresolved is the *result's* own method call.
    assert ("app.run", "lib.Repo.get") in calls
    assert ("app.run", "lib.Thing.method") not in calls
    unresolved_exprs = {o.payload["callee_expr"] for o in _unresolved_calls(run)}
    assert "x.method" in unresolved_exprs
