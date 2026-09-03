"""Composing more than one enricher's `Normalizer` into one.

`IndexingService` takes exactly one `Normalizer` (`application/indexing.py`),
but already accepts *multiple* `framework_adapters`/`data_adapters` --
the layering diagram this milestone was built from (`PythonAdapter ->
FrameworkAdapter -> DataAdapter -> System IR`) always implied more than one
enricher could be active in the same run. Naively running each enricher's
own `normalize()` independently on the same raw observations and
concatenating the results does not work: every enricher's `normalize()`
calls `adapters.python.normalizer.normalize` itself, and `Entity.id` is a
freshly minted ULID every time an `Entity` is constructed
(`core/ids.py::new_id`) -- so two enrichers would each mint a *different*
id for the same underlying Python class, and `IndexingService`'s
post-resolution relationship reconciliation only dedupes by
`(source_entity_id, target_entity_id, type)` against *storage*, not against
duplicates within one run's own candidate list (`_reconcile_relationships`'s
own docstring). The result would be duplicate relationship rows for
anything both enrichers also happen to derive from plain Python structure.

`compose_normalizers` avoids this the same way `IndexingService` itself
avoids re-deriving language-level structure per framework adapter: run
`normalize_python` exactly once, then hand each enricher's
`enrich_normalized_run` (not its `normalize`) the *previous* enricher's
output in sequence, so every enricher builds on the same entity pool and
mints new entities only for constructs no earlier stage already produced.

This is deliberately generic -- not "FastAPI+SQLAlchemy bridge code". It
takes any sequence of `enrich_normalized_run`-shaped callables and works
for any combination (`tests/integration/test_fastapi_sqlalchemy_together.py`
is what exercises it today; nothing here names FastAPI or SQLAlchemy).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from ..core.evidence import Observation
from ..core.ids import SystemID
from .python.normalizer import NormalizedRun
from .python.normalizer import normalize as normalize_python

__all__ = ["ComposedNormalizer", "EnrichNormalizedRun", "compose_normalizers"]


class EnrichNormalizedRun(Protocol):
    """The shape every enricher's `normalizer.enrich_normalized_run`
    satisfies -- e.g. `adapters.fastapi.normalizer.enrich_normalized_run`."""

    def __call__(
        self,
        python_run: NormalizedRun,
        observations: Sequence[Observation],
        *,
        system_id: SystemID,
        revision: str | None,
    ) -> NormalizedRun: ...


class ComposedNormalizer(Protocol):
    """The shape `application.indexing.Normalizer` expects -- duplicated
    structurally rather than imported, the same way that module's own
    `Normalizer`/`_NormalizedRun` avoid importing anything language- or
    adapter-specific (§18: this package must not depend upward on
    `application`)."""

    def __call__(
        self, observations: Sequence[Observation], *, system_id: SystemID, revision: str | None
    ) -> NormalizedRun: ...


def compose_normalizers(*enrichers: EnrichNormalizedRun) -> ComposedNormalizer:
    """Chain any number of enrichers into one `Normalizer`-shaped callable:
    Python normalizes once, then each enricher layers onto the previous
    one's output, in the order given."""

    def normalize(
        observations: Sequence[Observation], *, system_id: SystemID, revision: str | None
    ) -> NormalizedRun:
        run = normalize_python(observations, system_id=system_id, revision=revision)
        for enrich in enrichers:
            run = enrich(run, observations, system_id=system_id, revision=revision)
        return run

    return normalize
