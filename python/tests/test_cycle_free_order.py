"""Unit coverage for #381: a `Filter`/`Block` held in a reference cycle with
its `Schema` must still free its handle BEFORE the schema's — the C contract
requires children first, and freeing the schema under a live filter or block
handle is a use-after-free (the C ABI contract §Filters/§Blocks, handle
lifetime).

A fake native records the ORDER `chs_*_free` calls land in — never their
timing, which is the whole point: these cases assert on the recorded log,
not on when anything happens to run.
"""

from __future__ import annotations

import gc

import chtypes
from chtypes.registry import Filter, Schema
from chtypes.results import Format


class _RecordingNative:
    """Stands in for `NativeLibrary`: just enough of its surface for
    `Schema.__init__`, `compile_filter` and `parse_block` to reach, with
    every `chs_*_free` call appended to `freed` in call order."""

    def __init__(self) -> None:
        self.freed: list[str] = []
        self._next_handle = 100

    def schema_columns(self, handle: int) -> list[tuple[str, str, str, str, bool]]:
        return []

    def filter_compile(self, handle: int, expr: str, settings_json: str) -> tuple[int, int, str]:
        self._next_handle += 1
        return self._next_handle, 0, ""

    def block_parse(
        self,
        handle: int,
        fmt: int,
        body: bytes,
        settings_json: str,
        columns_json: str | None,
    ) -> tuple[int, int, str]:
        self._next_handle += 1
        return self._next_handle, 0, ""

    def filter_free(self, handle: int) -> None:
        self.freed.append("filter")

    def block_free(self, handle: int) -> None:
        self.freed.append("block")

    def schema_free(self, handle: int) -> None:
        self.freed.append("schema")


class _RecordingLibrary:
    """Stands in for `registry.Library`: just `.version` and `._native`, no
    dlopen behind it — the same shape `test_rows_export_with.py`'s
    `_FakeLibrary` uses."""

    def __init__(self) -> None:
        self.version = "26.9.1.1"
        self._native = _RecordingNative()


def _new_schema(lib: _RecordingLibrary) -> Schema:
    return Schema(lib, 1, "x UInt8")


def test_plain_drop_frees_children_before_schema() -> None:
    """No cycle: ordinary refcounting frees the filter and block the moment
    each is dropped, well before the schema. The baseline the cycle case
    below must also meet — this was already correct before #381's fix."""
    lib = _RecordingLibrary()
    schema = _new_schema(lib)
    f = schema.compile_filter("1")
    b = schema.parse_block(Format.JSON_EACH_ROW, b"{}")

    del f
    del b
    del schema
    gc.collect()

    assert lib._native.freed == ["filter", "block", "schema"]


def test_schema_itself_in_a_cycle_still_frees_the_open_filter_first() -> None:
    """The SCHEMA sits in a reference cycle (not the child): a self-
    referential list holds it, so only `gc.collect()` can reclaim it. The
    filter is held by nothing but a plain local reference and is freed by
    ordinary refcounting well before the cycle is ever collected."""
    lib = _RecordingLibrary()
    schema = _new_schema(lib)
    f = schema.compile_filter("1")

    cycle: list[object] = []
    cycle.append(cycle)  # a list holding itself is a cycle all on its own
    cycle.append(schema)

    del schema
    del f
    del cycle
    gc.collect()

    assert lib._native.freed == ["filter", "schema"]


def test_child_held_only_through_a_reference_cycle_with_its_schema() -> None:
    """The reproduction: a Filter AND a Block are reachable ONLY through a
    user reference cycle that also holds their Schema — `del`-ing every
    local name leaves all three reachable solely via the self-referential
    `cycle` list, so only `gc.collect()`'s cyclic collector can reclaim any
    of them, all in one pass.

    Must free both children before the schema, each exactly once, no matter
    which object's own finalizer the collector happens to run first.

    MUST FAIL on the pre-#381 design (`Schema` tracking children through a
    `weakref.WeakSet` of the `Filter`/`Block` objects, with `Schema.__del__`
    as the backstop): CPython clears a `WeakSet`'s membership — each
    member's own weakref callback removes it — in its `handle_weakrefs`
    pass, which runs BEFORE any `__del__`/`tp_finalize` call. By the time
    `Schema.__del__` runs, `_filters`/`_blocks` are already empty, so it
    frees the schema handle first. Measured on the pre-fix code: free order
    `['schema', 'filter', 'block']`, every trial.
    """
    lib = _RecordingLibrary()
    schema = _new_schema(lib)
    f = schema.compile_filter("1")
    b = schema.parse_block(Format.JSON_EACH_ROW, b"{}")

    cycle: list[object] = []
    cycle.append(cycle)  # self-referential: a cycle with no other help needed
    cycle.append(schema)
    cycle.append(f)
    cycle.append(b)

    del schema, f, b, cycle
    gc.collect()

    freed = lib._native.freed
    assert freed.count("filter") == 1, f"filter not freed exactly once: {freed!r}"
    assert freed.count("block") == 1, f"block not freed exactly once: {freed!r}"
    assert freed.count("schema") == 1, f"schema not freed exactly once: {freed!r}"
    assert freed.index("schema") > freed.index("filter"), (
        f"schema freed before filter — use-after-free order: {freed!r}"
    )
    assert freed.index("schema") > freed.index("block"), (
        f"schema freed before block — use-after-free order: {freed!r}"
    )


def test_explicit_close_frees_children_before_schema() -> None:
    lib = _RecordingLibrary()
    schema = _new_schema(lib)
    f = schema.compile_filter("1")
    b = schema.parse_block(Format.JSON_EACH_ROW, b"{}")

    schema.close()
    assert lib._native.freed == ["filter", "block", "schema"]

    # Idempotent, in both orders: closing an already-closed schema or an
    # already-closed child does nothing more.
    schema.close()
    f.close()
    b.close()
    assert lib._native.freed == ["filter", "block", "schema"]


def test_explicit_close_child_first_then_schema() -> None:
    lib = _RecordingLibrary()
    schema = _new_schema(lib)
    f = schema.compile_filter("1")
    b = schema.parse_block(Format.JSON_EACH_ROW, b"{}")

    f.close()
    assert lib._native.freed == ["filter"]
    b.close()
    assert lib._native.freed == ["filter", "block"]

    schema.close()
    assert lib._native.freed == ["filter", "block", "schema"]

    # A held child's schema stays usable until the child (or the schema) is
    # explicitly closed — closing the child alone must not touch the schema.
    assert isinstance(f, Filter)
    assert isinstance(schema, chtypes.Schema)
