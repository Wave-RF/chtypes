"""The discovery kit: declare the server's own profile, never ask the customer.

This library NEVER talks to ClickHouse; it ships the queries. At connect time a
caller (a gateway, WaveHouse) runs these three queries against the deployment's
own server with whatever client it already has, feeds the results to the typed
parsers below, and then declares what it learned:

    profile.version  -> Registry.for_version / the artifact to load
    profile.settings -> compile_ddl(settings=...) (compile-time) and per-call
    columns          -> reconstruct_ddl -> compile_ddl

The pattern, in full (spec/bindings.md §Discovery):

    1. run QUERY_SERVER_VERSION, QUERY_CHANGED_SETTINGS once per connection
    2. cache the ServerProfile per deployment/tenant
    3. library = registry.for_version(profile.version)
    4. schema = library.compile_ddl(ddl, settings=profile.settings)
    5. per-call: pass profile.settings (plus any per-INSERT overrides) to rows

Never ask the customer for their settings — ask their server. The queries
return exactly what the server believes, spelled the way the server spells it,
which is what the settings gate (spec/c-abi.md, Settings rule 2) validates
against.

Parse and reconstruction failures raise `ValueError`: they are verdicts about
caller-supplied bytes, not about an artifact or the ABI (the Go reference
returns plain errors here for the same reason).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "QUERY_CHANGED_SETTINGS",
    "QUERY_SERVER_VERSION",
    "QUERY_TABLE_COLUMNS",
    "DiscoveredColumn",
    "ServerProfile",
    "parse_changed_settings_result",
    "parse_columns_result",
    "parse_version_result",
    "reconstruct_ddl",
]

# The canonical discovery queries. Each carries its FORMAT clause so the bytes
# an HTTP client gets back are exactly what the matching parse_* function
# consumes. A native-protocol client that returns typed rows instead can ignore
# the parsers and fill the dataclasses directly. The SQL text is identical
# across every SDK (spec/bindings.md §Discovery).

# QUERY_SERVER_VERSION names the deployment's exact release — the string
# Registry.for_version resolves (minor line or exact patch both work).
# One row: {"version":"25.8.28.1"}.
QUERY_SERVER_VERSION: Final = "SELECT version() AS version FORMAT JSONEachRow"

# QUERY_CHANGED_SETTINGS lists every query setting the deployment runs at a
# NON-default value — the whole declared profile, from the server itself. One
# row per setting: {"name":"flatten_nested","value":"0"}.
# system.settings.value is already a String; the dict feeds
# compile_ddl(settings=...) and per-call settings verbatim.
QUERY_CHANGED_SETTINGS: Final = (
    "SELECT name, value FROM system.settings WHERE changed FORMAT JSONEachRow"
)

# QUERY_TABLE_COLUMNS describes one existing table, in declaration order, with
# everything a column-declaration list needs — INCLUDING default_kind and
# default_expression, without which a reconstructed schema silently loses its
# DEFAULT/MATERIALIZED semantics. Uses ClickHouse's own query parameters: send
# param_db / param_table (HTTP) or bind {db}/{table} (native).
QUERY_TABLE_COLUMNS: Final = (
    "SELECT name, type, default_kind, default_expression, position "
    "FROM system.columns WHERE database = {db:String} AND table = {table:String} "
    "ORDER BY position FORMAT JSONEachRow"
)


@dataclass(frozen=True, slots=True)
class ServerProfile:
    """What discovery learns about one deployment: the exact release and the
    settings it runs changed from defaults. Cache one per deployment (or per
    tenant on bring-your-own-ClickHouse) and declare it."""

    version: str  # e.g. "25.8.28.1" — feed Registry.for_version
    # changed settings — feed compile_ddl(settings=...) / per-call
    settings: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiscoveredColumn:
    """One row of QUERY_TABLE_COLUMNS."""

    name: str
    type: str
    default_kind: str = ""  # "" | "DEFAULT" | "MATERIALIZED" | "ALIAS" | "EPHEMERAL"
    default_expression: str = ""
    position: int = 0


def _json_each_row_docs(body: bytes) -> list[bytes]:
    """Split a JSONEachRow body into one raw object per line."""
    out: list[bytes] = []
    for line in bytes(body).split(b"\n"):
        line = line.strip()
        if not line:
            continue
        if not line.startswith(b"{"):
            raise ValueError(f"chtypes: not a JSONEachRow line: {line[:60]!r}")
        out.append(line)
    return out


def _row_of(doc: bytes) -> dict[str, object]:
    """One JSONEachRow line as a dict whose scalars never routed through a float.

    ClickHouse quotes 64-bit integers in JSON output by default
    (`output_format_json_quote_64bit_integers=1`), and a client may unset that —
    so a numeric field can arrive quoted OR bare, and both must survive with
    their digits exact. `parse_int`/`parse_float` keep every bare number as its
    literal text: a 19-digit value can never be rounded on the way in.
    """
    row = json.loads(doc, parse_int=str, parse_float=str)
    if not isinstance(row, dict):  # unreachable behind _json_each_row_docs
        raise ValueError(f"chtypes: not a JSONEachRow object: {doc[:60]!r}")
    return row


def _text(value: object) -> str:
    """A field that may arrive quoted or bare, as its exact text ("" if absent)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def parse_version_result(body: bytes) -> str:
    """Read QUERY_SERVER_VERSION's JSONEachRow body."""
    docs = _json_each_row_docs(body)
    if len(docs) != 1:
        raise ValueError(f"chtypes: version query returned {len(docs)} rows, want 1")
    row = _row_of(docs[0])
    if "version" not in row:
        raise ValueError("chtypes: version query row has no `version` field")
    version = _text(row["version"])
    if not version:
        raise ValueError("chtypes: version query returned an empty version")
    return version


