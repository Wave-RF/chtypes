//! parfix: a fixture for scripts/parity-surface.py — the Rust surface
//! tests/fixtures/parity-surface/doc.md describes, plus one allowlisted extra
//! (`ErrorCodeTable::len`). Nothing here does anything.

use std::sync::Arc;

/// The crate's result.
pub type Result<T> = std::result::Result<T, Error>;

/// The process setup.
#[derive(Default)]
pub struct SetupOptions {
    /// The image zone.
    pub timezone: Option<String>,
}

/// Record the setup.
pub fn setup(options: SetupOptions) -> Result<()> {
    let _ = options;
    Ok(())
}

/// The fetch layer's record.
pub struct Resolved {
    /// The library file.
    pub library_path: String,
    /// What the fetch warned about.
    pub warnings: Vec<String>,
}

/// Opens libraries.
pub struct Registry;

impl Registry {
    /// Construct a registry.
    pub fn new() -> Result<Registry> {
        Ok(Registry)
    }

    /// Open a version.
    pub fn for_version(&self, request: &str) -> Result<Arc<Library>> {
        let _ = request;
        Ok(Arc::new(Library))
    }

    /// What is installed.
    pub fn installed(&self) -> Result<Vec<Resolved>> {
        Ok(Vec::new())
    }
}

/// One loaded image.
pub struct Library;

impl Library {
    /// Its version.
    pub fn version(&self) -> &str {
        ""
    }

    /// Canonicalize a type.
    pub fn validate_type(&self, type_expr: impl AsRef<[u8]>) -> Result<RawText> {
        Ok(RawText(type_expr.as_ref().to_vec()))
    }

    /// Compile a table.
    pub fn compile_table(self: &Arc<Self>, create_table: impl AsRef<[u8]>, options: &CompileOptions) -> Result<Schema> {
        let _ = (create_table.as_ref(), options);
        Ok(Schema)
    }
}

/// The compile options.
#[derive(Default)]
pub struct CompileOptions {
    /// The settings.
    pub settings: Vec<(String, String)>,
}

/// A compiled table.
#[derive(Clone)]
pub struct Schema;

impl Drop for Schema {
    fn drop(&mut self) {}
}

/// Bytes out.
pub struct RawText(Vec<u8>);

/// A format.
#[repr(i32)]
pub enum Format {
    /// JSONEachRow.
    JsonEachRow = 0,
    /// CSV.
    Csv = 1,
}

impl Format {
    /// ClickHouse's own name.
    pub fn ch_name(&self) -> &'static str {
        match self {
            Format::JsonEachRow => "JSONEachRow",
            Format::Csv => "CSV",
        }
    }
}

/// A filter verdict.
pub enum Verdict {
    /// True.
    True,
    /// False.
    False,
}

impl Verdict {
    /// Whether the verdict is an answer.
    pub fn answered(&self) -> bool {
        true
    }
}

/// Every call error's fields.
pub struct CallError {
    /// ClickHouse's own code.
    pub ch_code: i32,
}

/// A loader refusal.
pub struct Refusal {
    /// Why.
    pub reason: String,
    /// The library path.
    pub path: String,
}

/// The errors.
pub enum Error {
    /// A refusal.
    Schema(CallError),
    /// An incompatible artifact.
    ArtifactIncompatible(Refusal),
    /// A missing artifact.
    ArtifactMissing(Refusal),
    /// A pinned artifact.
    ArtifactPinned(Refusal),
}

impl Error {
    /// Whether this is a decline.
    pub fn is_unsupported(&self) -> bool {
        false
    }
}

/// A byte range.
pub struct Span {
    /// Where.
    pub off: u64,
    /// How long.
    pub len: u64,
}

/// A header.
pub struct Header {
    /// Consumed.
    pub consumed: bool,
    /// Lines.
    pub lines: usize,
}

/// The reader's framing.
pub struct Framing {
    /// Whether a BOM was skipped.
    pub bom_skipped: Option<bool>,
    /// The header.
    pub header: Option<Header>,
}

/// One row's error.
pub struct RowError {
    /// The row.
    pub row: usize,
    /// The message.
    pub msg: RawText,
}

/// A batch document.
pub struct BatchResult {
    /// Rows read.
    pub rows_read: u64,
    /// The partition.
    pub partition_id: Option<RawText>,
    /// The columns' SQL.
    pub columns_sql: RawText,
    /// The spans.
    pub spans: Option<Vec<Span>>,
    /// The framing.
    pub framing: Option<Framing>,
    /// The row errors.
    pub errors: Vec<RowError>,
}

/// One error-code entry.
pub struct ErrorCodeEntry {
    /// The code.
    pub code: i32,
    /// The name.
    pub name: String,
}

/// The error-code table.
pub struct ErrorCodeTable {
    entries: Vec<ErrorCodeEntry>,
}

impl ErrorCodeTable {
    /// Look a code up.
    pub fn name(&self, code: i32) -> Option<&str> {
        self.entries.iter().find(|e| e.code == code).map(|e| e.name.as_str())
    }

    /// Every entry.
    pub fn iter(&self) -> std::slice::Iter<'_, ErrorCodeEntry> {
        self.entries.iter()
    }

    /// How many entries.
    #[allow(clippy::len_without_is_empty)]
    pub fn len(&self) -> usize {
        self.entries.len()
    }
}

impl<'a> IntoIterator for &'a ErrorCodeTable {
    type Item = &'a ErrorCodeEntry;
    type IntoIter = std::slice::Iter<'a, ErrorCodeEntry>;

    fn into_iter(self) -> Self::IntoIter {
        self.entries.iter()
    }
}
