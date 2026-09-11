"""The result surface: enums, the reason vocabulary, and the typed results.

Field for field the same concepts as the Go reference (`go/chtypes`), spelled
the Python way (docs/reference/bindings.md "The object model": follow the language idiom,
never let the meaning drift).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Final, NamedTuple

__all__ = [
    "COMPILE_DECLARED",
    "DOC_ALL",
    "DOC_DEFAULTS",
    "DOC_TRANSFORMS",
    "DOC_VALUES",
    "EXPORT_NONE",
    "LOSSLESS_REASONS",
    "BatchResult",
    "Column",
    "Computed",
    "DefaultKind",
    "FilterOutcome",
    "FilterResult",
    "FilterRowError",
    "Format",
    "Outcome",
    "Reason",
    "RowResult",
    "Span",
    "Substitution",
    "Transform",
    "Value",
    "Verdict",
]

# Mirrors the C `CHS_COMPILE_DECLARED` (enum chs_compile_mode): every setting
# `compile_ddl(settings=...)` names takes the caller's value, every setting it
# does not name keeps the library's own compile base. The only mode this build
# accepts — any other value is refused loudly (-2), reserved for a future
# COMPLETE-profile mode. The numeric value is part of the ABI: never renumber.
COMPILE_DECLARED: Final[int] = 0


class Format(IntEnum):
    """The `chs_format` codes. The numbers are part of the ABI: never renumber."""

    JSON_EACH_ROW = 0
    CSV = 1
    TSV = 2
    VALUES = 3
    JSON_COMPACT_EACH_ROW = 4
    # ClickHouse's own storage encoding (ISerialization::deserializeBinary): one
    # error code (33) for any framing fault, all-or-nothing batches, and
    # input_format_allow_errors_* never applies.
    ROW_BINARY = 5
    ROW_BINARY_WITH_DEFAULTS = 6
    # 26.x and later; earlier vendored trees answer 73 UNKNOWN_FORMAT, exactly
    # as their servers do.
    ROW_BINARY_WITH_NAMES_AND_TYPES_AND_DEFAULTS = 7
    # Column-oriented, self-describing, and what every ClickHouse client
    # library sends on INSERT. Modeled at the revision `INSERT ... FORMAT
    # Native` uses (0), so there is no BlockInfo prefix and no per-column
    # serialization-kind byte; blocks taken off a live TCP connection carry
    # both and are a different contract (docs/reference/c-abi.md §Native). Requires an
    # artifact built at or after the Native exposure — probe it, do not assume
    # it from the SDK version.
    NATIVE = 8
    # ClickHouse 26.5+. Native's column encoding under a length-prefixed frame
    # that carries NO names and NO types: per block a uint64le column count, a
    # uint64le row count, then per column a uint64le byte size followed by that
    # column's bytes exactly as Native writes them
    # (src/Formats/BuffersReader.h:13-24). The missing names and types are the
    # whole point: Native can reconcile a producer against a consumer, Buffers
    # cannot, so a schema disagreement of EQUAL width is undetectable in band
    # and lands as a silently reinterpreted value. Only a WIDTH disagreement is
    # caught. Earlier vendored trees answer 73 UNKNOWN_FORMAT, exactly as their
    # servers do — probe the artifact, do not assume it from the SDK version.
    BUFFERS = 9


# The C CHS_EXPORT_NONE sentinel (-1): no export requested. `rows()` spells it
# `export=None`; the constant exists for callers that carry the wire value.
EXPORT_NONE: Final[int] = -1

# The CHS_DOC_* bitmask (docs/reference/c-abi.md §Document flags): which document GROUPS
# the per-row documents carry. The verdict channel (batch and per-row
# outcome/code/err, rows_read, rows_skipped, unsupported_settings,
# engine_rows, storage_transforms) is ALWAYS emitted and is not a flag.
# DOC_ALL reproduces the full document byte-for-byte at the C layer; 0 is
# "lean" (verdicts only). A bit outside DOC_ALL is refused loudly by the
# library (the whole call answers unsupported) — flags ride through
# unvalidated. The numeric values are part of the ABI: never renumber.
#
# Cost asymmetry, so callers can reason: DOC_VALUES without DOC_TRANSFORMS
# skips the reference second-parse and the wire round trip C-side — real
# compute saved (ref is null, no wire; detectors 2/3 have nothing to run on,
# the caller's explicit choice). DOC_TRANSFORMS without DOC_VALUES still
# computes both and saves only bytes: cols[] is filtered to the entries a
# change detector could fire on, each with its full field set, so
# `transformed` derives exactly as always.
DOC_VALUES: Final[int] = 0x1
DOC_TRANSFORMS: Final[int] = 0x2
DOC_DEFAULTS: Final[int] = 0x4
DOC_ALL: Final[int] = DOC_VALUES | DOC_TRANSFORMS | DOC_DEFAULTS


class Span(NamedTuple):
    """One row's byte range inside an export payload.

    `payload[off:off+len]` IS that row's complete serialized line, terminating
    newline included, and is itself a valid one-row body in the export format.
    Spans are index-aligned with `BatchResult.rows`; a non-accepted row
    (rejected / skipped / unsupported) carries `(0, 0)`. The concatenation of
    all non-zero spans reproduces the payload exactly — batches merge by byte
    concatenation.
    """

    off: int
    len: int


class Verdict(StrEnum):
    """One row's answer from `Filter.rows`, in the document's own characters.

    Two of the four states are ANSWERS and two are NOT, and the split is
    load-bearing: a caller enforcing visibility MUST fail closed (hide the
    row / fail the request) on ERROR and on DECLINE — collapsing either into
    "false the answer" inverts fail-closed into fail-open under NOT, the
    measured leak class (docs/reference/bindings.md §Revision 3). No read-side
    enforcement may be built on this surface until the WHERE-truth rig gates
    green; until then it is shadow/replay only.
    """

    # The predicate is non-NULL and non-zero for this row.
    TRUE = "t"
    # False OR NULL — SQL's three-valued logic collapsed at the WHERE
    # boundary, computed by the vendored functions.
    FALSE = "f"
    # The predicate THREW on this row's values (e.g. NO_COMMON_TYPE 386 from
    # `s = 257` over String). On a real server a WHERE that throws fails the
    # WHOLE query. NOT an answer; fail closed.
    ERROR = "e"
    # This library declines to answer for this row: unparseable/poisoned
    # under the schema, or the admission envelope tripped. NOT an answer;
    # fail closed. Also the state every unknown verdict character degrades
    # to, mirroring the unknown-outcome rule.
    DECLINE = "d"

    @classmethod
    def of(cls, char: str) -> Verdict:
        """Map a verdict character; anything unrecognized degrades to DECLINE
        (fail closed), never to FALSE (which would be an invented answer)."""
        try:
            return cls(char)
        except ValueError:
            return cls.DECLINE

    @property
    def answered(self) -> bool:
        """Whether this verdict is an ANSWER (true/false) rather than an error
        or a decline. A security-enforcing caller hides the row when False."""
        return self in (Verdict.TRUE, Verdict.FALSE)


class FilterOutcome(StrEnum):
    """The CALL-level verdict of `Filter.rows` — whether evaluation completed
    at all; per-row failures live in the verdicts, not here."""

    # Evaluation completed; `verdicts` has one entry per row.
    OK = "ok"
    # A call-level failure with ClickHouse's own code — an unknown setting
    # name's 115, an unsplittable body's framing error, a binary decode
    # fault. `verdicts` is empty: a malformed body yields no partial answers.
    REJECTED = "rejected"
    # A call-level decline (-2), and the arm every unknown outcome spelling
    # degrades to — never REJECTED (docs/reference/bindings.md §RowResult).
    UNSUPPORTED = "unsupported"

    @classmethod
    def of(cls, text: str) -> FilterOutcome:
        try:
            return cls(text)
        except ValueError:
            return cls.UNSUPPORTED


@dataclass(frozen=True, slots=True)
class FilterRowError:
    """One 'e' or 'd' row, itemized: 0-based row index, the code and message
    verbatim — ClickHouse's own for an ERROR row, this library's decline for a
    DECLINE row."""

    row: int
    code: int
    err: str


@dataclass(frozen=True, slots=True)
class FilterResult:
    """One `Filter.rows` answer."""

    # The call-level verdict. On anything but OK, `verdicts` is empty and
    # `errors` is empty — no partial answers.
    outcome: FilterOutcome
    err_code: int = 0
    err_msg: str = ""
    # Rows the splitter/decoder yielded.
    rows_read: int = 0
    # Non-empty means the answer MUST NOT be scored as agreement.
    unsupported_settings: tuple[str, ...] = ()
    # One entry per row, in input order, index-aligned with the rows the
    # reader consumed — the itemized-skips addressing contract holds across
    # resync tails, so verdicts[i] answers input row i.
    verdicts: tuple[Verdict, ...] = ()
    # Every ERROR and DECLINE row, itemized.
    errors: tuple[FilterRowError, ...] = ()


class Outcome(StrEnum):
    """The verdict on a row or a batch, in the document's own vocabulary."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    # The insert returns rc=0 and every later SELECT fails with code 691. This
    # is an ACCEPTED insert, and reporting it as a rejection is wrong.
    ACCEPTED_POISONED = "accepted_poisoned"
    # This build declines to answer. Never scored as agreement.
    UNSUPPORTED = "unsupported"
    # The row was dropped under input_format_allow_errors_* and the batch
    # continued (2026-08-27): the server's own skip, itemized. err_code /
    # err_msg carry the error IRowInputFormat::generate caught before
    # resyncing, verbatim; `values` is empty — the row is NOT stored. Only
    # ever seen on rows inside a BatchResult, never as a batch verdict.
    SKIPPED = "skipped"

    @classmethod
    def of(cls, text: str) -> Outcome:
        """Map a document's `outcome` string; anything unrecognized degrades
        to UNSUPPORTED.

        A future artifact's new verdict is by definition an answer this
        binding cannot interpret: UNSUPPORTED is the arm that is never scored
        as agreement and sends the caller to the server, while defaulting to
        REJECTED would manufacture an over-reject — the zero-budget failure —
        out of pure vocabulary drift (docs/reference/bindings.md §RowResult, rule added
        2026-08-26; every SDK previously defaulted to REJECTED).
        """
        try:
            return cls(text)
        except ValueError:
            return cls.UNSUPPORTED


