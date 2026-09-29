"""The error-code table (revision 6): one loaded build's own code -> name map.

There is no table in this package, and there must never be one. The table is a
property of the BUILD: codes join and leave between ClickHouse lines, and one
number can name two different errors on two lines (903 is LICENSE_EXPIRED on
25.3 and 25.8, absent on 25.10, and DISTRIBUTED_CACHE_REGISTRY_SHUTDOWN on 26.2
through 26.9). Every answer
therefore comes from `Library.error_codes()`, i.e. from `chs_error_codes` of
the library being asked, and `scripts/check-no-error-code-table.py` fails the
build if a literal code -> name table appears in any binding.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from .errors import ChtypesError

__all__ = ["ErrorCodeEntry", "ErrorCodeTable"]


@dataclass(frozen=True, slots=True)
class ErrorCodeEntry:
    """One row of a build's error-code table: a ClickHouse error code and the
    name THAT BUILD gives it."""

    code: int
    name: str


class ErrorCodeTable:
    """One loaded library's own error-code table, from `chs_error_codes` —
    obtained from `Library.error_codes()` and valid for that library's
    ClickHouse line only. Immutable, so safe to share between threads.

    Lookups answer only what the build's own table holds: an unknown code, a
    negative code (the ABI's -1 and -2 sentinels included — they are not
    ClickHouse codes) or an unknown name is `None`, never a synthesized
    spelling. Names match exactly and case-sensitively, as the server prints
    them. Iterating yields the entries in ascending code order, as `all()`
    returns them.
    """

    __slots__ = ("_by_code", "_by_name", "_entries")

    def __init__(self, entries: tuple[ErrorCodeEntry, ...]) -> None:
        by_code: dict[int, str] = {}
        by_name: dict[str, int] = {}
        kept: list[ErrorCodeEntry] = []
        for e in entries:
            # Not a name, not a ClickHouse code, or a repeat: the first entry
            # for a code or a name wins, and nothing else is invented.
            if not e.name or e.code < 0 or e.code in by_code or e.name in by_name:
                continue
            by_code[e.code] = e.name
            by_name[e.name] = e.code
            kept.append(e)
        self._entries = tuple(sorted(kept, key=lambda e: e.code))
        self._by_code = by_code
        self._by_name = by_name

    def name(self, code: int) -> str | None:
        """The name this build gives `code`, or None."""
        if code < 0:
            return None
        return self._by_code.get(code)

    def code(self, name: str) -> int | None:
        """The code this build gives `name` — an exact, case-sensitive match —
        or None."""
        return self._by_name.get(name)

    def all(self) -> tuple[ErrorCodeEntry, ...]:
        """Every entry, in ascending code order."""
        return self._entries

    def __iter__(self) -> Iterator[ErrorCodeEntry]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"<chtypes.ErrorCodeTable {len(self._entries)} codes>"


def parse_error_codes_document(raw: bytes) -> ErrorCodeTable:
    """Build a table from a `chs_error_codes` document,
    `{"error_codes":[{"code":N,"name":"…"}, …]}`.

    Unknown keys are ignored and an absent (or null) key is its default, like
    every document this ABI hands back. A value of the wrong type is a bad
    document, never a guess.
    """
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise ChtypesError(f"chtypes: bad chs_error_codes document: {exc}") from exc
    if not isinstance(doc, dict):
        raise ChtypesError("chtypes: bad chs_error_codes document: not a JSON object")
    rows = doc.get("error_codes")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise ChtypesError("chtypes: bad chs_error_codes document: error_codes is not an array")
    entries: list[ErrorCodeEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ChtypesError("chtypes: bad chs_error_codes document: an entry is not an object")
        code = row.get("code")
        name = row.get("name")
        if code is None:
            code = 0
        if name is None:
            name = ""
        if isinstance(code, bool) or not isinstance(code, int) or not isinstance(name, str):
            raise ChtypesError(f"chtypes: bad chs_error_codes document: entry {row!r}")
        entries.append(ErrorCodeEntry(code=code, name=name))
    return ErrorCodeTable(tuple(entries))


class _ErrorCodeCache:
    """One library's table once it has been built, and only then.

    A NULL answer from `chs_error_codes` is a guarded exception inside the
    library — transient by definition — so it raises `ChtypesError` and is NOT
    remembered: the next call asks again. A missing symbol raises the decline
    type and is not remembered either. Only a table that was actually built is
    kept.
    """

    __slots__ = ("_mu", "_table")

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._table: ErrorCodeTable | None = None

    def get(self, fetch: Callable[[], bytes | None]) -> ErrorCodeTable:
        with self._mu:
            if self._table is not None:
                return self._table
            raw = fetch()
            if raw is None:
                raise ChtypesError(
                    "chtypes: chs_error_codes returned no document (a guarded exception "
                    "inside the library); nothing was cached, so the next call asks again"
                )
            table = parse_error_codes_document(raw)
            self._table = table
            return table
