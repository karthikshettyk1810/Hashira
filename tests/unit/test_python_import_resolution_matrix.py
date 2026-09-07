"""The Python import-resolution gap corpus: every syntactic import shape
Hashira claims to understand -- or does not -- enumerated once, here, as
executable documentation rather than prose that can drift from the
implementation. Companion to `test_python_call_resolution_matrix.py`;
together they are Hashira 0.2's "Resolution Integrity" corpus (see
`docs/ROADMAP.md`).

| Pattern                                          | Outcome   |
|---------------------------------------------------|-----------|
| module-level `import a.b.c`                       | SUPPORTED |
| aliased module import (`import a.b.c as abc`)      | SUPPORTED |
| `from a.b import c`                                | SUPPORTED |
| aliased `from`-import (`... import c as d`)        | SUPPORTED |
| relative import, same package (`from . import x`)  | SUPPORTED |
| relative import, sibling module (`from .x import y`) | SUPPORTED |
| relative import, parent package (`from .. import x`) | SUPPORTED |
| relative import inside a package's own `__init__.py` | SUPPORTED (real bug, fixed) |
| nested relative import two levels up from `__init__.py` | SUPPORTED |
| function-local `import a.b.c`                      | SUPPORTED (real bug, fixed) |
| function-local `from a.b import c`                  | SUPPORTED (real bug, fixed) |
| function-local aliased import                       | SUPPORTED |
| function-local import shadows a module-level one    | SUPPORTED, correctly scoped |
| same target imported under two different bound names | both SUPPORTED independently |
| re-export through a package `__init__.py`           | SUPPORTED (real bug, fixed) |
| chained re-export (two hops)                        | SUPPORTED (bounded alias chase) |
| wildcard import (`from x import *`)                 | correctly produces no binding |
| import of a target never indexed in this run         | correctly unresolved, never fabricated |

Every SUPPORTED row is checked as a real `IMPORTS` relationship (or, for
function-local/shadowing rows, a real resolved `CALLS`/`LOCAL_INSTANCE`
binding) in the whole-run graph `normalizer.normalize` produces -- not just
a syntactic binding in isolation. `test_python_resolve.py` and
`test_python_extractor.py` already hold focused, single-mechanism tests for
several of these rows; this file exists to hold the *shape of the whole
import-resolution claim* in one place, the way `test_sqlalchemy_coverage_matrix.py`
does for field-access coverage.
"""

from __future__ import annotations

from pathlib import Path

from hashira.adapters.python.extractor import extract_file
from hashira.adapters.python.normalizer import normalize
from hashira.core.enums import RelationshipType
from hashira.core.ids import IDPrefix, new_id


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


def _imports(run) -> set[tuple[str, str]]:  # type: ignore[no-untyped-def]
    by_id = {e.id: e.qualified_name for e in run.entities}
    return {
        (by_id[r.source_entity_id], by_id[r.target_entity_id])
        for r in run.relationships
        if r.type is RelationshipType.IMPORTS
    }


def _calls(run) -> set[tuple[str, str]]:  # type: ignore[no-untyped-def]
    by_id = {e.id: e.qualified_name for e in run.entities}
    return {
        (by_id[r.source_entity_id], by_id[r.target_entity_id])
        for r in run.relationships
        if r.type is RelationshipType.CALLS
    }


_PUBLISHER_SRC = "class KafkaEventPublisher:\n    def push(self):\n        pass\n"


# --- SUPPORTED: ordinary module-level import shapes -------------------------


def test_supported__module_level_import(tmp_path: Path) -> None:
    """`import a.b.c` (unaliased) binds only the top-level name `a` -- CPython's
    own binding rule -- so the `IMPORTS` edge lands on `lib`, not `lib.util`;
    `lib/__init__.py` must exist for `lib` to be a real entity at all."""
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {"lib/__init__.py": "", "lib/util.py": "", "app.py": "import lib.util\n"},
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert ("app", "lib") in _imports(run)


