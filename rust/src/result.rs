//! The result types, field for field with `docs/reference/c-abi.md` and
//! `docs/reference/bindings.md`.
//!
//! Every field of a result document is optional at the wire level: a rejected
//! row omits `unknown_fields`, `unsupported_settings` and `computed` entirely
//! and carries `"cols":[]`. Every struct here therefore defaults rather than
//! requiring a key; `crate::doc` is the reader that fills them.
//!
//! # Which fields carry bytes, and which carry text
//!
//! A stored value's rendering is **bytes** ([`RawText`]): a ClickHouse `String`
//! column holds arbitrary bytes, so [`Value::text`], [`Substitution::text`] and
//! [`Computed::text`] are byte-exact and their UTF-8 view is fallible. Those are
//! the values a caller splices back into an INSERT or compares against a real
//! server's output, and an approximation there is indistinguishable from a
//! coercion defect.
//!
//! [`Transform::input`] and [`Transform::stored`] are deliberately `String`:
//! they are a *report* — what to warn a tenant about — and the ABI itself
//! delivers `input` as JSON text. See [`Transform`] for the whole argument and
//! for where the authoritative bytes of the same value live.

use crate::raw::RawText;
use crate::transform::classify;

/// The input encoding of a row, as the `chs_format` integer codes. **The
/// numbers are part of the ABI** and must not be renumbered.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
#[repr(i32)]
pub enum Format {
    /// Name-addressed JSON objects, one per row.
    JsonEachRow = 0,
    /// Positional CSV.
    Csv = 1,
    /// Positional TSV.
    Tsv = 2,
    /// ClickHouse's `Values` literal syntax.
    Values = 3,
    /// Positional JSON arrays, one per row.
    JsonCompactEachRow = 4,
    /// ClickHouse's own storage encoding (`ISerialization::deserializeBinary`),
    /// so every parse-time text guard is bypassed exactly as on a real server:
    /// one error code (33) for any framing fault, all-or-nothing batches, and
    /// `input_format_allow_errors_*` never applies.
    RowBinary = 5,
    /// `RowBinary` plus the per-column marker byte: any nonzero byte means
    /// "compute this column's DEFAULT and read no value bytes".
    RowBinaryWithDefaults = 6,
    /// ClickHouse 26.x and later. Earlier vendored trees answer code 73
    /// `Unknown format`, exactly as their servers do.
    RowBinaryWithNamesAndTypesAndDefaults = 7,
    /// ClickHouse's `Native` block format — COLUMN-oriented, self-describing,
    /// and what every ClickHouse client library sends on INSERT.
    ///
    /// Modeled at the revision `INSERT ... FORMAT Native` uses (0), so there
    /// is no `BlockInfo` prefix and no per-column serialization-kind byte;
    /// blocks taken off a live TCP connection carry both and are a different
    /// contract (`docs/reference/c-abi.md` §Native). Because the stream carries names
    /// and types, the declared schema and the payload can disagree, and
    /// ClickHouse's own resolution is not uniformly an error — a type
    /// mismatch is CAST by default. Requires an artifact built at or after
    /// the Native exposure; probe it rather than assuming it from this
    /// crate's version.
    Native = 8,
    /// ClickHouse 26.5+. `Native`'s column encoding under a length-prefixed
    /// frame that carries NO names and NO types: per block a `uint64le` column
    /// count, a `uint64le` row count, then per column a `uint64le` byte size
    /// followed by that column's bytes exactly as `Native` writes them
    /// (`src/Formats/BuffersReader.h:13-24`).
    ///
    /// The missing names and types are the whole point. `Native` can reconcile
    /// a producer against a consumer; `Buffers` cannot, so a schema
    /// disagreement of EQUAL width is undetectable in band and lands as a
    /// silently reinterpreted value — a `UInt32` producer's 4294967295 stored
    /// as -1 by an `Int32` consumer. Only a WIDTH disagreement is caught.
    /// Earlier vendored trees answer code 73 `Unknown format`, exactly as
    /// their servers do; probe the artifact rather than assuming it from this
    /// crate's version.
    Buffers = 9,
}

impl Format {
    /// The `chs_format` integer this format crosses the C boundary as.
    pub fn code(self) -> i32 {
        self as i32
    }
}

/// Which document GROUPS the per-row documents carry (revision 3;
/// `docs/reference/c-abi.md` §Document flags). The verdict channel — batch and per-row
/// outcome/code/err, `rows_read`, `rows_skipped`, `unsupported_settings`,
/// `engine_rows`, `storage_transforms` — is ALWAYS emitted and is not a flag.
///
/// [`DocFlags::ALL`] is today's full document — what [`crate::Schema::rows`]
/// always requests; [`DocFlags::NONE`] (the default) is "lean": verdicts
/// intact, `values` / `transformed` / `substituted` / `computed` /
/// `unknown_fields` all empty. A bit outside `ALL` is refused loudly by the
/// library (the whole call answers unsupported) — bits are passed through,
/// never pre-validated here.
///
/// The cost asymmetry, so callers can reason: `VALUES` without `TRANSFORMS`
/// skips the reference second-parse and the wire round trip C-side — real
/// compute saved, and the precise detectors have nothing to run on (`ref` is
/// null, no `wire`; the caller's choice, not data loss). `TRANSFORMS` without
/// `VALUES` still computes both and saves only bytes: `cols[]` is filtered to
/// the entries a change detector could fire on, each with its full field set,
/// so [`crate::BatchResult::transformed`] is derived exactly as always.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash)]
pub struct DocFlags(u32);

