"""`core.revisions.RevisionGraph`: ancestry over a DAG of `Revision` records,
answered by walking `parent_shas` -- no Git call, no timestamp comparison."""

from __future__ import annotations

from hashira.core import Revision, RevisionGraph, System


def _rev(system: System, sha: str, *parents: str) -> Revision:
    return Revision(system_id=system.id, sha=sha, parent_shas=parents)


def test_linear_history_is_transitively_ancestral(system: System) -> None:
    # A -> B -> C (A is B's parent, B is C's parent)
    graph = RevisionGraph([_rev(system, "A"), _rev(system, "B", "A"), _rev(system, "C", "B")])
    assert graph.is_ancestor("A", "C")
    assert graph.is_ancestor("A", "B")
    assert graph.is_ancestor("B", "C")
    assert not graph.is_ancestor("C", "A")
    assert not graph.is_ancestor("B", "A")


def test_is_ancestor_or_self(system: System) -> None:
    graph = RevisionGraph([_rev(system, "A"), _rev(system, "B", "A")])
    assert graph.is_ancestor_or_self("A", "A")
    assert graph.is_ancestor_or_self("A", "B")
    assert not graph.is_ancestor_or_self("B", "A")


def test_a_sha_is_not_its_own_proper_ancestor(system: System) -> None:
    graph = RevisionGraph([_rev(system, "A")])
    assert not graph.is_ancestor("A", "A")


def test_merge_commit_reaches_both_parents(system: System) -> None:
    #     A
    #    / \
    #   B   C
    #    \ /
    #     D  (merge, parents B and C)
    graph = RevisionGraph(
        [
            _rev(system, "A"),
            _rev(system, "B", "A"),
            _rev(system, "C", "A"),
            _rev(system, "D", "B", "C"),
        ]
    )
    assert graph.is_ancestor("A", "D")
    assert graph.is_ancestor("B", "D")
    assert graph.is_ancestor("C", "D")


def test_unrelated_branches_are_not_ancestors_of_each_other(system: System) -> None:
    #   A        X
    #   |        |
    #   B        Y   (two disjoint root histories)
    graph = RevisionGraph(
        [_rev(system, "A"), _rev(system, "B", "A"), _rev(system, "X"), _rev(system, "Y", "X")]
    )
    assert not graph.is_ancestor("A", "Y")
    assert not graph.is_ancestor("X", "B")
    assert not graph.is_ancestor_or_self("A", "Y")


def test_unknown_sha_has_no_ancestors_rather_than_raising(system: System) -> None:
    """A sha with no recorded ancestry (e.g. history predating when Hashira
    started tracking this system) is an absent fact, not an error (§12)."""
    graph = RevisionGraph([_rev(system, "A")])
    assert graph.ancestors("never-recorded") == set()
    assert not graph.is_ancestor("A", "never-recorded")
    assert not graph.is_ancestor("never-recorded", "A")


def test_ancestry_between_excludes_the_since_side(system: System) -> None:
    graph = RevisionGraph(
        [_rev(system, "A"), _rev(system, "B", "A"), _rev(system, "C", "B"), _rev(system, "D", "C")]
    )
    assert set(graph.ancestry_between("B", "D")) == {"C", "D"}
    assert set(graph.ancestry_between(None, "C")) == {"A", "B", "C"}


def test_revision_records_round_trip_parent_shas_as_a_tuple(system: System) -> None:
    revision = Revision(system_id=system.id, sha="deadbeef", parent_shas=["a", "b"])
    assert revision.parent_shas == ("a", "b")
