"""Unit coverage for `_reconstruct_ddl` (`chtypes.discover`) that needs no
loaded artifact — `Library.reconstruct_ddl` wraps it to supply its own
`quote_identifier`.

Reconstruction moved from a free function to a method on a loaded library in
issue #52, because spelling a column name now goes through the artifact. That
took this no-artifact coverage away; it is restored here against
`_reconstruct_ddl` directly, with a marker quoter standing in for the
library's real one.

The marker quoter, `marker()` below, returns something deliberately
un-ClickHouse-like (`<name>`, never a back-quoted spelling) so these tests pin
this module's own logic — the column kinds, the expressions, and the
refusals — and never the rule for how a name is quoted. That rule now lives
behind an artifact (issue #119); pinning any spelling of it here would
re-assert what #52 deleted. Mirrors `rust/src/discover.rs`'s `marker` test
helper and the two tests built on it.
"""

from __future__ import annotations

import pytest

from chtypes.discover import DiscoveredColumn, _reconstruct_ddl


def marker(name: str) -> str:
    """Stand-in for the library's own `quote_identifier`: deliberately NOT a
    ClickHouse spelling, so these cases pin only what this module decides —
    the kinds, the expressions, the refusals."""
    return f"<{name}>"


def test_reconstruct_ddl_spells_kinds_and_leaves_the_name_to_the_quoter() -> None:
    columns = [
        DiscoveredColumn(name="id", type="UInt64"),
        DiscoveredColumn(
            name="ts", type="DateTime", default_kind="DEFAULT", default_expression="now()"
        ),
        DiscoveredColumn(name="n.a", type="Array(Int64)"),
        DiscoveredColumn(name="e", type="UInt8", default_kind="EPHEMERAL"),
        DiscoveredColumn(
            name="m", type="UInt64", default_kind="MATERIALIZED", default_expression="id + 1"
        ),
    ]
    ddl = _reconstruct_ddl(columns, marker)
    # Every name is whatever the quoter answered, verbatim: this function no
    # longer decides which names are spelled how.
    assert "<n.a> Array(Int64)" in ddl, f"the name did not come from the quoter: {ddl}"
    assert "<ts> DateTime DEFAULT now()" in ddl
    assert "<e> UInt8 EPHEMERAL" in ddl
    assert "<m> UInt64 MATERIALIZED id + 1" in ddl


def test_reconstruct_ddl_error_surfaces() -> None:
    # DEFAULT/MATERIALIZED/ALIAS require an expression.
    for kind in ("DEFAULT", "MATERIALIZED", "ALIAS"):
        with pytest.raises(ValueError, match=kind):
            _reconstruct_ddl(
                [DiscoveredColumn(name="x", type="UInt8", default_kind=kind)],
                marker,
            )

    # An unknown kind errors rather than passing through.
    with pytest.raises(ValueError):
        _reconstruct_ddl(
            [DiscoveredColumn(name="x", type="UInt8", default_kind="WEIRD")],
            marker,
        )

    # An expression with no kind errors: it would silently drop semantics.
    with pytest.raises(ValueError):
        _reconstruct_ddl(
            [DiscoveredColumn(name="x", type="UInt8", default_expression="1")],
            marker,
        )

    # No columns is a wrong table, not an empty DDL.
    with pytest.raises(ValueError):
        _reconstruct_ddl([], marker)

    # EPHEMERAL may omit its expression, and may carry one.
    ddl = _reconstruct_ddl(
        [
            DiscoveredColumn(
                name="e", type="UInt8", default_kind="EPHEMERAL", default_expression="7"
            )
        ],
        marker,
    )
    assert ddl == "<e> UInt8 EPHEMERAL 7"