impl DocFlags {
    /// The lean document: verdicts only.
    pub const NONE: DocFlags = DocFlags(0);
    /// `cols[]` (per-column stored text and value provenance) and
    /// `unknown_fields`.
    pub const VALUES: DocFlags = DocFlags(0x1);
    /// The change-detection channel: the reference second-parse and the TSV
    /// wire round trip run and are emitted; without [`DocFlags::VALUES`],
    /// `cols[]` keeps only the entries a spec'd detector could fire on.
    pub const TRANSFORMS: DocFlags = DocFlags(0x2);
    /// `computed[]` (MATERIALIZED values) and, without [`DocFlags::VALUES`],
    /// the `default_substituted` entries a caller must echo into its INSERT.
    pub const DEFAULTS: DocFlags = DocFlags(0x4);
    /// All three groups — today's full document, byte-for-byte.
    pub const ALL: DocFlags = DocFlags(0x7);

    /// The raw bitmask, as it crosses the C boundary.
    pub fn bits(self) -> u32 {
        self.0
    }

    /// A mask from raw bits, unvalidated on purpose: an unknown bit must
    /// reach the library, which refuses it loudly rather than silently
    /// meaning nothing.
    pub fn from_bits(bits: u32) -> DocFlags {
        DocFlags(bits)
    }
}

impl std::ops::BitOr for DocFlags {
    type Output = DocFlags;
    fn bitor(self, rhs: DocFlags) -> DocFlags {
        DocFlags(self.0 | rhs.0)
    }
}

impl std::ops::BitOrAssign for DocFlags {
    fn bitor_assign(&mut self, rhs: DocFlags) {
        self.0 |= rhs.0;
    }
}

/// One row's byte range inside an export payload:
/// `payload[off..off + len]` IS that row's complete serialized line,
/// terminating `\n` included, and is itself a valid one-row body in the
/// export format. Spans are index-aligned with [`BatchResult::rows`]; a
/// non-accepted row (rejected / skipped / unsupported) carries `{0, 0}`. The
/// concatenation of all non-zero spans reproduces the payload exactly, which
/// is what lets batches merge by byte concatenation.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash)]
pub struct Span {
    /// Byte offset of the row's line inside the payload.
    pub off: usize,
    /// Byte length of the line, its `\n` included. `0` for a non-accepted row.
    pub len: usize,
}

/// One row's answer from [`crate::Filter::rows`]. Two of the four states are
/// ANSWERS and two are NOT, and the split is load-bearing: a caller enforcing
/// visibility MUST fail closed (hide the row / fail the request) on
/// [`Verdict::Error`] and [`Verdict::Decline`] — collapsing either into
/// "false the answer" inverts fail-closed into fail-open under `NOT`, the
/// measured leak class (`docs/reference/bindings.md` §Revision 3). The default is
/// `Decline`, so an unset or unknown verdict is fail-closed by construction.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash)]
pub enum Verdict {
    /// This library declines to answer for this row — unparseable under the
    /// schema, poisoned, or the admission envelope tripped. NOT an answer;
    /// fail closed. Also the state an unknown verdict character degrades to.
    #[default]
    Decline,
    /// The predicate is non-NULL and non-zero for this row.
    True,
    /// False OR NULL — SQL's three-valued logic collapsed at the WHERE
    /// boundary, computed by the vendored functions.
    False,
    /// The predicate THREW on this row's values (e.g. `NO_COMMON_TYPE` 386
    /// from `s = 257` over `String`). On a real server a WHERE that throws
    /// fails the WHOLE query; NOT an answer; fail closed.
    Error,
}

impl Verdict {
    /// Whether this verdict is an ANSWER (true/false) rather than an error or
    /// a decline. A security-enforcing caller hides the row on `!answered()`.
    pub fn answered(self) -> bool {
        matches!(self, Verdict::True | Verdict::False)
    }

    /// The result-document character: `t`, `f`, `e`, `d`.
    pub fn as_char(self) -> char {
        match self {
            Verdict::True => 't',
            Verdict::False => 'f',
            Verdict::Error => 'e',
            Verdict::Decline => 'd',
        }
    }
}

/// The CALL-level verdict of [`crate::Filter::rows`] — whether evaluation
/// completed at all; per-row failures live in the verdicts, not here. The
/// default (and the degradation for an outcome spelling this crate does not
/// recognize) is `Unsupported`, never `Rejected` — the same
/// vocabulary-drift rule as [`Outcome`].
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash)]
pub enum FilterOutcome {
    /// Evaluation completed; there is one verdict per row.
    Ok,
    /// A call-level failure with ClickHouse's own code — an unknown setting
    /// name's 115, an unsplittable body's framing error, a binary decode
    /// fault. Verdicts are empty: a malformed body yields no partial answers.
    Rejected,
    /// A call-level decline (`-2`), and the unknown-spelling degradation.
    #[default]
    Unsupported,
}