def test_supported__aliased_module_import(tmp_path: Path) -> None:
    """Aliasing changes the binding rule: `import a.b.c as x` binds the
    *full* dotted path to `x`, unlike the bare form above."""
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {"lib/util.py": "", "app.py": "import lib.util as u\n"},
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert ("app", "lib.util") in _imports(run)


def test_supported__from_import(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {"lib/util.py": "def helper():\n    pass\n", "app.py": "from lib.util import helper\n"},
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert ("app", "lib.util.helper") in _imports(run)


def test_supported__aliased_from_import(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "lib/util.py": "def helper():\n    pass\n",
            "app.py": "from lib.util import helper as h\n",
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert ("app", "lib.util.helper") in _imports(run)


# --- SUPPORTED: relative imports, including the real __init__.py bug --------


def test_supported__relative_import_same_package(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "shop/payments.py": "class PaymentService:\n    pass\n",
            "shop/checkout.py": "from . import payments\n",
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert ("shop.checkout", "shop.payments") in _imports(run)


def test_supported__relative_import_sibling_module(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "shop/payments.py": "class PaymentService:\n    pass\n",
            "shop/checkout.py": "from .payments import PaymentService\n",
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert ("shop.checkout", "shop.payments.PaymentService") in _imports(run)


def test_supported__relative_import_parent_package(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "shop/shared.py": "def util():\n    pass\n",
            "shop/sub/checkout.py": "from ..shared import util\n",
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert ("shop.sub.checkout", "shop.shared.util") in _imports(run)


def test_supported__relative_import_inside_a_package_init(tmp_path: Path) -> None:
    """The real bug: `common/kafka/__init__.py`'s own qualified name
    (`common.kafka`) already *is* the package -- a relative import written
    there anchors on itself, not its parent (`resolve.py`'s `_package_of`,
    `is_package_init`)."""
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "common/kafka/publisher.py": "class KafkaEventPublisher:\n    pass\n",
            "common/kafka/__init__.py": "from .publisher import KafkaEventPublisher\n",
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert ("common.kafka", "common.kafka.publisher.KafkaEventPublisher") in _imports(run)


def test_supported__nested_relative_import_two_levels_up_from_package_init(tmp_path: Path) -> None:
    """`from .. import shared` inside `common/kafka/__init__.py` walks one
    package above `common.kafka` *itself* (`common`), not two levels above
    it the way the ordinary-module formula would compute."""
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "common/shared.py": "def util():\n    pass\n",
            "common/kafka/__init__.py": "from .. import shared\n",
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert ("common.kafka", "common.shared") in _imports(run)


# --- SUPPORTED: function-local imports, including shadowing -----------------


def test_supported__function_local_import(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "common/kafka/publisher.py": _PUBLISHER_SRC,
            "common/outbox/service.py": (
                "def enqueue():\n"
                "    import common.kafka.publisher\n"
                "    common.kafka.publisher.KafkaEventPublisher()\n"
            ),
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    expected = ("common.outbox.service.enqueue", "common.kafka.publisher.KafkaEventPublisher")
    assert expected in _calls(run)


def test_supported__function_local_from_import(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "common/kafka/publisher.py": _PUBLISHER_SRC,
            "common/outbox/service.py": (
                "def enqueue():\n"
                "    from common.kafka.publisher import KafkaEventPublisher\n"
                "    KafkaEventPublisher()\n"
            ),
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    expected = ("common.outbox.service.enqueue", "common.kafka.publisher.KafkaEventPublisher")
    assert expected in _calls(run)


def test_supported__function_local_aliased_import(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "common/kafka/publisher.py": _PUBLISHER_SRC,
            "common/outbox/service.py": (
                "def enqueue():\n"
                "    from common.kafka.publisher import KafkaEventPublisher as Publisher\n"
                "    Publisher()\n"
            ),
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    expected = ("common.outbox.service.enqueue", "common.kafka.publisher.KafkaEventPublisher")
    assert expected in _calls(run)


def test_supported__function_local_import_shadows_a_module_level_one(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "legacy.py": "class Publisher:\n    def push(self):\n        pass\n",
            "v2.py": "class Publisher:\n    def push(self):\n        pass\n",
            "app.py": (
                "from legacy import Publisher\n\n\n"
                "def enqueue():\n"
                "    from v2 import Publisher\n"
                "    Publisher()\n"
            ),
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    calls = _calls(run)
    assert ("app.enqueue", "v2.Publisher") in calls
    assert ("app.enqueue", "legacy.Publisher") not in calls


# --- SUPPORTED: multiple bound names, re-exports -----------------------------


def test_supported__same_target_imported_under_two_different_names(tmp_path: Path) -> None:
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "lib.py": "def helper():\n    pass\n",
            "app.py": (
                "from lib import helper as h1\n\n\n"
                "def a():\n    h1()\n"
            ),
            "app2.py": (
                "from lib import helper as h2\n\n\n"
                "def b():\n    h2()\n"
            ),
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    calls = _calls(run)
    assert ("app.a", "lib.helper") in calls
    assert ("app2.b", "lib.helper") in calls


def test_supported__re_export_through_a_package_init(tmp_path: Path) -> None:
    """A real bug found while building this corpus: `pkg/__init__.py` doing
    `from pkg.sub import Thing` re-exports a name it does not itself
    define. `consumer.py`'s `from pkg import Thing` resolves its own
    literal target to `pkg.Thing`, which names no entity -- Stage 1 has no
    way to chase another file's own import statement. Closed by
    `normalizer.py`'s `_resolve_through_aliases`, which chases a chain of
    known import bindings (a deterministic fact, not a guess: `from a
    import b` really does make `a.b` the same object as wherever `b`
    really lives)."""
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "pkg/sub.py": "class Thing:\n    def method(self):\n        pass\n",
            "pkg/__init__.py": "from pkg.sub import Thing\n",
            "consumer.py": (
                "from pkg import Thing\n\n\n"
                "def use():\n    t = Thing()\n    t.method()\n"
            ),
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert ("consumer", "pkg.sub.Thing") in _imports(run)
    calls = _calls(run)
    assert ("consumer.use", "pkg.sub.Thing") in calls
    assert ("consumer.use", "pkg.sub.Thing.method") in calls
    assert run.unresolved == []


def test_supported__chained_re_export_two_hops(tmp_path: Path) -> None:
    """A re-export of a re-export (`a.sub` -> `a/__init__.py` -> `b/__init__.py`
    -> `consumer.py`) -- `_resolve_through_aliases`' bounded, iterative chase
    must follow more than one hop, not just one."""
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {
            "a/sub.py": "class Thing:\n    pass\n",
            "a/__init__.py": "from a.sub import Thing\n",
            "b/__init__.py": "from a import Thing\n",
            "consumer.py": "from b import Thing\n\n\ndef use():\n    Thing()\n",
        },
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert ("consumer", "a.sub.Thing") in _imports(run)
    assert ("consumer.use", "a.sub.Thing") in _calls(run)


# --- Correctly unresolved, never fabricated ----------------------------------


def test_correctly_unresolved__wildcard_import_produces_no_binding(tmp_path: Path) -> None:
    """A wildcard genuinely does not say what it binds without executing
    it -- `bindings_for_import_from` skips it, not guesses at it."""
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {"lib.py": "def helper():\n    pass\n", "app.py": "from lib import *\n"},
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert _imports(run) == set()


def test_correctly_unresolved__target_never_indexed_in_this_run(tmp_path: Path) -> None:
    """`PaymentService` is imported but its defining file was never part of
    this run -- the import must not silently resolve to nothing worth
    reporting, or invent an entity; it must just stay an unresolved
    observation."""
    system_id = new_id(IDPrefix.SYSTEM)
    observations = _extract_all(
        tmp_path,
        {"app.py": "from shop.payments import PaymentService\n"},
        system_id,
    )
    run = normalize(observations, system_id=system_id, revision="rev1")
    assert _imports(run) == set()
    assert len(run.unresolved) == 1
    assert run.unresolved[0].kind == "python.import"
