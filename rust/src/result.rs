//! The result types, one to one with the library's documents
//! (`docs/reference/bindings-v1.md` §5).
//!
//! Every field is a field of the document the library returned, or the bytes
//! of an output buffer. Nothing is computed here: whether a reason is lossy,
//! whether a source is stored and whether a verdict is an answer are the
//! description's own facts, read from the generated vocabularies, never from a
//! list this crate keeps.
//!
//! **Bytes are bytes.** A column name, a message and a rendered value are
//! [`RawText`]: the bytes are authoritative and the UTF-8 view is fallible.
//! The only strings are the ASCII the ABI promises (a setting name, a
//! capability) and one lossy display form per error; every vocabulary value is
//! its generated type, whose `Unknown` member keeps a value the description
//! does not list (rule r3).
//!
//! The result types are `#[non_exhaustive]`: a later minor can add a document
//! field without breaking a caller.

use std::collections::BTreeMap;

use crate::abi2::vocab_gen::{DefaultKind, FilterOutcome, Outcome, Reason, Source, Verdict};
use crate::raw::RawText;

/// A byte range of the input (or of an export): `off` and `len`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
#[non_exhaustive]
pub struct Span {
    /// Where the range starts.
    pub off: u64,
    /// How many bytes it covers.
    pub len: u64,
}

/// One column's entry of a row document (`cols`).
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Value {
    /// The column's name (from `name` or `name_b64`).
    pub column: RawText,
    /// ClickHouse's own rendering of the value (from `stored`). Empty when the
    /// document carried none.
    pub text: RawText,
    /// Whether the stored value is NULL: the library decides it, poison
    /// included.
    pub null: bool,
    /// Where the value came from (`src`, a `value_src` value). One the
    /// description does not list is [`Source::Unknown`] (rule r3).
    pub source: Source,
    /// The description's `is_stored` fact for [`Value::source`]; `false` for
    /// an unknown source.
    pub is_stored: bool,
    /// The raw bytes of a scalar `String` or `FixedString` value (from
    /// `value_b64`), whether or not they are valid UTF-8; `None` otherwise. A
    /// value nested in another type has none in 1.0.
    pub value: Option<RawText>,
}

/// One transformation: a value the library stored differently from the input.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
#[non_exhaustive]
pub struct Transform {
    /// The column (from `column` or `column_b64`).
    pub column: RawText,
    /// The input, as the document renders it.
    pub input: RawText,
    /// What was stored.
    pub stored: RawText,
    /// A `transform_reason` value. One the description does not list is
    /// [`Reason::Unknown`] (rule r3).
    pub reason: Reason,
    /// The description's `lossy` fact for [`Transform::reason`]; an unknown
    /// reason reports its fallback's, lossy.
    pub lossy: bool,
    /// The row's index in the body; 0 in a single-row result.
    pub row: u64,
}

/// One computed (MATERIALIZED) value.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
#[non_exhaustive]
pub struct Computed {
    /// The column (from `name` or `name_b64`).
    pub column: RawText,
    /// What kind of computed value this is. ASCII.
    pub kind: String,
    /// The rendering (from `stored` or `stored_b64`).
    pub text: RawText,
    /// The raw bytes of a scalar `String` or `FixedString` value (from
    /// `value_b64`); `None` otherwise.
    pub value: Option<RawText>,
}

/// One cell of a stored row after the engine's insert-time merge.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
#[non_exhaustive]
pub struct EngineCell {
    /// The column's name (from `name` or `name_b64`).
    pub column: RawText,
    /// ClickHouse's own rendering of the value (from `stored` or `stored_b64`).
    pub text: RawText,
    /// Whether the value is NULL.
    pub null: bool,
    /// The raw bytes of a scalar `String` or `FixedString` value (from
    /// `value_b64`); `None` otherwise.
    pub value: Option<RawText>,
}

/// A row document, from `chs_preview_row`, and each entry of a batch's `rows`.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
#[non_exhaustive]
pub struct RowResult {
    /// The outcome, already final: the library applies every promotion.
    pub outcome: Outcome,
    /// ClickHouse's error code for this row (`code`).
    pub err_code: i32,
    /// ClickHouse's message for this row (`err`).
    pub err_msg: RawText,
    /// Every entry of `cols`, in document order.
    pub columns: Vec<Value>,
    /// The entries of `cols` whose [`Value::is_stored`] is true, in order.
    pub values: Vec<Value>,
    /// The transformations, as the library computed them.
    pub transformed: Vec<Transform>,
    /// Input fields that name no column (`unknown_fields`), as bytes.
    pub unknown_fields: Vec<RawText>,
    /// Settings this build does not support (`unsupported_settings`, name
    /// objects), as bytes.
    pub unsupported_settings: Vec<RawText>,
    /// MATERIALIZED values (`computed`).
    pub computed: Vec<Computed>,
    /// The attached filter's verdict, present only with an attached filter.
    pub verdict: Option<Verdict>,
    /// The filter's error code for this row.
    pub verdict_code: i32,
    /// The filter's message for this row.
    pub verdict_err: RawText,
    /// The row's partition id, when the schema has a partition key.
    pub partition_id: Option<RawText>,
    /// The bytes of the input the reader consumed for this record.
    pub input_span: Option<Span>,
}