impl FilterOutcome {
    /// The wire spelling, as the filter result document uses it — the same
    /// vocabulary [`Outcome::as_str`] answers in, and the one the Python and
    /// TypeScript bindings' `FilterOutcome` values already are. [`Outcome`] had
    /// this and [`FilterOutcome`] did not, so a caller could render every
    /// verdict in the ABI except the filter call's own.
    pub fn as_str(self) -> &'static str {
        match self {
            FilterOutcome::Ok => "ok",
            FilterOutcome::Rejected => "rejected",
            FilterOutcome::Unsupported => "unsupported",
        }
    }
}

impl std::fmt::Display for FilterOutcome {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// One `'e'` or `'d'` row, itemized: the row's 0-based index and the code and
/// message verbatim — ClickHouse's own for an error row, this library's
/// decline for a declined row.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct FilterRowError {
    /// 0-based index into the request body's rows.
    pub row: usize,
    /// The code, verbatim.
    pub code: i32,
    /// The message, verbatim.
    pub err: String,
}

/// One [`crate::Filter::rows`] answer.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct FilterResult {
    /// The call-level verdict. On anything but [`FilterOutcome::Ok`],
    /// `verdicts` and `errors` are empty.
    pub outcome: FilterOutcome,
    /// The call-level code: ClickHouse's own when rejected, `-2` on a
    /// call-level decline, `0` when ok.
    pub err_code: i32,
    /// The call-level message.
    pub err_msg: String,
    /// Rows the splitter/decoder yielded.
    pub rows_read: usize,
    /// As everywhere: non-empty means the answer MUST NOT be scored as
    /// agreement.
    pub unsupported_settings: Vec<String>,
    /// One verdict per row, in input order, index-aligned with the rows the
    /// reader consumed — the itemized-skips addressing contract holds even
    /// across resync tails.
    pub verdicts: Vec<Verdict>,
    /// Every [`Verdict::Error`] and [`Verdict::Decline`] row, itemized.
    pub errors: Vec<FilterRowError>,
}

/// The verdict on one row or one batch.
///
/// `Unsupported` is the default and the unknown-string degradation, and
/// deliberately so (docs/reference/bindings.md §RowResult, 2026-08-26): an outcome this
/// crate cannot interpret must never come back as `Accepted` (an over-accept
/// streams rows to subscribers and then fails the insert) — and must not come
/// back as `Rejected` either, which would manufacture an over-reject (silent
/// data loss) out of vocabulary drift. `Unsupported` is the arm that is never
/// scored as agreement and sends the caller to the server.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Hash)]
pub enum Outcome {
    /// ClickHouse would accept the insert.
    Accepted,
    /// ClickHouse would reject it, with its own error code.
    Rejected,
    /// ClickHouse accepts the insert and the stored value cannot be read back —
    /// every later `SELECT` fails with code 691. This is an **accepted** insert
    /// and must never be reported as a rejection.
    AcceptedPoisoned,
    /// This build refuses to answer. Never scored as agreement, and never
    /// mapped onto accepted or rejected. Also the degradation for an outcome
    /// spelling this crate does not recognize — see the type docs.
    #[default]
    Unsupported,
    /// The row was dropped under `input_format_allow_errors_*` and the batch
    /// continued — the server's own skip, itemized (2026-08-27). `err_code` /
    /// `err_msg` carry the error `IRowInputFormat::generate` caught before
    /// resyncing, verbatim; `values` is empty (the row is NOT stored). Only
    /// ever seen on rows inside a [`BatchResult`], never as a batch verdict
    /// and never from the single-row call. Filter on it when rendering
    /// survivors.
    Skipped,
}

impl Outcome {
    /// The wire spelling, as the result document uses it.
    pub fn as_str(self) -> &'static str {
        match self {
            Outcome::Accepted => "accepted",
            Outcome::Rejected => "rejected",
            Outcome::AcceptedPoisoned => "accepted_poisoned",
            Outcome::Unsupported => "unsupported",
            Outcome::Skipped => "skipped",
        }
    }
}

impl std::fmt::Display for Outcome {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Map a document's outcome string. An outcome this crate does not recognize
/// degrades to [`Outcome::Unsupported`], never to [`Outcome::Rejected`]
/// (docs/reference/bindings.md §RowResult, rule added 2026-08-26): a future artifact's
/// new verdict is an answer this crate cannot interpret, and `Unsupported` is
/// the arm that is never scored as agreement, while a default of `Rejected`
/// would manufacture an over-reject — the zero-budget failure — out of pure
/// vocabulary drift.
fn outcome_of(s: &str) -> Outcome {
    match s {
        "accepted" => Outcome::Accepted,
        "rejected" => Outcome::Rejected,
        "accepted_poisoned" => Outcome::AcceptedPoisoned,
        "unsupported" => Outcome::Unsupported,
        "skipped" => Outcome::Skipped,
        _ => Outcome::Unsupported,
    }
}

/// One coerced column value, as ClickHouse itself renders it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Value {
    /// The column this value belongs to.
    pub column: String,
    /// ClickHouse's own JSON rendering of the stored value (`0`,
    /// `"2023-11-14 22:13:20"`, `[1,0,3]`, `null`) — the same **bytes** a server
    /// would emit for the row, which is what makes it directly comparable
    /// against one. Emit it verbatim; re-encoding a tick count as a JSON float
    /// is a hard reject (code 27), not a coercion.
    ///
    /// It is a [`RawText`] and not a `String` because a `String` column holds
    /// arbitrary bytes: [`RawText::as_bytes`] is authoritative and
    /// [`RawText::as_str`] answers `None` when the value is not valid UTF-8,
    /// rather than handing back a U+FFFD-repaired approximation that reads
    /// downstream as a coercion that never happened.
    ///
    /// Empty means **no value**: a poisoned column, or a column the document
    /// carried no `stored` key for. A stored null renders as `null` — see
    /// [`null`](Self::null).
    pub text: RawText,
    /// True only for a genuine stored null, in which case
    /// [`text`](Self::text) is the four bytes `null`. A poisoned column is
    /// `null: false` with an empty `text`, because there is no value, not a null
    /// one.
    pub null: bool,
    /// The `src` string from the result document: `input`, `default`,
    /// `default_substituted`, `absent`, `default_volatile_unresolved`,
    /// `default_pending`, `default_expr_unsupported`. (`skipped` columns are
    /// dropped from the stored row.)
    pub source: String,
}

