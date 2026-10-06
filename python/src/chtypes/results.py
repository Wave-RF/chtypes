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

from dataclasses import dataclass

from ._abi1._vocab import DefaultKind, FilterOutcome, Outcome, Verdict

__all__ = [
    "BatchResult",
    "BuildInfo",
    "Capabilities",
    "Column",
    "Computed",
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
class RowResult:
    """The `row` document. `columns` is every `cols` entry in document order;
    `values` is the subset whose `is_stored` is true, in order."""

    outcome: Outcome
    err_code: int
    err_msg: bytes
    columns: tuple[Value, ...]
    values: tuple[Value, ...]
    transformed: tuple[Transform, ...]
    unknown_fields: tuple[bytes, ...]
    unsupported_settings: tuple[bytes, ...]
    computed: tuple[Computed, ...]
    verdict: Verdict | None
    verdict_code: int
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
    than records while `unconsumed` is empty. A caller that needs every record
    accounted for declines a body with any skipped row or any `unconsumed`
    range, or compares the verdict count with its own count of the body's
    records (docs/guides/batches.md).
    """

    outcome: Outcome
    err_code: int
    err_msg: bytes
    rows: tuple[RowResult, ...]
    rows_read: int
    rows_skipped: int
    transformed: tuple[Transform, ...]
    engine_rows: tuple[tuple[EngineCell, ...], ...] | None
    payload: bytes | None
    spans: tuple[Span, ...] | None
    export_declined: bytes
    rows_passed: int
    rows_cut: int
    partition_count: int | None
    unconsumed: tuple[Span, ...]
    framing: Framing | None


@dataclass(frozen=True, slots=True)
class FilterRowError:
    row: int
    code: int
    msg: bytes


@dataclass(frozen=True, slots=True)
class FilterResult:
    """The `filter_result` document. `e` and `d` are never answers, and a caller
    enforcing visibility fails closed on both; `Verdict.answered` says which."""

    outcome: FilterOutcome
    err_code: int
    err_msg: bytes
    rows_read: int
    unsupported_settings: tuple[bytes, ...]
    verdicts: tuple[Verdict, ...]
    errors: tuple[FilterRowError, ...]


@dataclass(frozen=True, slots=True)
class Column:
    """One column of a compiled schema. `type` is the canonical type; it can
    differ between ClickHouse lines, so a cross-line hash is taken over the
    caller's own statement, never over `columns`."""

    name: bytes
    type: bytes
    default_kind: DefaultKind
    default_expr: bytes


@dataclass(frozen=True, slots=True)
class SchemaDescription:
    columns: tuple[Column, ...]


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
