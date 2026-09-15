"""The ABI-revision handshake, run against a wrong-revision fixture (#36).

`docs/reference/artifact.md` step 5 is normative: an artifact whose
`chs_abi_revision()` answers a value that is neither 0 nor the revision this
binding was written against MUST be refused, **naming both numbers**
(`chtypes/_native.py`, `NativeLibrary.__init__`: "refusing to call through
mismatched declarations"). This suite is the first thing in this repository
that actually RUNS that rule instead of assuming it.

The fixture is a pair of stub shared libraries generated from `include/chtypes.h`
itself, by the served ABI-revision fixture generator (`abi-revision/gen.py`):
one registry whose artifact answers `abi_revision + 1` (`wrong-revision/`), one
answering `abi_revision`
exactly (`at-revision/`), both on ClickHouse minor line "0.0". **Both halves
matter** — without the `at-revision` control, a binding whose own
`ABI_REVISION` had drifted would refuse everything and pass the refusal case
for entirely the wrong reason.

Every expected number comes from `fixture.json` — never from
`chtypes.ABI_REVISION` — because a case that read the constant under test
could not catch that constant being wrong.

`isolated_search_path` (`tests/conftest.py`) is used to keep the per-user
artifact cache and any ambient `$CHTYPES_REGISTRY`/system roots out of the
search path: `Registry(root)` puts `root` first regardless, but this removes
any chance that the fixture's odd "0.0" minor line is ever resolved from
somewhere other than the fixture itself.

Reopening a library after `close()` in the same process is measured to
segfault (`docs/reference/bindings.md` §Teardown), so nothing here closes a
loaded library: the refusal case never gets far enough to open one, the
control case leaves its `Library` for process teardown (`chs_shutdown` runs
via `atexit`), and neither path is ever loaded a second time.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Final

import pytest

import chtypes

#: Where the fixture root lives. Built by the served abi-revision generator
#: (`abi-revision/gen.py build`, core repo); this suite never builds one itself.
ENV_ABI_FIXTURES: Final = "CHTYPES_ABI_FIXTURES"

#: The one ClickHouse minor line every fixture artifact answers to.
FIXTURE_MINOR: Final = "0.0"


@pytest.fixture(scope="session")
def abi_fixture() -> dict:
    """Parse and validate `fixture.json`. Missing `$CHTYPES_ABI_FIXTURES` is a
    SKIP (no fallback build); a fixture that exists but is malformed is a
    FAILURE naming the path — a bad fixture must never read as "nothing to
    test here"."""
    env = os.environ.get(ENV_ABI_FIXTURES)
    if not env:
        pytest.skip(
            f"no ABI revision fixture: ${ENV_ABI_FIXTURES} is unset (build one with "
            "abi-revision/gen.py build --out DIR --header include/chtypes.h)"
        )
    root = Path(env)
    fixture_path = root / "fixture.json"
    try:
        raw = fixture_path.read_text()
    except OSError as exc:
        pytest.fail(f"{fixture_path}: cannot read the ABI revision fixture: {exc}")
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        pytest.fail(f"{fixture_path}: not valid JSON: {exc}")
    abi_revision = doc.get("abi_revision")
    valid = isinstance(abi_revision, int) and not isinstance(abi_revision, bool)
    if not valid or abi_revision <= 0:
        pytest.fail(
            f"{fixture_path}: abi_revision must be a positive integer, got {abi_revision!r}"
        )
    mismatch_revision = doc.get("mismatch_revision")
    if mismatch_revision != abi_revision + 1:
        pytest.fail(
            f"{fixture_path}: mismatch_revision is {mismatch_revision!r}, expected "
            f"abi_revision ({abi_revision}) + 1"
        )
    doc["root"] = str(root)
    return doc


def _names_number(message: str, n: int) -> bool:
    """ "The message says N", not "the message contains the digits of N": a
    refusal naming revision 4 must not be satisfied by the 4 in a path like
    `.../24.8/`, and one naming 5 must not be satisfied by 15."""
    return re.search(rf"(^|[^0-9]){n}([^0-9]|$)", message) is not None


def _without_fixture_path(message: str, root: Path) -> str:
    """Strip every occurrence of the fixture root from a message before
    `_names_number` checks it. The refusal message embeds the artifact's
    PATH, and a path segment can itself hold a standalone digit (a scratchpad
    session id such as `4d9fd784-...`), which would let the path alone
    satisfy "the message names 4" for reasons that have nothing to do with
    the refusal text itself. Both the given root and its resolved form are
    stripped (macOS's `/tmp` vs `/private/tmp` differ), longest first so one
    is never a partial match of the other.
    """
    candidates = sorted({str(root), os.path.realpath(root)}, key=len, reverse=True)
    for candidate in candidates:
        message = message.replace(candidate, "<fixture>")
    return message


def test_wrong_revision_is_refused(
    abi_fixture: dict,
    isolated_search_path: Path,
) -> None:
    root = Path(abi_fixture["root"], "wrong-revision")
    registry = chtypes.Registry(root)
    with pytest.raises(chtypes.RegistryError) as excinfo:
        registry.for_version(FIXTURE_MINOR)
    message = str(excinfo.value)
    stripped = _without_fixture_path(message, root)
    for n in (abi_fixture["mismatch_revision"], abi_fixture["abi_revision"]):
        assert _names_number(stripped, n), f"the refusal does not name {n}: {message!r}"


def test_matching_revision_loads(
    abi_fixture: dict,
    isolated_search_path: Path,
) -> None:
    root = Path(abi_fixture["root"], "at-revision")
    registry = chtypes.Registry(root)
    library = registry.for_version(FIXTURE_MINOR)
    assert library.abi_revision == abi_fixture["abi_revision"]
    assert library.version == abi_fixture["clickhouse_version"]