/// One silent change: input `256` into `UInt8` stored as `0`.
///
/// # Why these two fields are text and not bytes
///
/// A `Transform` is a **report**: it exists so a gateway can warn a tenant
/// before a row ships to subscribers. Its two value fields are therefore
/// `String`, carrying U+FFFD where a byte was not valid UTF-8:
///
/// * `input` is text by the ABI's own definition — `docs/reference/c-abi.md` gives it as
///   "the raw input **text** for this field, as a string" — and the reference
///   SDK's JSON decoder repairs it identically, so the two SDKs report the same
///   thing.
/// * `stored` is the same value that [`RowResult::values`] carries **byte-exact**
///   for the same column. Read [`Value::text`] when the bytes must round-trip;
///   read this when reporting the change to a human.
///
/// Nothing here is the authoritative copy of a value, which is why a lossy view
/// is honest rather than a repeat of the defect it replaced.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Transform {
    /// The column that changed. Empty for a row-level transform such as
    /// `ttl_expired`.
    pub column: String,
    /// The raw input text for the field, as the row supplied it. Empty for a
    /// column the row never sent, and for `RowBinary` (there is no text).
    pub input: String,
    /// ClickHouse's rendering of what it kept, or `<unreadable>` when the value
    /// was poisoned. A stored null renders as `null`.
    pub stored: String,
    /// One of the stable reason strings — the harness groups on them, so the
    /// spellings are fixed. See [`crate::reason`].
    pub reason: String,
    /// The 0-based row index inside the request body. A batch of ten rows with
    /// one bad value is useless to a tenant without it.
    pub row: usize,
}

impl Transform {
    /// Whether information was lost, as opposed to only the way the value is
    /// written having changed (`1700000000` -> `"2023-11-14 22:13:20"`).
    ///
    /// False for exactly four reasons — `reformat`, `default_filled`,
    /// `zero_filled`, `default_materialized` — and true for everything else.
    /// All of them are still reported: a preview must show the tenant what the
    /// table will actually hold. Only the lossy ones are a warning.
    pub fn lossy(&self) -> bool {
        !matches!(
            self.reason.as_str(),
            crate::reason::REFORMAT
                | crate::reason::DEFAULT_FILLED
                | crate::reason::ZERO_FILLED
                | crate::reason::DEFAULT_MATERIALIZED
        )
    }
}

/// One volatile DEFAULT this library resolved instead of the server.
///
/// **The caller MUST send every one of these as an explicit column in the
/// INSERT.** That is the mechanism, not a nicety: if the server evaluates the
/// expression instead, preview and stored differ always at `now64` resolution
/// and sometimes at `now()` resolution, because ClickHouse reads the clock once
/// per block.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Substitution {
    /// The column whose volatile DEFAULT was resolved here.
    pub column: String,
    /// The DEFAULT as ClickHouse canonicalized it.
    pub expr: String,
    /// The value rendered by ClickHouse's own serializer for the declared type,
    /// so sending it back verbatim round-trips to the identical stored value.
    /// Bytes, because "verbatim" is the entire contract — see [`Value::text`].
    pub text: RawText,
}

/// One `MATERIALIZED` column's value: stored at insert, frozen thereafter.
///
/// Deliberately not part of [`RowResult::values`] — `SELECT *` does not return
/// these, so a preview that mixed them in would disagree with what a subscriber
/// reading the table sees. `ALIAS` is never reported at all, because
/// `ALTER ... MODIFY COLUMN a ALIAS <expr>` retroactively changes what already
/// inserted rows read back as.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Computed {
    /// The `MATERIALIZED` column.
    pub column: String,
    /// Always `MATERIALIZED` today.
    pub kind: String,
    /// ClickHouse's rendering of the computed value, as bytes — see
    /// [`Value::text`].
    pub text: RawText,
}