/// What a batch's header line said, when the reader exposes it.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
#[non_exhaustive]
pub struct Header {
    /// Whether the header line was consumed.
    pub consumed: bool,
    /// How many lines it took.
    pub lines: u64,
    /// The names it carried, each from a `name` or `name_b64` entry.
    pub names: Vec<RawText>,
}

/// What the reader decided about the body's framing. `None` is **unknown**
/// (the document wrote JSON `null`), never false and never empty.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
#[non_exhaustive]
pub struct Framing {
    /// Whether a leading byte-order mark was skipped; `None` when the vendored
    /// reader does not expose it.
    pub bom_skipped: Option<bool>,
    /// `array`, `stream`, or none. ASCII.
    pub container: Option<String>,
    /// The header; `None` when the vendored reader does not expose it.
    pub header: Option<Header>,
}

/// A batch document and its export buffer, from `chs_preview_batch`.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
#[non_exhaustive]
pub struct BatchResult {
    /// The outcome, final.
    pub outcome: Outcome,
    /// ClickHouse's error code (`code`).
    pub err_code: i32,
    /// ClickHouse's message (`err`).
    pub err_msg: RawText,
    /// Each row, decoded as a [`RowResult`].
    pub rows: Vec<RowResult>,
    /// Rows the reader read.
    pub rows_read: u64,
    /// Rows the reader skipped under error recovery.
    pub rows_skipped: u64,
    /// Every transformation in the batch with its `row`, storage transforms
    /// (TTL) included, as the library lists them.
    pub transformed: Vec<Transform>,
    /// Each stored row after the engine's insert-time merge: a list of cells.
    pub engine_rows: Option<Vec<Vec<EngineCell>>>,
    /// The export bytes, present only when an export was asked for.
    pub payload: Option<Vec<u8>>,
    /// Each exported row's place in [`BatchResult::payload`].
    pub spans: Option<Vec<Span>>,
    /// Why an export was declined; empty when it was not.
    pub export_declined: RawText,
    /// Rows an attached filter passed.
    pub rows_passed: u64,
    /// Rows an attached filter cut.
    pub rows_cut: u64,
    /// How many distinct partitions the batch touched.
    pub partition_count: Option<u64>,
    /// The byte ranges the reader's error recovery skipped. It does not account
    /// for every record: a skipped row's `input_span` can cover more than one
    /// input record, so verdicts can be fewer than records while this is empty.
    /// An independent record count can be fooled too. A caller that needs every
    /// record accounted for declines a body with any skipped row or any
    /// `unconsumed` range (`docs/guides/batches.md`).
    pub unconsumed: Vec<Span>,
    /// What the reader decided about the body's framing.
    pub framing: Option<Framing>,
}

/// One row's filter error.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
#[non_exhaustive]
pub struct FilterRowError {
    /// The row's index in the body.
    pub row: u64,
    /// ClickHouse's error code for the row.
    pub code: i32,
    /// ClickHouse's message for the row (from `err`).
    pub msg: RawText,
}

/// A filter evaluation, from `chs_filter_eval_body` and `chs_filter_eval_block`.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
#[non_exhaustive]
pub struct FilterResult {
    /// The outcome.
    pub outcome: FilterOutcome,
    /// ClickHouse's error code (`code`).
    pub err_code: i32,
    /// ClickHouse's message (`err`).
    pub err_msg: RawText,
    /// Rows the evaluation read.
    pub rows_read: u64,
    /// Settings this build does not support (name objects), as bytes.
    pub unsupported_settings: Vec<RawText>,
    /// One verdict per row. `Error` and `Decline` are never answers; a caller
    /// enforcing visibility fails closed on both ([`Verdict::answered`]).
    pub verdicts: Vec<Verdict>,
    /// Each error or declined row, itemized.
    pub errors: Vec<FilterRowError>,
}