class DefaultKind(StrEnum):
    """`chs_schema_column_default_kind`'s five answers."""

    NONE = ""
    DEFAULT = "DEFAULT"
    MATERIALIZED = "MATERIALIZED"
    ALIAS = "ALIAS"
    EPHEMERAL = "EPHEMERAL"

    @classmethod
    def of(cls, text: str) -> DefaultKind:
        """Map a document's `default_kind` string, defaulting to NONE for
        anything unrecognized (a future artifact must degrade, not raise)."""
        try:
            return cls(text.upper() if text else "")
        except ValueError:
            return cls.NONE


class Reason:
    """The stable reason strings. The rigs group on them, so the spellings are fixed.

    Not an enum: `storage_transforms` reasons arrive from the C layer, and an
    unknown reason from a newer artifact must pass through, not raise.
    """

    OVERFLOW_WRAP: Final = "overflow_wrap"
    NULL_TO_DEFAULT: Final = "null_to_default"
    NULL_LOSS: Final = "null_loss"
    DECIMAL_TRUNCATE: Final = "decimal_truncate"
    DATE_CLAMP: Final = "date_clamp"
    DATETIME_WRAP: Final = "datetime_wrap"
    DATE_SHIFT: Final = "date_shift"
    UUID_MANGLE: Final = "uuid_mangle"
    IP_MANGLE: Final = "ip_mangle"
    FLOAT_PRECISION: Final = "float_precision"
    LOSSY_NUMERIC: Final = "lossy_numeric"
    FIXEDSTRING_PAD: Final = "fixedstring_pad"
    EMPTIED: Final = "emptied"
    ELEMENT_CHANGED: Final = "element_changed"
    ENUM_COERCE: Final = "enum_coerce"
    VALUE_CHANGED: Final = "value_changed"
    POISONED: Final = "poisoned"
    DUPLICATE_KEY_DROPPED: Final = "duplicate_key_dropped"
    REFORMAT: Final = "reformat"
    DEFAULT_FILLED: Final = "default_filled"
    ZERO_FILLED: Final = "zero_filled"
    DEFAULT_MATERIALIZED: Final = "default_materialized"
    TTL_EXPIRED: Final = "ttl_expired"
    TTL_COLUMN_EXPIRED: Final = "ttl_column_expired"