def parse_changed_settings_result(body: bytes) -> dict[str, str]:
    """Read QUERY_CHANGED_SETTINGS' JSONEachRow body.

    An empty body is a stock server: an empty dict, not an error. A duplicated
    `name` resolves LAST-WRITE-WINS — the later row replaces the earlier —
    which is the spec rule (spec/bindings.md §Discovery), asserted by every
    SDK so one server answer can never discover two different profiles.
    """
    out: dict[str, str] = {}
    for doc in _json_each_row_docs(body):
        row = _row_of(doc)
        if "name" not in row:
            raise ValueError(f"chtypes: settings row has no `name` field: {doc[:60]!r}")
        if "value" not in row:
            raise ValueError(f"chtypes: settings row has no `value` field: {doc[:60]!r}")
        out[_text(row["name"])] = _text(row["value"])
    return out


def parse_columns_result(body: bytes) -> list[DiscoveredColumn]:
    """Read QUERY_TABLE_COLUMNS' JSONEachRow body, in the query's ORDER BY
    position order."""
    out: list[DiscoveredColumn] = []
    for doc in _json_each_row_docs(body):
        row = _row_of(doc)
        name = _text(row.get("name"))
        type_ = _text(row.get("type"))
        if not name or not type_:
            raise ValueError(f"chtypes: columns row missing name/type: {doc[:80]!r}")
        position = 0
        raw = row.get("position")
        if raw is not None:
            text = _text(raw)
            # Digits only, exactly — never through a float, and a malformed
            # position degrades to 0 rather than failing the whole description
            # (the reference parser's strconv.ParseUint posture).
            if text.isascii() and text.isdigit():
                position = int(text)
        out.append(
            DiscoveredColumn(
                name=name,
                type=type_,
                default_kind=_text(row.get("default_kind")),
                default_expression=_text(row.get("default_expression")),
                position=position,
            )
        )
    if not out:
        raise ValueError(
            "chtypes: columns query returned no rows — wrong database/table, or no access"
        )
    return out


def _backquote_if_needed(name: str) -> str:
    """Quote an identifier the way ClickHouse DDL requires: plain
    [A-Za-z_][A-Za-z0-9_]* stays bare, anything else is backticked with
    backticks doubled. system.columns can return anything — the flattened
    Nested idiom (`n.a`), spaces, keywords."""
    plain = bool(name) and not ("0" <= name[0] <= "9")
    for c in name:
        if not plain:
            break
        if not (c == "_" or "a" <= c <= "z" or "A" <= c <= "Z" or "0" <= c <= "9"):
            plain = False
    if plain:
        return name
    return "`" + name.replace("`", "``") + "`"


def reconstruct_ddl(columns: Sequence[DiscoveredColumn]) -> str:
    """Turn QUERY_TABLE_COLUMNS' rows back into the column-declaration list
    `compile_ddl` takes. A spelling exercise, not a semantic one: types and
    expressions are the server's own text, passed through verbatim, and the
    library's own compile is the judge of the result.

    Two facts a caller must know, both properties of the server rather than of
    this function:

    - `system.columns` reports the table AS STORED. Under the default
      `flatten_nested=1` a `Nested(a,b)` column appears as its flattened
      `n.a`/`n.b` Array columns and reconstructs to exactly those — which is
      the same table. Under `flatten_nested=0` it appears as one column of
      type `Nested(...)`, which also reconstructs directly. Reconstruction is
      therefore shape-faithful either way, PROVIDED the same `flatten_nested`
      value is declared to `compile_ddl(settings=...)` that the table was
      created under — which is what the discovered `ServerProfile.settings`
      carries.
    - a MATERIALIZED/ALIAS column reconstructs with its expression; an
      EPHEMERAL column may legitimately have an empty default_expression.
    """
    if not columns:
        raise ValueError("chtypes: no columns to reconstruct")
    parts: list[str] = []
    for i, c in enumerate(columns):
        if not c.name or not c.type:
            raise ValueError(f"chtypes: column {i} has no name/type")
        decl = f"{_backquote_if_needed(c.name)} {c.type}"
        kind = c.default_kind
        if kind == "":
            if c.default_expression:
                raise ValueError(
                    f"chtypes: column {c.name} has a default_expression but no default_kind"
                )
        elif kind in ("DEFAULT", "MATERIALIZED", "ALIAS"):
            if not c.default_expression:
                raise ValueError(
                    f"chtypes: column {c.name} is {kind} but has no default_expression"
                )
            decl += f" {kind} {c.default_expression}"
        elif kind == "EPHEMERAL":
            decl += " EPHEMERAL"
            if c.default_expression:
                decl += f" {c.default_expression}"
        else:
            raise ValueError(f"chtypes: column {c.name} has unknown default_kind {kind!r}")
        parts.append(decl)
    return ", ".join(parts)
