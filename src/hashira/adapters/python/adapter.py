"""`PythonAdapter`: the `LanguageAdapter` port, satisfied.

Deliberately thin. Discovery, extraction and cross-file linking already exist
as plain functions (`discovery.py`, `extractor.py`, `normalizer.py`); this
class exists only to be the thing `hashira.ports.LanguageAdapter` expects, and
to advertise what it can do (§38's capability matrix). It knows nothing about
storage, identity resolution, or Django/FastAPI/Celery — see
docs/ARCHITECTURE.md's "Hashira's own stack vs. what Hashira understands".
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from ...core.enums import EntityType, RelationshipType
from ...core.ids import SystemID
from ...ports.adapters import (
    AdapterCapabilities,
    ExtractionResult,
    Limitation,
    LimitationKind,
    LimitationScope,
)
from .discovery import discover_python_files, import_root_for
from .extractor import extract_file

__all__ = ["PythonAdapter"]

_IR_VERSIONS = ["0.1.1"]


class PythonAdapter:
    """Extracts Python source into `Observation`/`Evidence` — nothing else.

    Entities and relationships are deliberately left empty in the returned
    `ExtractionResult`: this adapter reports what it saw (§18), and turning
    that into candidate IR is `normalizer.py`'s job, run by whoever calls this
    adapter (see `hashira.application.indexing`), not this adapter's own.
    """

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            name="python",
            version="0.1.0",
            ir_versions=_IR_VERSIONS,
            languages=["python"],
            frameworks=[],
            entity_types=[EntityType.MODULE, EntityType.SYMBOL],
            relationship_types=[
                RelationshipType.DEFINES,
                RelationshipType.IMPORTS,
                RelationshipType.CALLS,
                RelationshipType.EXTENDS,
            ],
            requires_network=False,
            known_limitations=[
                Limitation(
                    kind=LimitationKind.DYNAMIC_DISPATCH,
                    scope=LimitationScope.CALL_RESOLUTION,
                    detail=(
                        "A call whose target is resolved at runtime (e.g. "
                        "getattr(obj, method_name)(...), a dispatch table) is "
                        "not represented as a CALLS edge -- only a literal "
                        "obj.method(...) call is."
                    ),
                ),
            ],
        )

    def detect(self, root: Path) -> bool:
        """Cheap: does at least one `*.py` file exist? No parsing here (§18)."""
        return next(discover_python_files(root), None) is not None

    def owned_files(self, root: Path) -> Iterable[Path]:
        return discover_python_files(root)

    def extract(
        self, root: Path, files: Sequence[Path], *, system_id: SystemID, revision: str | None
    ) -> ExtractionResult:
        result = ExtractionResult()
        for file in files:
            extracted = extract_file(
                file,
                import_root=import_root_for(file, root),
                project_root=root,
                system_id=system_id,
                revision=revision,
            )
            result.observations.extend(extracted.observations)
            result.evidence.extend(extracted.evidence)
            result.errors.extend(extracted.errors)
        return result
