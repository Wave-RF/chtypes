"""Revision 6's error-code table.

The first half needs no artifact: it parses documents in the shape
`chs_error_codes` returns through the SAME parser `Library.error_codes()` uses,
and drives the success-only cache with a stand-in for the native call. What it
pins is the binding's own contract — unknown is absent, `all()` is ascending,
names match exactly, a NULL answer is never remembered — and none of it is a
ClickHouse fact.

The second half asks a loaded revision-6 library and skips LOUDLY, by name,
without one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import chtypes
from chtypes._error_codes import _ErrorCodeCache, parse_error_codes_document

# Out of order on purpose, with unknown keys at both levels, an entry with no
# name and a negative code — none of which may reach a lookup.
FAKE = b"""{
  "error_codes": [
    {"code": 252, "name": "TOO_MANY_PARTS", "since": "whatever"},
    {"code": 0, "name": "OK"},
    {"code": 1, "name": "UNSUPPORTED_METHOD"},
    {"code": 7, "name": ""},
    {"code": -2, "name": "NOT_A_CLICKHOUSE_CODE"},
    {"code": 47, "name": "UNKNOWN_IDENTIFIER"}
  ],
  "generator": "ignored"
}"""

KNOWN = {0: "OK", 1: "UNSUPPORTED_METHOD", 47: "UNKNOWN_IDENTIFIER", 252: "TOO_MANY_PARTS"}


def test_lookups_answer_the_document_and_nothing_else() -> None:
    table = parse_error_codes_document(FAKE)
    for code, name in KNOWN.items():
        assert table.name(code) == name
        assert table.code(name) == code
    # Absent, never synthesized: an unknown code, the unnamed entry, a
    # negative code even though the document carried one, the ABI sentinels.
    for code in (2, 7, 999_999, -1, -2, -3):
        assert table.name(code) is None, code
    # Exact and case-sensitive; no trimming, no folding.
    for name in (
        "too_many_parts",
        "Too_Many_Parts",
        " TOO_MANY_PARTS",
        "TOO_MANY_PARTS ",
        "",
        "NOT_A_CLICKHOUSE_CODE",
        "NO_SUCH_ERROR",
    ):
        assert table.code(name) is None, name


def test_all_and_iteration_are_ascending() -> None:
    table = parse_error_codes_document(FAKE)
    want = tuple(chtypes.ErrorCodeEntry(code=c, name=n) for c, n in sorted(KNOWN.items()))
    assert table.all() == want
    assert tuple(table) == want
    assert len(table) == len(want)


def test_the_first_entry_wins_on_a_repeat() -> None:
    table = parse_error_codes_document(
        b'{"error_codes":[{"code":5,"name":"A_NAME"},{"code":5,"name":"B_NAME"},'
        b'{"code":6,"name":"A_NAME"}]}'
    )
    assert table.name(5) == "A_NAME"
    assert table.name(6) is None
    assert len(table.all()) == 1


@pytest.mark.parametrize(
    "doc",
    [b"{}", b'{"error_codes":null}', b'{"error_codes":[]}', b'{"something_else":[1,2,3]}'],
)
def test_absent_keys_are_an_empty_table(doc: bytes) -> None:
    table = parse_error_codes_document(doc)
    assert table.all() == ()
    assert table.name(0) is None


@pytest.mark.parametrize(
    "doc",
    # The same list every binding's bad-document test runs: a truncated
    # document, a top-level value that is not an object, error_codes that is not
    # an array, an entry that is not an object, and a field of the wrong type.
    [
        b'{"error_codes":[',
        b"[]",
        b"null",
        b"42",
        b'"x"',
        b'{"error_codes":{}}',
        b'{"error_codes":[null]}',
        b'{"error_codes":[[252,"X"]]}',
        b'{"error_codes":[{"code":"252","name":"X"}]}',
        b'{"error_codes":[{"code":252.5,"name":"X"}]}',
        b'{"error_codes":[{"code":1,"name":5}]}',
    ],
)
def test_a_bad_document_is_the_general_error(doc: bytes) -> None:
    with pytest.raises(chtypes.ChtypesError) as info:
        parse_error_codes_document(doc)
    assert not isinstance(info.value, (chtypes.SchemaError, chtypes.UnsupportedError))


def test_the_cache_keeps_a_built_table_and_nothing_else() -> None:
    cache = _ErrorCodeCache()
    calls: list[str] = []

    def null() -> bytes | None:
        calls.append("null")
        return None

    def missing() -> bytes | None:
        calls.append("missing")
        raise chtypes.UnsupportedError("this artifact predates chs_error_codes (rebuild it)")

    def built() -> bytes | None:
        calls.append("built")
        return FAKE

    # A NULL answer: the general error, not a decline, and not remembered.
    with pytest.raises(chtypes.ChtypesError) as info:
        cache.get(null)
    assert not isinstance(info.value, chtypes.UnsupportedError)
    # A missing symbol: the decline type, not remembered either.
    with pytest.raises(chtypes.UnsupportedError):
        cache.get(missing)
    first = cache.get(built)
    assert first.name(252) == "TOO_MANY_PARTS"

    def never() -> bytes | None:
        raise AssertionError("the cache asked the library again after a table was built")

    assert cache.get(never) is first
    assert calls == ["null", "missing", "built"]


def test_no_package_level_table() -> None:
    """The table hangs off a Library; the package carries none."""
    assert not hasattr(chtypes, "ERROR_CODES")
    assert not hasattr(chtypes, "error_codes")


# ------------------------------------------------------- through the ABI fixture


def test_error_codes_through_the_abi_fixture(isolated_search_path: Path) -> None:
    """`chs_error_codes` through the REAL ctypes path, on the at-revision stub
    the ABI fixture builds from THIS header — the one library that exists
    before any revision-6 artifact does, and the one every pull request's
    abi-fixtures job loads. The stub answers by return type, not with a table,
    so this pins the wiring rather than an answer: the symbol resolves (never
    the decline type, never a refusal), and whatever it answers takes the rule
    — a NULL is the general error and is not kept, a document is kept.

    Nothing here closes the library: reopening after a close is measured to
    segfault (docs/reference/bindings.md §Teardown)."""
    env = os.environ.get("CHTYPES_ABI_FIXTURES")
    if not env:
        pytest.skip(
            "no ABI revision fixture: $CHTYPES_ABI_FIXTURES is unset (build one with "
            "abi-revision/gen.py build --out DIR --header include/chtypes.h)"
        )
    root = Path(env)
    doc = json.loads((root / "fixture.json").read_text())
    library = chtypes.Registry(root / "at-revision").for_version(doc["clickhouse_minor"])
    try:
        first = library.error_codes()
    except (chtypes.UnsupportedError, chtypes.SchemaError) as exc:
        pytest.fail(
            f"error_codes() on a stub built from this header raised {exc!r}; the symbol is "
            f"declared there, so neither a decline nor a refusal is a possible answer"
        )
    except chtypes.ChtypesError as exc:
        assert "returned no document" in str(exc), exc
        # Not kept: the next call asks the library again, and gets NULL again.
        with pytest.raises(chtypes.ChtypesError, match="returned no document"):
            library.error_codes()
    else:
        assert library.error_codes() is first, "a built table was not kept"


# ---------------------------------------------------------------- with artifacts


def _rev6_libraries(registry: chtypes.Registry) -> list[chtypes.Library]:
    libs: list[chtypes.Library] = []
    for version in registry.versions():
        try:
            lib = registry.for_version(version)
        except chtypes.ChtypesError:
            continue
        if lib.abi_revision >= 6:
            libs.append(lib)
    if not libs:
        pytest.skip(
            f"{registry.directory} holds no ABI revision-6 artifact: every case here needs one — "
            f"fetch one with `scripts/fetch.sh` (docs/guides/fetch.md)"
        )
    return libs


def test_error_codes_from_the_loaded_library(registry: chtypes.Registry) -> None:
    for lib in _rev6_libraries(registry):
        table = lib.error_codes()
        entries = table.all()
        assert entries, lib.minor
        codes = [e.code for e in entries]
        assert codes == sorted(set(codes)), f"{lib.minor}: all() is not strictly ascending"
        for e in entries:
            assert table.name(e.code) == e.name
            assert table.code(e.name) == e.code
        assert table.name(252) == "TOO_MANY_PARTS", lib.minor
        assert table.name(-1) is None and table.name(-2) is None
        assert lib.error_codes() is table, f"{lib.minor}: the table was not kept"


def test_code_903_differs_across_lines(registry: chtypes.Registry) -> None:
    checked = 0
    for lib in _rev6_libraries(registry):
        major, minor = (int(x) for x in lib.minor.split("."))
        if lib.minor in ("25.3", "25.8"):
            want = "LICENSE_EXPIRED"
        elif (major, minor) >= (26, 2):
            want = "DISTRIBUTED_CACHE_REGISTRY_SHUTDOWN"
        else:
            continue
        assert lib.error_codes().name(903) == want, lib.minor
        checked += 1
    if not checked:
        pytest.skip("no loaded revision-6 line has a documented expectation for code 903")
