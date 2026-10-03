"""Goldens revision selection (docs/guides/fetch-v1.md §9).

The registry is append-only, so a corrected goldens set for the same build
is published as a SECOND goldens referrer of the same platform manifest; the
first can never be deleted. The artifact producer therefore puts an integer
`revision` in the goldens predicate and never reuses one for a different
document. `_ensure.fetch_signed`, called with the goldens predicate type and
a platform manifest digest, verifies every goldens referrer and returns the
one with the highest revision; this module is the pure half (reading the
revision, choosing among verified candidates) so it is unit-testable with
no registry.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from chtypes._ocifetch._errors import ArtifactCorruptError

__all__ = ["MAX_GOLDENS_REVISION", "GoldensCandidate", "select_goldens", "statement_revision"]

# The largest revision every binding accepts: the largest integer a
# JavaScript number holds exactly (2**53 - 1).
MAX_GOLDENS_REVISION = 2**53 - 1

_CANONICAL_INT = re.compile(r"^(0|[1-9][0-9]*)$")


class _RawInt(int):
    """An int that remembers the exact text it was parsed from."""

    raw: str


class _RawFloat(float):
    """Any JSON number with a fraction or an exponent: never an integer here."""


def _parse_int(text: str) -> int:
    value = _RawInt(text)
    value.raw = text
    return value


@dataclass(frozen=True)
class GoldensCandidate:
    """One goldens referrer whose own signature verified."""

    manifest: str  # the goldens referrer manifest's digest
    blob: str  # its layer digest: the signed goldens document
    revision: int
    path: str  # the verified blob on disk (a scratch file until selected)
    predicate: dict
    signed_by: str
    bundle: str  # the bundle blob's digest


def statement_revision(payload: bytes) -> int:
    """The integer `revision` of a verified statement's predicate, read from
    the signed bytes. It must be a JSON integer written without a fraction,
    an exponent, a sign or a leading zero: a missing key, a boolean, a
    string, null, a float such as 1.0 and anything above
    `MAX_GOLDENS_REVISION` raise `ArtifactCorruptError`."""
    try:
        doc = json.loads(payload.decode("utf-8"), parse_int=_parse_int, parse_float=_RawFloat)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ArtifactCorruptError(f"a signed statement was not valid JSON: {e}") from e
    predicate = doc.get("predicate") if isinstance(doc, dict) else None
    if not isinstance(predicate, dict) or "revision" not in predicate:
        raise ArtifactCorruptError('a goldens predicate carries no "revision"')
    value = predicate["revision"]
    if type(value) is not _RawInt or not _CANONICAL_INT.match(value.raw):
        raise ArtifactCorruptError(
            f'a goldens predicate "revision" is {json.dumps(value)}, '
            "not a non-negative JSON integer"
        )
    if int(value) > MAX_GOLDENS_REVISION:
        raise ArtifactCorruptError(
            f'a goldens predicate "revision" {value.raw} is larger than {MAX_GOLDENS_REVISION}'
        )
    return int(value)


def select_goldens(candidates: Sequence[GoldensCandidate]) -> GoldensCandidate:
    """The verified candidate with the highest revision. Two or more at that
    revision with DIFFERENT blob digests are a tie, refused as
    `ArtifactCorruptError` naming both digests and the revision; the same blob
    digest under the same revision (two bundles, say) is one document, not a
    tie. The earliest-listed candidate of the winning document is returned."""
    if not candidates:
        raise ValueError("select_goldens needs at least one candidate")
    best = candidates[0]
    for c in candidates[1:]:
        if c.revision > best.revision:
            best = c
    for c in candidates:
        if c.revision == best.revision and c.blob != best.blob:
            raise ArtifactCorruptError(
                f"goldens revision {best.revision} is carried by two different documents, "
                f"{best.blob} and {c.blob}"
            )
    return best