/// The outcome of validating and coercing one row.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RowResult {
    /// The coerced stored row, positional, excluding `skipped` columns.
    pub values: Vec<Value>,
    /// Every silent change. Not optional: this is the one part of the product
    /// ClickHouse does not provide, and omitting it scores 0 % recall on the
    /// arbiter's transformation axis.
    pub transformed: Vec<Transform>,
    /// The verdict on this row.
    pub outcome: Outcome,
    /// ClickHouse's code when rejected, `-2` when unsupported, `691` when
    /// poisoned.
    pub err_code: i32,
    /// ClickHouse's own message, empty when there is none.
    pub err_msg: String,
    /// Input fields with no matching column.
    pub unknown_fields: Vec<String>,
    /// Settings this build models but declines. Non-empty means the answer must
    /// not be scored as agreement, and promotes the row to
    /// [`Outcome::Unsupported`].
    pub unsupported_settings: Vec<String>,
    /// Columns whose volatile DEFAULT this library resolved itself.
    pub substituted: Vec<Substitution>,
    /// `MATERIALIZED` values — durable, but not part of `SELECT *`.
    pub computed: Vec<Computed>,
}

/// The outcome of one request body, which may hold many rows.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct BatchResult {
    /// One entry per row the reader consumed, in input order — including
    /// (2026-08-27) rows dropped under `input_format_allow_errors_*`, which
    /// appear as [`Outcome::Skipped`] with the server's own caught error and
    /// no values. Filter on the row outcome when rendering survivors; a
    /// skipped row is never stored.
    pub rows: Vec<RowResult>,
    /// The batch verdict, which is not the conjunction of the row verdicts: a
    /// row can be accepted while the batch stores nothing.
    pub outcome: Outcome,
    /// ClickHouse's code for the batch, `-2` when unsupported.
    pub err_code: i32,
    /// ClickHouse's own message for the batch.
    pub err_msg: String,
    /// Rows the reader consumed, failed ones included.
    pub rows_read: usize,
    /// Rows dropped under `input_format_allow_errors_num` / `_ratio`.
    pub rows_skipped: usize,
    /// Every transform in the batch, each tagged with its row index, including
    /// the batch-level `storage_transforms` — a TTL-expired row is `accepted`
    /// per row and **not stored** per batch, and a binding that dropped these
    /// would tell a tenant a row was stored that the table deletes at merge
    /// time.
    pub transformed: Vec<Transform>,
    /// The stored preview **after** the engine's insert-time merge, as raw JSON
    /// objects. Present only when a specialized engine or a TTL forced the
    /// storage path. When present it — not [`rows`](Self::rows) — is the stored
    /// truth: it can be shorter (a `SummingMergeTree` dropping an all-zero row)
    /// or reordered (the block is sorted by the sorting key first).
    ///
    /// This is the **UTF-8 view** and is the one place in this crate where a
    /// rendering can still be repaired: a merged row containing a non-UTF-8
    /// `String` value carries U+FFFD here.
    /// [`engine_rows_bytes`](Self::engine_rows_bytes) is the authoritative form
    /// and always exact.
    pub engine_rows: Option<Vec<String>>,
    /// The same merged rows, byte-exact. Private so that the two can never
    /// disagree; read through [`engine_rows_bytes`](Self::engine_rows_bytes).
    engine_rows_raw: Option<Vec<RawText>>,

    /// The export channel ([`crate::Schema::rows_export`] only; always `None`
    /// from [`crate::Schema::rows`]). The batch's accepted rows, serialized
    /// once by the transcription of ClickHouse's own output writer for the
    /// requested export format, copied out of the C buffer and freed before
    /// the call returned — no ownership crosses the boundary. Three states,
    /// the ABI's own (`docs/reference/c-abi.md` §Rows):
    ///
    /// * `None` — no export was requested, the export was DECLINED
    ///   ([`export_declined`](Self::export_declined) then names the reason),
    ///   or a call-level verdict preempted the export machinery entirely
    ///   (the batch [`outcome`](Self::outcome) is then the reason and
    ///   `export_declined` stays empty).
    /// * `Some(empty)` — an accepted batch with zero accepted rows: the
    ///   EMITTED-EMPTY case, distinguishable from a decline.
    /// * `Some(bytes)` — the exported lines; slice per
    ///   [`spans`](Self::spans).
    pub payload: Option<Vec<u8>>,
    /// Index-aligned with [`rows`](Self::rows): `spans[i]` addresses row i's
    /// line inside [`payload`](Self::payload), `{0,0}` for a non-accepted
    /// row. `None` when no bytes were emitted.
    pub spans: Option<Vec<Span>>,
    /// The library's reason for withholding requested export bytes (a
    /// non-accepted batch, the full-arity guard, a serialization failure,
    /// `output_format_json_validate_utf8`) — empty when bytes were emitted or
    /// no export was requested. A decline here is `-2`-class honesty, never a
    /// server verdict.
    pub export_declined: String,
}

impl BatchResult {
    /// The engine's merged rows as ClickHouse wrote them, byte for byte.
    ///
    /// Prefer this to [`engine_rows`](Self::engine_rows) whenever the bytes
    /// matter — a comparison against a real server, or a body to post onward.
    /// `None` means no engine semantics ran; `Some(&[])` means they ran and
    /// stored nothing, which is a different answer (a TTL-expired row).
    pub fn engine_rows_bytes(&self) -> Option<&[RawText]> {
        self.engine_rows_raw.as_deref()
    }
}