# Exactly four reasons change how a value is written, or add one the row never
# carried, without losing information. Everything else is a warning. All of them
# are still reported: a preview must show what the table will actually hold.
LOSSLESS_REASONS: Final[frozenset[str]] = frozenset(
    {
        Reason.REFORMAT,
        Reason.DEFAULT_FILLED,
        Reason.ZERO_FILLED,
        Reason.DEFAULT_MATERIALIZED,
    }
)


@dataclass(frozen=True, slots=True)
class Column:
    """One column of a compiled schema, as ClickHouse canonicalised it.

    Canonicalisation is schema-aware: `x Int64 DEFAULT NULL` compiles to
    `Nullable(Int64)`. Pass `type` through verbatim — the library's spelling
    (`Decimal(18, 4)`, a space after each comma) is the canonical one.
    """

    name: str
    type: str
    default_kind: DefaultKind = DefaultKind.NONE
    default_expr: str = ""
    default_is_literal: bool = False


@dataclass(frozen=True, slots=True)
class Value:
    """One coerced column value, as ClickHouse itself renders it.

    `text` is ClickHouse's own JSON text of the stored value (`0`,
    `"2023-11-14 22:13:20"`, `[1,0,3]`) — the bytes a server would emit for the
    row, which is what makes it directly comparable against a real ClickHouse.
    `null` is true only for a genuine stored null: a poisoned column has
    `null=False` and an empty `text`, because there is no value, not a null one.
    """

    column: str
    text: str
    null: bool
    source: str  # the document's `src`: input | default | default_substituted | absent | ...


