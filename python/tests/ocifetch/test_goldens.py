"""Goldens revision selection's pure half (`_goldens.py`, docs/guides/fetch-v1.md §9):
reading the integer `revision` from a statement's signed bytes and choosing among
verified candidates. The registry-facing half is the conformance suite's
`goldens-revision-*` cases."""

from __future__ import annotations

import pytest

from chtypes._ocifetch._errors import ArtifactCorruptError
from chtypes._ocifetch._goldens import GoldensCandidate, select_goldens, statement_revision


def _stmt(revision_json: str | None) -> bytes:
    body = '"schema":1' if revision_json is None else f'"revision":{revision_json}'
    return ('{"predicate":{' + body + "}}").encode()


@pytest.mark.parametrize(
    ("literal", "want"),
    [("0", 0), ("1", 1), ("9007199254740991", 9007199254740991)],
)
def test_revision_integers_are_accepted(literal: str, want: int) -> None:
    assert statement_revision(_stmt(literal)) == want


@pytest.mark.parametrize(
    "literal",
    [
        None,  # missing
        "true",
        "false",
        '"2"',
        "null",
        "1.0",
        "1e0",
        "-1",
        "-0",
        "{}",
        "[]",
        "9007199254740992",
    ],
)
def test_revision_non_integers_are_corrupt(literal: str | None) -> None:
    with pytest.raises(ArtifactCorruptError):
        statement_revision(_stmt(literal))


def _cand(manifest: str, blob: str, revision: int, bundle: str = "b") -> GoldensCandidate:
    return GoldensCandidate(
        manifest=manifest,
        blob=blob,
        revision=revision,
        path="/nonexistent",
        predicate={},
        signed_by="k",
        bundle=bundle,
    )


def test_highest_revision_wins_wherever_listed() -> None:
    chosen = select_goldens([_cand("m1", "a", 1), _cand("m2", "b", 2), _cand("m3", "a", 0)])
    assert (chosen.revision, chosen.blob) == (2, "b")


def test_tie_of_different_documents_names_both_digests_and_the_revision() -> None:
    with pytest.raises(ArtifactCorruptError) as exc:
        select_goldens([_cand("m1", "a", 3), _cand("m2", "b", 3)])
    message = str(exc.value)
    assert "a" in message and "b" in message and "revision 3" in message


def test_same_document_under_the_same_revision_is_not_a_tie() -> None:
    chosen = select_goldens(
        [_cand("m1", "a", 2, "b1"), _cand("m1", "a", 2, "b2"), _cand("m2", "b", 1)]
    )
    assert chosen.bundle == "b1"


def test_a_tie_below_the_highest_revision_does_not_matter() -> None:
    chosen = select_goldens([_cand("m1", "a", 1), _cand("m2", "b", 1), _cand("m3", "a", 4)])
    assert chosen.revision == 4