// ------------------------------------------------------------- wire documents
//
// Read by `crate::doc`, which parses bytes rather than `str`: a result document
// holding a non-UTF-8 `String` value is not valid UTF-8, and every `stored` /
// `ref` here keeps ClickHouse's own bytes. `Option` means **key presence**, not
// non-nullness: a present `null` is `Some("null")`, which is what makes a stored
// null distinguishable from an absent value.

#[derive(Debug, Default)]
pub(crate) struct RowDoc {
    pub outcome: String,
    pub code: i32,
    pub err: String,
    pub unknown_fields: Vec<String>,
    pub unsupported_settings: Vec<String>,
    pub cols: Vec<ColDoc>,
    pub computed: Vec<CompDoc>,
}

#[derive(Debug, Default)]
pub(crate) struct CompDoc {
    pub name: String,
    pub kind: String,
    pub stored: Option<RawText>,
}

#[derive(Debug, Default)]
pub(crate) struct ColDoc {
    pub name: String,
    /// The column's canonical declared type. Part of the documented wire shape
    /// and parsed for completeness; the classifier switches on `base` instead,
    /// and the public `Value` mirrors the spec's four fields.
    #[allow(dead_code)]
    pub declared_type: String,
    /// The type with `Nullable`/`LowCardinality` peeled off. The reason
    /// classifier switches on this.
    pub base: String,
    pub src: String,
    /// The raw input text for this field. Bytes, not `String`: a row can supply
    /// invalid UTF-8 on purpose, and the supplied-vs-stored detector has to read
    /// both sides the same way — repairing one side and not the other would
    /// report a change that never happened.
    pub input: RawText,
    pub ref_type: String,
    /// `stored` and `ref` stay raw: a ClickHouse `String` holds arbitrary bytes
    /// and its integers go to 2^256, so decoding either into a language type
    /// would invent a transformation that never happened.
    pub stored: Option<RawText>,
    pub reference: Option<RawText>,
    pub nullable: bool,
    pub poison: bool,
    pub null_input: bool,
    /// The row named this column more than once and ClickHouse kept the
    /// **first** value, discarding the rest with no signal.
    pub dup_dropped: bool,
    /// The C layer found this leaf numeric/temporal but had no reference-ladder
    /// entry (audit F4): the classifier treats a visible change as lossy, never
    /// `reformat`. Absent from every artifact whose build gate ran.
    pub ref_unclassified: bool,
}

impl ColDoc {
    /// ClickHouse's rendering of the stored value, byte for byte. A JSON `null`
    /// here is a real stored value, not the absence of one; only the poison case
    /// has no renderable value.
    pub(crate) fn stored_raw(&self) -> RawText {
        match (&self.stored, self.poison) {
            (Some(v), false) => v.clone(),
            _ => RawText::default(),
        }
    }

    /// The same input bytes parsed through the widened reference type, or empty
    /// when no reference parse applies.
    pub(crate) fn ref_raw(&self) -> RawText {
        match &self.reference {
            Some(v) if v != "null" => v.clone(),
            _ => RawText::default(),
        }
    }

    pub(crate) fn stored_is_json_null(&self) -> bool {
        self.stored.as_ref().is_some_and(|v| v == "null")
    }
}

#[derive(Debug, Default)]
pub(crate) struct BatchDoc {
    pub outcome: String,
    pub code: i32,
    pub err: String,
    pub rows_read: usize,
    pub rows_skipped: usize,
    pub rows: Vec<RowDoc>,
    pub engine_rows: Option<Vec<RawText>>,
    pub storage_transforms: Vec<StorageTransformDoc>,
    /// Present exactly when export bytes were emitted — one `{off,len}` per
    /// `rows[]` entry, index-aligned (`docs/reference/c-abi.md` §Rows).
    pub row_spans: Option<Vec<Span>>,
    /// Present exactly when an export was requested and withheld.
    pub export_declined: String,
}

/// The wire document `chs_filter_rows` returns.
#[derive(Debug, Default)]
pub(crate) struct FilterDoc {
    pub outcome: String,
    pub code: i32,
    pub err: String,
    pub rows_read: usize,
    pub unsupported_settings: Vec<String>,
    pub verdicts: String,
    pub errors: Vec<FilterRowError>,
}

#[derive(Debug, Default)]
pub(crate) struct StorageTransformDoc {
    pub row: usize,
    pub column: String,
    pub reason: String,
    pub stored: Option<RawText>,
}

// ------------------------------------------------------------- documents -> API

pub(crate) fn row_result_of(doc: RowDoc) -> RowResult {
    let mut res = RowResult {
        outcome: outcome_of(&doc.outcome),
        err_code: doc.code,
        err_msg: doc.err,
        unknown_fields: doc.unknown_fields,
        unsupported_settings: doc.unsupported_settings,
        ..Default::default()
    };
    // A declined setting must never be scored as agreement.
    if !res.unsupported_settings.is_empty() && res.outcome != Outcome::Rejected {
        res.outcome = Outcome::Unsupported;
    }
    for c in &doc.cols {
        if c.src == "skipped" {
            continue;
        }
        res.values.push(Value {
            column: c.name.clone(),
            text: c.stored_raw(),
            null: c.stored_is_json_null() && !c.poison,
            source: c.src.clone(),
        });
        if c.src == "default_substituted" {
            res.substituted.push(Substitution {
                column: c.name.clone(),
                // For a DEFAULT-sourced column the ABI puts the EXPRESSION in
                // `input` — always text, and always this build's own canonical
                // spelling of it.
                expr: c.input.to_lossy().into_owned(),
                text: c.stored_raw(),
            });
        }
        res.transformed.extend(classify(c));
    }
    for m in doc.computed {
        res.computed.push(Computed {
            column: m.name,
            kind: m.kind,
            text: m.stored.unwrap_or_default(),
        });
    }
    res
}