@dataclass(frozen=True, slots=True)
class Transform:
    """A silent change: input 256 into UInt8 stored as 0.

    ClickHouse has no notion of "I changed your value" — `readIntText` wraps mod
    2**N, `SerializationDateTime` truncates, `parseUUID` maps every non-hex byte
    to 0xff, and all of it returns success. This is the one part of the product
    ClickHouse does not provide, and it is not optional.
    """

    column: str
    input: str
    stored: str
    reason: str
    row: int = 0  # 0-based index inside the request body

    @property
    def lossy(self) -> bool:
        """Whether information was lost, as opposed to only the spelling changing."""
        return self.reason not in LOSSLESS_REASONS


@dataclass(frozen=True, slots=True)
class Substitution:
    """A volatile DEFAULT this library resolved from its own clock, once per batch.

    THE CALLER MUST SEND EVERY ONE OF THESE AS AN EXPLICIT COLUMN IN THE INSERT.
    That is the mechanism, not a nicety: if the server evaluates the expression
    instead, preview and stored differ always at now64 resolution (2-60 ms apart
    even back to back), because ClickHouse reads the clock once per *block*.
    Emit `text` verbatim — a tick count re-encoded as a JSON float is a hard
    reject (code 27), not a coercion.
    """

    column: str
    expr: str
    text: str


@dataclass(frozen=True, slots=True)
class Computed:
    """One MATERIALIZED column's value: stored at insert, frozen thereafter.

    Deliberately not part of `RowResult.values`, because `SELECT *` does not
    return it and a preview that mixed it in would disagree with what a
    subscriber reading the table sees. ALIAS is never reported at all: an
    `ALTER ... MODIFY COLUMN a ALIAS <expr>` retroactively changes what
    already-inserted rows read back as, so it is a fact about the schema at read
    time, not about the row.
    """

    column: str
    kind: str
    text: str


@dataclass(frozen=True, slots=True)
class RowResult:
    """The outcome of validating and coercing one row."""

    outcome: Outcome
    err_code: int = 0
    err_msg: str = ""
    values: tuple[Value, ...] = ()
    transformed: tuple[Transform, ...] = ()
    unknown_fields: tuple[str, ...] = ()
    # Non-empty means the answer MUST NOT be scored as agreement; such a row is
    # promoted to UNSUPPORTED unless it was already REJECTED.
    unsupported_settings: tuple[str, ...] = ()
    substituted: tuple[Substitution, ...] = ()
    computed: tuple[Computed, ...] = ()

    @property
    def accepted(self) -> bool:
        """Whether the insert would succeed. Poisoned rows ARE accepted inserts."""
        return self.outcome in (Outcome.ACCEPTED, Outcome.ACCEPTED_POISONED)

    @property
    def poisoned(self) -> bool:
        """Whether ClickHouse stored a value it cannot read back (code 691).

        A poisoned row IS an accepted insert; every later SELECT on it fails.
        """
        return self.outcome is Outcome.ACCEPTED_POISONED

    @property
    def lossy_transforms(self) -> tuple[Transform, ...]:
        """Only the transforms where information was lost — the warnings.

        The full `transformed` list also carries the four lossless reasons
        (`LOSSLESS_REASONS`), which a preview must still show.
        """
        return tuple(t for t in self.transformed if t.lossy)

    def value(self, column: str) -> Value:
        """The stored value for one column, by name.

        Raises `KeyError` when the row result carries no such column (a
        rejected row's document has no values at all, and `skipped` columns —
        MATERIALIZED / ALIAS / EPHEMERAL — are never in `values`).
        """
        for v in self.values:
            if v.column == column:
                return v
        raise KeyError(column)


