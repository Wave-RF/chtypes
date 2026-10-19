"""The v1 result types: one-to-one decodes of the library's documents
(docs/reference/bindings-v1.md section 5).

Every field is a field of the document the library returned, or the bytes of an
output buffer. Nothing is computed here. Names, SQL, messages and renderings are
`bytes`, never assumed to be UTF-8; the only text is ASCII the ABI promises
(`ch_name`, build-info fields, setting names, vocabulary values). Where the
vendored reader does not know a fact, the document says `null` and the field is
`None`: unknown is never false and never empty.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ._abi2._vocab import (
    DeclinedLayer,
    DeclinedTier,
    DefaultKind,
    DefaultSupport,
    FilterOutcome,
    MergeReason,
    Outcome,
    Posture,
    RefusalSite,
    Verdict,
)

__all__ = [
    "AtMergeEntry",
    "BatchResult",
    "BuildInfo",
    "Capabilities",
    "Column",
    "Computed",
    "DeclinedSetting",
    "DiscoveredColumn",
    "EngineCell",
    "Discovery",
    "ErrorCodeEntry",
    "ErrorCodeTable",
    "FilterResult",
    "FilterRowError",
    "Framing",
    "Header",
    "RowResult",
    "SchemaDescription",
    "SchemaReplicated",
    "SchemaServer",
    "Span",
    "Transform",
    "Value",
]


@dataclass(frozen=True, slots=True)
class Span:
    """A byte range: `off` and `len`, unsigned 64-bit. In `input_span` it is the
    bytes of the body the reader consumed for a record; in `spans`, an exported
    row's place in `payload`."""

    off: int
    len: int


@dataclass(frozen=True, slots=True)
class Value:
    """One entry of a row's `cols`.

    `column`: the name, bytes (from `name` or `name_b64`). `text`: ClickHouse's
    own rendering of the stored value, bytes (from `stored`). `value`: the raw
    bytes of a scalar String or FixedString leaf (from `value_b64`), None where
    the document carries none. `null`: the library's verdict, poison included.
    `source`: the `value_src` spelling (from `src`); `Source` lists them.
    `is_stored`: the description's own fact for `source`.
    """

    column: bytes
    text: bytes
    null: bool
    source: str
    is_stored: bool
    value: bytes | None = None


@dataclass(frozen=True, slots=True)
class Transform:
    """One change the library made to a value. `reason` is a `transform_reason`
    spelling (`Reason` lists them); `lossy` is the description's fact for it;
    `row` is the row's index in the body, 0 in a single-row result."""

    column: bytes
    input: bytes
    stored: bytes
    reason: str
    lossy: bool
    row: int


@dataclass(frozen=True, slots=True)
class Computed:
    """A computed column's value: `value` is the raw bytes of a scalar String or
    FixedString (from `value_b64`), None otherwise."""

    column: bytes
    kind: str
    text: bytes
    value: bytes | None = None


@dataclass(frozen=True, slots=True)
class EngineCell:
    """One cell of a stored row after the engine's insert-time merge: the column
    name, ClickHouse's rendering, null, and the raw bytes (`value`) of a scalar
    String or FixedString."""

    column: bytes
    text: bytes
    null: bool
    value: bytes | None = None


@dataclass(frozen=True, slots=True)
class AtMergeEntry:
    """One thing an `OPTIMIZE TABLE ... FINAL` of the part the INSERT writes would do
    to one of its rows, at the call's clock instant: an entry of the batch's
    `at_merge` list (ABI v2). `engine_rows` stays exactly what the INSERT writer
    produces, so the two never contradict each other.

    `part_row` indexes `engine_rows`: the part's row, not the input body's. `reason` is
    a `MergeReason`; one the description does not list is its unknown(n) member
    (rule r3). `column` is the column a `ttl_column_reset` resets (from `column`
    or `column_b64`), None when the entry names none. `stored` is the column's
    value after the reset (from `stored` or `stored_b64`), None when the document
    carries none, which it omits when the column's DEFAULT reads the clock or a
    generator and the value is decided at the merge. `input_rows` is the input
    rows, each by its index in the body, that formed the part's row, None when
    the document carries none.
    """

    part_row: int
    reason: MergeReason
    column: bytes | None = None
    stored: bytes | None = None
    input_rows: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class RowResult:
    """The `row` document. `columns` is every `cols` entry in document order;
    `values` is the subset whose `is_stored` is true, in order.

    `err_code` is the document's `code`: ClickHouse's own code, 0 only on a
    success, and -2 (`CHS_CODE_DECLINED`) where this build declined the row.
    `reason` is the decline's catalog id (ASCII), present exactly where `err_code`
    is -2, else None; key on it, never on `err_msg`. `refused_at` is where a
    single-row result's refusal sits (the rows of a batch carry neither this nor
    `refused_row`: the batch's own say where), None for an accepted or skipped
    row. `refused_row` is the row index of a `RefusalSite.ROW` refusal, else None.
    `verdict_reason` is the catalog id of a declined verdict (`verdict_code` is
    -2), else None."""

    outcome: Outcome
    err_code: int
    err_msg: bytes
    reason: str | None
    refused_at: RefusalSite | None
    refused_row: int | None
    columns: tuple[Value, ...]
    values: tuple[Value, ...]
    transformed: tuple[Transform, ...]
    unknown_fields: tuple[bytes, ...]
    computed: tuple[Computed, ...]
    verdict: Verdict | None
    verdict_code: int
    verdict_reason: str | None
    verdict_err: bytes
    partition_id: bytes | None
    input_span: Span | None