pub(crate) fn batch_result_of(doc: BatchDoc) -> BatchResult {
    let mut res = BatchResult {
        outcome: outcome_of(&doc.outcome),
        err_code: doc.code,
        err_msg: doc.err,
        rows_read: doc.rows_read,
        rows_skipped: doc.rows_skipped,
        engine_rows: doc.engine_rows.as_ref().map(|rows| {
            rows.iter()
                .map(|r| r.to_lossy().into_owned())
                .collect::<Vec<_>>()
        }),
        engine_rows_raw: doc.engine_rows,
        spans: doc.row_spans,
        export_declined: doc.export_declined,
        ..Default::default()
    };
    for (i, rd) in doc.rows.into_iter().enumerate() {
        let mut rr = row_result_of(rd);
        for t in &mut rr.transformed {
            t.row = i;
        }
        res.transformed.extend(rr.transformed.iter().cloned());
        res.rows.push(rr);
    }
    // storage_transforms is where "not stored" lives: fold it in, or a tenant is
    // told a row was stored that the table deletes at merge time.
    for st in doc.storage_transforms {
        res.transformed.push(Transform {
            column: st.column,
            input: String::new(),
            stored: st
                .stored
                .map(|v| v.to_lossy().into_owned())
                .unwrap_or_default(),
            reason: st.reason,
            row: st.row,
        });
    }
    res
}