@dataclass(frozen=True, slots=True)
class BatchResult:
    """The outcome of one request body, which may hold many rows.

    `chs_rows` is not `chs_row` in a loop: row separation is format-specific (a
    quoted CSV field can contain a newline) and
    `input_format_allow_errors_num` / `_ratio` decide whether a bad row is
    skipped or aborts the batch. It is also the unit of the volatile-DEFAULT
    clock guarantee: one batch is one clock instant.

    `rows` holds one entry per row the reader consumed, in input order —
    including (2026-08-27) rows dropped under `input_format_allow_errors_*`,
    which appear as `Outcome.SKIPPED` with the server's own caught error and
    no values. Filter on the row outcome when rendering survivors; a SKIPPED
    row is never stored.

    When `engine_rows` is present it — not `rows` — is the stored truth. It can
    be shorter than `rows` (a SummingMergeTree dropping an all-zero row, a
    TTL-expired row) or reordered (the block is sorted by the sorting key before
    the part is written).
    """

    outcome: Outcome
    err_code: int = 0
    err_msg: str = ""
    rows: tuple[RowResult, ...] = ()
    rows_read: int = 0
    rows_skipped: int = 0
    # Every transform in the batch, each tagged with its row index, INCLUDING
    # the storage layer's own verdicts (`ttl_expired`, `ttl_column_expired`). A
    # TTL-expired row is `accepted` per row and *not stored* per batch: reading
    # only `rows` previews a row the table will silently delete.
    transformed: tuple[Transform, ...] = ()
    engine_rows: tuple[str, ...] | None = field(default=None)

    # The export channel (`rows(..., export=...)` only; always None from a
    # plain `rows()` call). The batch's accepted rows, serialized once by the
    # transcription of ClickHouse's own output writer for the requested
    # format, copied out of the C buffer (which is freed before the call
    # returns — no ownership crosses the boundary). Three states, the ABI's
    # own (docs/reference/c-abi.md §Rows):
    #
    #   None   no export was requested, the export was DECLINED
    #          (`export_declined` then names the reason), or a call-level
    #          verdict preempted the export machinery entirely (the batch
    #          `outcome` is then the reason and `export_declined` stays "").
    #   b""    an accepted batch with zero accepted rows — EMITTED-EMPTY,
    #          distinguishable from a decline.
    #   bytes  the exported lines; slice per `spans`.
    payload: bytes | None = field(default=None)
    # Index-aligned with `rows`: spans[i] addresses row i's line inside
    # `payload`, (0, 0) for a non-accepted row. None when no bytes were
    # emitted.
    spans: tuple[Span, ...] | None = field(default=None)
    # The library's reason for withholding requested export bytes (a
    # non-accepted batch, the full-arity guard, a serialization failure,
    # output_format_json_validate_utf8) — "" when bytes were emitted or no
    # export was requested. A decline here is -2-class honesty, never a
    # server verdict.
    export_declined: str = ""

    @property
    def lossy_transforms(self) -> tuple[Transform, ...]:
        """Only the transforms where information was lost, across the whole
        batch — the storage layer's `ttl_expired` / `ttl_column_expired`
        verdicts included, since both are lossy by construction."""
        return tuple(t for t in self.transformed if t.lossy)

    def transforms_for_row(self, row: int) -> tuple[Transform, ...]:
        """Every transform tagged with one 0-based row index — including
        storage transforms, which per-row `rows[i].transformed` cannot carry."""
        return tuple(t for t in self.transformed if t.row == row)