/// One column of a compiled schema.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct Column {
    /// The column's name (rule 5: `name` or `name_b64`).
    pub name: RawText,
    /// The canonical type. It can differ between ClickHouse lines, so a
    /// cross-line schema hash is taken over the caller's own statement, never
    /// over this.
    pub r#type: RawText,
    /// What the column's DEFAULT clause is.
    pub default_kind: DefaultKind,
    /// The DEFAULT expression; empty when there is none.
    pub default_expr: RawText,
}

/// A schema's columns, from `chs_schema_describe`.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
#[non_exhaustive]
pub struct SchemaDescription {
    /// The columns, in declared order.
    pub columns: Vec<Column>,
}

/// One column read from a server's `system.columns` row.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct DiscoveredColumn {
    /// The column's name (rule 5).
    pub name: RawText,
    /// The column declaration as ClickHouse's own formatter writes it.
    pub declaration: RawText,
}

/// The columns of a table, from `chs_discover_columns`.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
#[non_exhaustive]
pub struct Discovery {
    /// The columns, in `system.columns` position order.
    pub columns: Vec<DiscoveredColumn>,
    /// The declarations joined for a `CREATE TABLE` (`columns_sql`).
    pub columns_sql: RawText,
}

/// What a build supports, from `build_info`'s `capabilities`. Every list is
/// ASCII text.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
#[non_exhaustive]
pub struct Capabilities {
    /// ClickHouse format names this build reads.
    pub input_formats: Vec<String>,
    /// ClickHouse format names this build writes.
    pub export_formats: Vec<String>,
    /// The document groups this build emits.
    pub doc_flags: Vec<String>,
    /// Build-level features; open (an unknown entry is not an error).
    pub features: Vec<String>,
}

/// What a library is, from `chs_build_info`.
#[derive(Debug, Clone, PartialEq)]
#[non_exhaustive]
pub struct BuildInfo {
    /// The `build_info` schema number.
    pub schema: i64,
    /// The ABI generation.
    pub abi: i64,
    /// The ABI fingerprint.
    pub abi_fingerprint: String,
    /// Four parts, no channel.
    pub clickhouse_version: String,
    /// The release channel.
    pub channel: String,
    /// The minor line.
    pub clickhouse_minor: String,
    /// The upstream commit.
    pub clickhouse_commit: String,
    /// The core commit.
    pub core_commit: String,
    /// The build id.
    pub build: String,
    /// The inputs digest.
    pub inputs_sha256: String,
    /// The operating system.
    pub os: String,
    /// The architecture.
    pub arch: String,
    /// The parsed toolchain object, uninterpreted.
    pub toolchain: serde_json::Value,
    /// What the build supports.
    pub capabilities: Capabilities,
    /// The exact bytes the library returned.
    pub raw: Vec<u8>,
}

/// One entry of the error-code table.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ErrorCodeEntry {
    /// ClickHouse's numeric code.
    pub code: i32,
    /// ClickHouse's name for it.
    pub name: String,
}

/// This build's error-code table: every code of the vendored table, read from
/// the library's own document. An unknown code or name is absent, never
/// synthesized; names match exactly.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ErrorCodeTable {
    entries: Vec<ErrorCodeEntry>,
    by_code: BTreeMap<i32, usize>,
    by_name: BTreeMap<String, usize>,
}

impl ErrorCodeTable {
    pub(crate) fn from_entries(entries: Vec<ErrorCodeEntry>) -> ErrorCodeTable {
        let mut by_code = BTreeMap::new();
        let mut by_name = BTreeMap::new();
        for (i, e) in entries.iter().enumerate() {
            by_code.entry(e.code).or_insert(i);
            by_name.entry(e.name.clone()).or_insert(i);
        }
        ErrorCodeTable {
            entries,
            by_code,
            by_name,
        }
    }

    /// The name of a code, or `None` if the table has none.
    pub fn name(&self, code: i32) -> Option<&str> {
        self.by_code
            .get(&code)
            .map(|&i| self.entries[i].name.as_str())
    }

    /// The code of a name (an exact match), or `None`.
    pub fn code(&self, name: &str) -> Option<i32> {
        self.by_name.get(name).map(|&i| self.entries[i].code)
    }

    /// Every entry, in the library's order (ascending code).
    pub fn iter(&self) -> std::slice::Iter<'_, ErrorCodeEntry> {
        self.entries.iter()
    }

    /// How many entries the table holds.
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    /// Whether the table holds no entry.
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

impl<'a> IntoIterator for &'a ErrorCodeTable {
    type Item = &'a ErrorCodeEntry;
    type IntoIter = std::slice::Iter<'a, ErrorCodeEntry>;

    fn into_iter(self) -> Self::IntoIter {
        self.iter()
    }
}