/// Turn a filter document into a [`FilterResult`] — the one assembler, like
/// [`batch_result_of`]. An unrecognized outcome spelling degrades to
/// [`FilterOutcome::Unsupported`], never `Rejected`; an unrecognized verdict
/// character degrades to [`Verdict::Decline`] — both are the fail-closed,
/// never-scored-as-agreement arms.
pub(crate) fn filter_result_of(doc: FilterDoc) -> FilterResult {
    let outcome = match doc.outcome.as_str() {
        "ok" => FilterOutcome::Ok,
        "rejected" => FilterOutcome::Rejected,
        _ => FilterOutcome::Unsupported,
    };
    let mut res = FilterResult {
        outcome,
        err_code: doc.code,
        err_msg: doc.err,
        rows_read: doc.rows_read,
        unsupported_settings: doc.unsupported_settings,
        ..Default::default()
    };
    if res.outcome != FilterOutcome::Ok {
        // A malformed body yields no partial answers; enforce what the
        // document already promises.
        return res;
    }
    res.verdicts = doc
        .verdicts
        .chars()
        .map(|c| match c {
            't' => Verdict::True,
            'f' => Verdict::False,
            'e' => Verdict::Error,
            _ => Verdict::Decline,
        })
        .collect();
    res.errors = doc.errors;
    res
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::json::quote_bare_denormals;

    fn parse_row(s: &str) -> RowResult {
        let repaired = quote_bare_denormals(s.as_bytes());
        row_result_of(crate::doc::row_doc(&repaired).unwrap())
    }

    #[test]
    fn an_unknown_outcome_degrades_to_unsupported_never_rejected() {
        // docs/reference/bindings.md §RowResult (2026-08-26): a future artifact's new
        // verdict lands on the arm that is never scored as agreement; a
        // default of Rejected would manufacture an over-reject.
        let rr = parse_row(r#"{"outcome":"verdict_from_the_future","code":0,"err":"","cols":[]}"#);
        assert_eq!(rr.outcome, Outcome::Unsupported);
        assert_eq!(outcome_of("accepted"), Outcome::Accepted);
        assert_eq!(outcome_of("rejected"), Outcome::Rejected);
        assert_eq!(outcome_of("accepted_poisoned"), Outcome::AcceptedPoisoned);
        assert_eq!(outcome_of("unsupported"), Outcome::Unsupported);
        assert_eq!(outcome_of("skipped"), Outcome::Skipped);
        assert_eq!(outcome_of(""), Outcome::Unsupported);
    }

    #[test]
    fn a_skipped_row_parses_with_the_caught_error_and_no_values() {
        // docs/reference/c-abi.md §"outcome":"skipped" (2026-08-27): a row dropped under
        // input_format_allow_errors_* keeps its place in `rows` in the short
        // form of a rejected document, carrying the server's caught error.
        let rr =
            parse_row(r#"{"outcome":"skipped","code":27,"err":"Cannot parse input","cols":[]}"#);
        assert_eq!(rr.outcome, Outcome::Skipped);
        assert_eq!(rr.err_code, 27);
        assert_eq!(rr.err_msg, "Cannot parse input");
        assert!(rr.values.is_empty());
    }

    #[test]
    fn a_rejected_document_omits_most_keys() {
        let rr =
            parse_row(r#"{"outcome":"rejected","code":27,"err":"Cannot parse input","cols":[]}"#);
        assert_eq!(rr.outcome, Outcome::Rejected);
        assert_eq!(rr.err_code, 27);
        assert!(rr.values.is_empty());
        assert!(rr.unknown_fields.is_empty());
    }

    #[test]
    fn overflow_wrap_is_derived_from_the_reference_parse() {
        let rr = parse_row(
            r#"{"outcome":"accepted","code":0,"err":"","cols":[
                 {"name":"x","type":"UInt8","base":"UInt8","src":"input","input":"256",
                  "stored":0,"ref":256,"ref_type":"Int256",
                  "nullable":false,"poison":false,"null_input":false,"dup_dropped":false}]}"#,
        );
        assert_eq!(rr.outcome, Outcome::Accepted);
        assert_eq!(rr.values[0].text, "0");
        assert_eq!(rr.transformed.len(), 1);
        assert_eq!(rr.transformed[0].reason, crate::reason::OVERFLOW_WRAP);
        assert!(rr.transformed[0].lossy());
    }

    #[test]
    fn declined_settings_promote_the_row_to_unsupported() {
        let rr = parse_row(
            r#"{"outcome":"accepted","unsupported_settings":["some_setting"],"cols":[]}"#,
        );
        assert_eq!(rr.outcome, Outcome::Unsupported);
    }

    #[test]
    fn storage_transforms_are_folded_into_the_batch() {
        let doc = crate::doc::batch_doc(
            br#"{"outcome":"accepted","code":0,"err":"","engine_rows":[],
                "storage_transforms":[{"row":0,"column":"","reason":"ttl_expired"}],
                "rows_read":1,"rows_skipped":0,
                "rows":[{"outcome":"accepted","cols":[]}]}"#,
        )
        .unwrap();
        let br = batch_result_of(doc);
        assert_eq!(br.outcome, Outcome::Accepted);
        assert_eq!(br.engine_rows.as_deref(), Some(&[][..]));
        // An engine that stored nothing is present-and-empty in both views, and
        // is not the same answer as no engine at all.
        assert_eq!(br.engine_rows_bytes().map(<[RawText]>::len), Some(0));
        assert_eq!(br.rows.len(), 1);
        assert_eq!(br.transformed.len(), 1);
        assert_eq!(br.transformed[0].reason, crate::reason::TTL_EXPIRED);
        assert!(br.transformed[0].lossy());
    }

    #[test]
    fn engine_rows_keep_clickhouses_bytes_in_the_authoritative_view() {
        // A merged row whose String value is not valid UTF-8: the text view is
        // repaired, and the byte view is not.
        let mut doc: Vec<u8> = br#"{"outcome":"accepted","engine_rows":[{"x":""#.to_vec();
        doc.extend_from_slice(&[0xc3, b'(']);
        doc.extend_from_slice(br#""}],"rows":[]}"#);
        let br = batch_result_of(crate::doc::batch_doc(&doc).unwrap());
        assert_eq!(
            br.engine_rows_bytes().unwrap()[0].as_bytes(),
            [b'{', b'"', b'x', b'"', b':', b'"', 0xc3, b'(', b'"', b'}'],
        );
        assert_eq!(br.engine_rows.as_ref().unwrap()[0], "{\"x\":\"\u{fffd}(\"}");
    }

    #[test]
    fn a_stored_null_is_a_value_and_an_absent_one_is_not() {
        // Both columns are Nullable and neither is poisoned; the difference is
        // that the wire said `null` for the first and said nothing for the
        // second. The reference SDK reports Text "null" / Null true for the
        // first, which is what this must agree with.
        let rr = parse_row(
            r#"{"outcome":"accepted","cols":[
                 {"name":"a","type":"Nullable(UInt8)","base":"UInt8","src":"input",
                  "input":"null","stored":null,"nullable":true,"null_input":true},
                 {"name":"b","type":"Nullable(UInt8)","base":"UInt8","src":"input",
                  "input":"null","nullable":true,"null_input":true}]}"#,
        );
        assert_eq!(
            (rr.values[0].text.as_str(), rr.values[0].null),
            (Some("null"), true)
        );
        assert_eq!(
            (rr.values[1].text.as_str(), rr.values[1].null),
            (Some(""), false)
        );
    }

    #[test]
    fn denormals_survive_as_text() {
        let rr = parse_row(
            r#"{"outcome":"accepted","cols":[
                 {"name":"f","type":"Float64","base":"Float64","src":"input",
                  "input":"1e400","stored":inf,"ref":null,"ref_type":""}]}"#,
        );
        assert_eq!(rr.values[0].text, "\"inf\"");
        assert_eq!(rr.transformed[0].reason, crate::reason::LOSSY_NUMERIC);
    }

    #[test]
    fn format_codes_are_the_abi_numbers() {
        assert_eq!(Format::JsonEachRow.code(), 0);
        assert_eq!(Format::Csv.code(), 1);
        assert_eq!(Format::Tsv.code(), 2);
        assert_eq!(Format::Values.code(), 3);
        assert_eq!(Format::JsonCompactEachRow.code(), 4);
        assert_eq!(Format::RowBinary.code(), 5);
        assert_eq!(Format::RowBinaryWithDefaults.code(), 6);
        assert_eq!(Format::RowBinaryWithNamesAndTypesAndDefaults.code(), 7);
        assert_eq!(Format::Native.code(), 8);
        assert_eq!(Format::Buffers.code(), 9);
    }
}