@dataclass(frozen=True, slots=True)
class Header:
    """A header the reader consumed: whether it did, how many lines, the names."""

    consumed: bool
    lines: int
    names: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class Framing:
    """What the reader decided about the body's framing. `bom_skipped` and
    `header` are `None` when the vendored reader does not expose the fact
    (TSV, TSVWithNames and Values): unknown, never false and never empty.
    `container` is `array`, `stream`, or None."""

    bom_skipped: bool | None
    container: str | None
    header: Header | None


@dataclass(frozen=True, slots=True)
class BatchResult:
    """The `batch` document and the export buffer.

    `unconsumed` does not account for every record: a skipped row's
    `input_span` can cover more than one input record, so verdicts can be fewer
    than records while `unconsumed` is empty, and an independent record count
    can be fooled too. A caller that needs every record accounted for declines
    a body with any skipped row or any `unconsumed` range
    (docs/guides/batches.md).

    `reason`, `refused_at` and `refused_row` are the row's (see `RowResult`) for the
    batch: the catalog id of a declined batch, and where a refusal or decline
    sits, None when the document carries none. `spans` is `row_spans`: one entry
    per `rows` entry, index-aligned, None for a row not exported (a skipped row, or
    a row a filter cut); the tuple itself is None when absent, and always None when
    the export was declined. `export_columns` is the export's one column list, in
    the order every line carries its columns (None when no export ran), and
    `export_declined_reason` the catalog id of a declined export (None when the
    export ran or none was asked for); `export_declined` stays the cause text.

    `unsupported_settings` is the call's own settings the library declined, from
    the batch's top-level list (ABI v2). `at_merge` is what an `OPTIMIZE TABLE ...
    FINAL` of the part would do to its rows, at least: a later background merge
    can remove more as more rows expire, and a row both removed and reset lists
    only `ttl_delete`. Each is empty when the document carries none.
    """

    outcome: Outcome
    err_code: int
    err_msg: bytes
    reason: str | None
    refused_at: RefusalSite | None
    refused_row: int | None
    rows: tuple[RowResult, ...]
    rows_read: int
    rows_skipped: int
    transformed: tuple[Transform, ...]
    engine_rows: tuple[tuple[EngineCell, ...], ...] | None
    payload: bytes | None
    spans: tuple[Span | None, ...] | None
    export_columns: tuple[bytes, ...] | None
    export_declined: bytes
    export_declined_reason: str | None
    rows_passed: int
    rows_cut: int
    partition_count: int | None
    unconsumed: tuple[Span, ...]
    framing: Framing | None
    unsupported_settings: tuple[bytes, ...] = ()
    at_merge: tuple[AtMergeEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class FilterRowError:
    """One entry of a filter result's `errors`. `reason` is the catalog id where
    `code` is -2 (`CHS_CODE_DECLINED`), else None."""

    row: int
    code: int
    reason: str | None
    msg: bytes


@dataclass(frozen=True, slots=True)
class FilterResult:
    """The `filter_result` document. A verdict counts only when `outcome` is `ok`;
    otherwise treat every verdict as `d`, whatever the verdict string holds.
    `reason`, `refused_at` and `refused_row` are the batch's (see `BatchResult`):
    where a refused or declined evaluation sits, and its catalog id. Under
    `ok`, `e` and `d` are never answers, and a caller enforcing visibility fails
    closed on both; `Verdict.answered` says which."""

    outcome: FilterOutcome
    err_code: int
    err_msg: bytes
    reason: str | None
    refused_at: RefusalSite | None
    refused_row: int | None
    rows_read: int
    unsupported_settings: tuple[bytes, ...]
    verdicts: tuple[Verdict, ...]
    errors: tuple[FilterRowError, ...]


@dataclass(frozen=True, slots=True)
class Column:
    """One column of a compiled schema. `type` is the canonical type; it can
    differ between ClickHouse lines, so a cross-line hash is taken over the
    caller's own statement, never over `columns`.

    `default_support` is what this build does with the column's DEFAULT and
    `default_support_reason` the catalog id behind a `DECLINED` one; `reads` is
    the columns the column's expression reads. All three are reserved: None when
    the build reports none."""

    name: bytes
    type: bytes
    default_kind: DefaultKind
    default_expr: bytes
    default_support: DefaultSupport | None = None
    default_support_reason: str | None = None
    reads: tuple[bytes, ...] | None = None


@dataclass(frozen=True, slots=True)
class DeclinedSetting:
    """One setting the schema compiles under that this build's filters do not
    honor in a WHERE: an entry of the schema description's
    `filter_declined_settings` (ABI v2). `name` is bytes (from `name` or
    `name_b64`). `tier` is the tier this build line's WHERE-settings list gives
    it, a `DeclinedTier`, and `layer` the settings layer it comes from, the
    highest one that sets the name, a `DeclinedLayer`; a value the description
    does not list is its vocabulary's unknown(n) member (rule r3)."""

    name: bytes
    tier: DeclinedTier
    layer: DeclinedLayer


@dataclass(frozen=True, slots=True)
class SchemaServer:
    """The server a schema was compiled on, as the library holds it. Its strings
    are the caller's own profile values given back, so they are plain text, not
    data-derived. `timezone` is the zone the schema's types bind: the profile's,
    or else the image zone. `settings` is the profile's settings (`{}` when it
    gave none). `macros` is None exactly when the profile carried no macros
    (unknown), and a mapping, even an empty one, when it did (the complete set). A
    setting value that was not UTF-8 comes back as its bytes decoded with
    `surrogateescape`, the spelling that goes back in unchanged."""

    timezone: str
    settings: Mapping[str, str]
    macros: Mapping[str, str] | None


@dataclass(frozen=True, slots=True)
class SchemaReplicated:
    """What ClickHouse's own TableZnodeInfo resolved for a Replicated engine on a
    server, fully expanded. Both expand DDL bytes, so both are `bytes`."""

    zookeeper_path: bytes
    replica_name: bytes


@dataclass(frozen=True, slots=True)
class SchemaDescription:
    """A schema's columns, in declared order, the server it was compiled on, and
    the settings its filters decline. `server` is None exactly when it was
    compiled without one; `replicated` is None when the document carries none.
    `filter_declined_settings` is the settings the schema compiles under that
    this build's filters do not honor in a WHERE, over every layer known at
    schema compile (the defaults `setup` set, the server profile and the
    schema's own settings), empty when it lists none: one entry per name, at
    the highest layer that sets it, ordered by layer and then by name bytes, as
    the library wrote it. A schema that lists any is one `compile_filter`
    declines (`UnsupportedError`), and a consumer that streams rows refuses
    such a tenant (spec/abi-v2/where-settings/README.md).

    `posture` is how the statement was compiled, which the document carries only
    for `Posture.LOAD`: None is `Posture.CREATE`."""

    columns: tuple[Column, ...]
    server: SchemaServer | None = None
    replicated: SchemaReplicated | None = None
    filter_declined_settings: tuple[DeclinedSetting, ...] = ()
    posture: Posture | None = None


@dataclass(frozen=True, slots=True)
class DiscoveredColumn:
    name: bytes
    declaration: bytes


@dataclass(frozen=True, slots=True)
class Discovery:
    columns: tuple[DiscoveredColumn, ...]
    columns_sql: bytes


@dataclass(frozen=True, slots=True)
class ErrorCodeEntry:
    code: int
    name: str


class ErrorCodeTable:
    """This build's error-code table, decoded with the stock JSON parser from the
    library's own document. An unknown code or name is absent, never
    synthesized; names match exactly."""

    __slots__ = ("_by_code", "_by_name", "_entries")

    def __init__(self, entries: tuple[ErrorCodeEntry, ...]) -> None:
        self._entries = entries
        self._by_code = {e.code: e.name for e in entries}
        self._by_name = {e.name: e.code for e in entries}

    def name(self, code: int) -> str | None:
        return self._by_code.get(code)

    def code(self, name: str) -> int | None:
        return self._by_name.get(name)

    def all(self) -> tuple[ErrorCodeEntry, ...]:
        return self._entries

    def __len__(self) -> int:
        return len(self._entries)


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a build supports, read from its `build_info`, never discovered by
    calling. Format names are ClickHouse's own (`Format.ch_name`)."""

    input_formats: tuple[str, ...]
    export_formats: tuple[str, ...]
    doc_flags: tuple[str, ...]
    features: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BuildInfo:
    """`chs_build_info`'s document. `raw` is the exact bytes the library returned;
    `toolchain` is the parsed object, uninterpreted."""

    schema: int
    abi: int
    abi_fingerprint: str
    clickhouse_version: str
    channel: str
    clickhouse_minor: str
    clickhouse_commit: str
    core_commit: str
    build: str
    inputs_sha256: str
    os: str
    arch: str
    toolchain: dict
    capabilities: Capabilities
    raw: bytes
