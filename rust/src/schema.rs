//! A compiled schema, and the two entry points that answer "what would this
//! table do with this row?".

use std::cell::Cell;
use std::ffi::CString;
use std::marker::PhantomData;
use std::sync::Arc;

use crate::error::{Error, Result};
use crate::ffi::{ChsBlock, ChsFilter, ChsSchema, EXPORT_NONE, cstring};
use crate::json::quote_bare_denormals;
use crate::library::{Column, Library};
use crate::result::{
    BatchDoc, BatchResult, DocFlags, FilterDoc, FilterResult, Format, RowDoc, RowResult,
    batch_result_of, filter_result_of, row_result_of,
};

/// An empty settings map, for the common call with no per-request settings.
pub const NO_SETTINGS: &[(&str, &str)] = &[];

/// An empty query-parameter map, for the common [`Schema::compile_filter`]
/// call with no `{name:Type}` parameters. Positionally, like the settings
/// slice [`Schema::rows`] and [`Schema::set_engine`] already take — the
/// crate's one-optional-parameter convention (`docs/reference/bindings.md` §One compile
/// function).
pub const NO_PARAMS: &[(&str, &str)] = &[];

/// Pin the batch instant outright — tests, replay, anything that must be
/// reproducible. The value is a 19-digit nanosecond epoch and **must** cross as a
/// string: as a JSON number through a float it becomes `1.7e+18` and the setting
/// is silently ignored.
pub const SETTING_NOW_EPOCH_NANOS: &str = "chtypes_now_epoch_nanos";
/// The caller's measured `(server - client)` offset, added to every clock read.
pub const SETTING_CLOCK_OFFSET_NANOS: &str = "chtypes_clock_offset_nanos";
/// Refuse to substitute a volatile DEFAULT when `|offset|` exceeds this; `0`
/// means no budget, and with none of these set the tolerated skew is unbounded.
pub const SETTING_MAX_CLOCK_SKEW_NANOS: &str = "chtypes_max_clock_skew_nanos";
/// Admission ceiling on DEFAULT/TTL evaluation memory. Process-wide: settable
/// only through [`Library::set_default_settings`].
pub const SETTING_DEFAULT_EVAL_MEMORY_BYTES: &str = "chtypes_default_eval_memory_bytes";
/// Admission ceiling on DEFAULT/TTL evaluation wall time. Process-wide, as above.
pub const SETTING_DEFAULT_EVAL_WALL_NANOS: &str = "chtypes_default_eval_wall_nanos";

/// A schema compiled inside one specific version's library.
///
/// # Thread-safety
///
/// `chtypes.h`: *"The library is thread-safe for concurrent `chs_row()` calls on
/// distinct handles; a single handle must not be used from two threads at once."*
/// That is encoded here in the type system: `Schema` is [`Send`] — a handle may
/// be moved to another thread, which is what an executor needs — and deliberately
/// **not** [`Sync`], so the compiler rejects sharing one handle between threads
/// rather than leaving it to a comment. Concurrency across versions, or across
/// schemas of one version, is expressed by making more schemas.
pub struct Schema {
    lib: Arc<Library>,
    handle: *mut ChsSchema,
    columns: Vec<Column>,
    /// `Cell` is `Send` and `!Sync`: it states the intent the raw pointer already
    /// implies, so removing the pointer later cannot silently make `Schema` sync.
    _not_sync: PhantomData<Cell<()>>,
}

// SAFETY: the handle is owned exclusively by this Schema (compile returns a fresh
// one and Drop is the only release), and every call into the library takes the
// library's mutex, so moving the handle to another thread cannot produce a
// concurrent use of it. `Sync` is deliberately NOT implemented: two threads
// holding &Schema could call chs_row on one handle at once, which the header
// forbids.
unsafe impl Send for Schema {}

impl Schema {
    pub(crate) fn new(lib: Arc<Library>, handle: *mut ChsSchema, columns: Vec<Column>) -> Schema {
        Schema {
            lib,
            handle,
            columns,
            _not_sync: PhantomData,
        }
    }

    /// The declared columns, canonicalised by this build. Empty when the artifact
    /// predates the column-introspection group (which shipped all-or-nothing).
    pub fn columns(&self) -> &[Column] {
        &self.columns
    }

    /// The library this schema was compiled in.
    pub fn library(&self) -> &Arc<Library> {
        &self.lib
    }

    /// Declare the table's engine so [`Schema::rows`] applies the engine's own
    /// insert-time semantics — the single-block merge every INSERT runs under
    /// `optimize_on_insert = 1`: `CollapsingMergeTree` refusing an invalid `Sign`
    /// with code 117 before anything is stored, `SummingMergeTree` summing equal
    /// keys and dropping all-zero rows, `ReplacingMergeTree` deduplicating within
    /// the block.
    ///
    /// `engine` is the `SHOW CREATE` spelling (`"CollapsingMergeTree(sign)"`);
    /// `order_by` the sorting key (`"tuple()"`, `"id"`, `"(day, key)"`);
    /// `merge_tree_settings` is the table's **MergeTree-namespace** settings —
    /// the `SETTINGS` clause after the engine (`allow_nullable_key` and
    /// friends), a namespace `DB::Settings` cannot carry. Pass
    /// [`NO_SETTINGS`] for none.
    ///
    /// Any engine or sorting key this build does not model — and any artifact
    /// that predates the entry point — is [`crate::Error::Unsupported`], never
    /// a guess and never a load failure.
    ///
    /// `merge_tree_settings` names are validated by the server's own
    /// `MergeTreeSettings` object: an unknown name answers the server's code
    /// `115` ([`crate::Error::Schema`], code visible, so a caller can tell
    /// "bad name" from "not modeled"). A known name declared at a
    /// **non-default** value is refused ([`crate::Error::Unsupported`],
    /// naming it) — no MergeTree setting's behavior is modeled yet, and
    /// silently ignoring a declared value would mean the declared profile is
    /// not in force. A name declared **at** its default is inert and
    /// accepted. Values cross as strings, as everywhere on this boundary.
    ///
    /// # Errors
    ///
    /// Two KINDS of failure travel through one return, told apart by the C
    /// return's **sign** (`docs/reference/bindings.md` rule 12) — never flatten them:
    ///
    /// * [`crate::Error::Schema`] — positive code: **the server refused**.
    ///   This DDL can never exist and the tenant must be told. Today `115`
    ///   (unknown MergeTree setting name) is the only positive code here.
    /// * [`crate::Error::Unsupported`] — negative code: **this library
    ///   declined** (`-2` unmodelled engine or sorting key, or a non-default
    ///   declared MergeTree setting value; `-1` a guarded exception). Validate
    ///   cautiously — a real server might have accepted it.
    /// * [`crate::Error::PredatesFeature`] — the artifact predates
    ///   `chs_schema_engine`; also a decline, never a load failure.
    /// * [`crate::Error::Nul`] — an argument contained an interior NUL byte.
    pub fn set_engine<K: AsRef<str>, V: AsRef<str>>(
        &mut self,
        engine: &str,
        order_by: &str,
        merge_tree_settings: &[(K, V)],
    ) -> Result<()> {
        let e = cstring(engine, "engine")?;
        let o = cstring(order_by, "order by")?;
        let mt = settings_json(merge_tree_settings)?;
        let _guard = self.lib.lock();
        // SAFETY: our own handle, under the library lock.
        unsafe { self.lib.api().engine(self.handle, &e, &o, &mt) }
    }

    /// Declare the table's rows TTL — the `TTL ...` clause after the engine, e.g.
    /// `"ts + INTERVAL 30 DAY"`. Evaluated per batch against the batch's one
    /// clock instant with `force=true`, which is `OPTIMIZE FINAL`'s posture and
    /// how the acceptance rig captures its ground truth.
    ///
    /// An expired row is reported **not stored**, as a batch-level
    /// `ttl_expired` transform. `WHERE`/`GROUP BY` TTLs, `TO DISK`/`VOLUME`
    /// moves, `RECOMPRESS` and any clock-reading TTL expression are
    /// [`crate::Error::Unsupported`]. Column-level TTLs need no call: they are part of
    /// the declaration list and are captured at compile time.
    ///
    /// The TTL is validated under the handle's declared compile profile when
    /// it has one, the process defaults otherwise — the handle IS the CREATE,
    /// and a `TTL` clause is validated by the CREATE, which is why this call
    /// takes no settings parameter.
    ///
    /// # Errors
    ///
    /// * [`crate::Error::Unsupported`] — any nonzero C return is a decline:
    ///   `-2` for a refused TTL form, `-1` for a guarded exception. Never a
    ///   rejection; a real server might have accepted the clause.
    /// * [`crate::Error::PredatesFeature`] — the artifact predates
    ///   `chs_schema_ttl`.
    /// * [`crate::Error::Nul`] — the clause contained an interior NUL byte.
    pub fn set_ttl(&mut self, ttl_sql: &str) -> Result<()> {
        let t = cstring(ttl_sql, "ttl")?;
        let _guard = self.lib.lock();
        // SAFETY: our own handle, under the library lock.
        unsafe { self.lib.api().ttl(self.handle, &t) }
    }

    /// Validate and coerce one row, with no per-request settings —
    /// [`Schema::row_with_settings`] with an empty map. Same errors.
    pub fn row(&self, format: Format, raw: &[u8]) -> Result<RowResult> {
        self.row_with_settings(format, raw, NO_SETTINGS)
    }

    /// Validate and coerce one row.
    ///
    /// `raw` is passed counted, never as text: binary formats contain NUL bytes
    /// and text rows can carry invalid UTF-8 on purpose. Settings values cross as
    /// strings — see [`SETTING_NOW_EPOCH_NANOS`].
    ///
    /// **The verdict is in the `Ok` value, not the `Err`.** A row the server
    /// would reject comes back `Ok` with [`crate::Outcome::Rejected`] and
    /// ClickHouse's code in [`RowResult::err_code`]; a decline is
    /// [`crate::Outcome::Unsupported`]; an unknown setting name rejects the
    /// call with the server's own `115` — also in the result, not the `Err`.
    /// The `Err` arm is reserved for the machinery failing to ask at all.
    ///
    /// # Errors
    ///
    /// * [`crate::Error::PredatesFeature`] — the artifact predates `chs_row`.
    /// * [`crate::Error::BadDocument`] — the result document could not be
    ///   read exactly, even after the bare-denormal repair.
    /// * [`crate::Error::Nul`] — a setting contained an interior NUL byte.
    pub fn row_with_settings<K: AsRef<str>, V: AsRef<str>>(
        &self,
        format: Format,
        raw: &[u8],
        settings: &[(K, V)],
    ) -> Result<RowResult> {
        let json = settings_json(settings)?;
        let doc: RowDoc = {
            let _guard = self.lib.lock();
            // SAFETY: our own handle, under the library lock; the byte slice
            // outlives the call.
            let out = unsafe { self.lib.api().row(self.handle, format.code(), raw, &json)? };
            parse_row_doc(&out)?
        };
        Ok(row_result_of(doc))
    }

    /// Validate and coerce a whole request body, which may hold many rows.
    ///
    /// This is **not** [`Schema::row`] in a loop and must not be implemented as
    /// one: row separation is format-specific (a quoted CSV field can contain a
    /// newline) and `input_format_allow_errors_num` / `_ratio` decide whether a
    /// bad row is skipped or aborts the batch. It is also the unit of the
    /// volatile-DEFAULT clock guarantee — one batch is one clock instant.
    ///
    /// When [`BatchResult::engine_rows`] is present it, not
    /// [`BatchResult::rows`], is what the table will hold.
    ///
    /// **The verdict is in the `Ok` value, not the `Err`** — see
    /// [`Schema::row_with_settings`]. The batch verdict is
    /// [`BatchResult::outcome`] / [`BatchResult::err_code`].
    ///
    /// # Errors
    ///
    /// * [`crate::Error::PredatesFeature`] — the artifact predates `chs_rows`
    ///   (unreachable for an artifact this crate loaded: the symbol is
    ///   mandatory).
    /// * [`crate::Error::BadDocument`] — the result document could not be
    ///   read exactly, even after the bare-denormal repair.
    /// * [`crate::Error::Nul`] — a setting contained an interior NUL byte.
    pub fn rows<K: AsRef<str>, V: AsRef<str>>(
        &self,
        format: Format,
        body: &[u8],
        settings: &[(K, V)],
    ) -> Result<BatchResult> {
        // export off, all document groups on: the revision-3 pass-through
        // that keeps rows() byte-identical to revision 2.
        self.rows_through(format, body, settings, EXPORT_NONE, DocFlags::ALL)
    }

    /// [`Schema::rows`] with the revision-3 export and document-flag channels
    /// exposed: ONE `chs_rows` call, never a second, never re-parsing
    /// (`docs/proposals/rows-export.md`; `docs/reference/c-abi.md` §Rows is normative).
    ///
    /// `export` is `None` (no bytes; `doc_flags` still thins the document) or
    /// `Some(format)` for a [`Format`] this artifact can SERIALIZE — this
    /// revision exactly [`Format::JsonCompactEachRow`]. Any other value
    /// answers the whole call [`crate::Outcome::Unsupported`] and processes
    /// nothing — loud, never silent.
    ///
    /// `doc_flags` selects the document groups; [`DocFlags::NONE`] (the
    /// `Default`) is the LEAN document — verdicts intact, but `values`,
    /// `transformed`, `substituted`, `computed` and `unknown_fields` all come
    /// back empty. [`Schema::rows`] is the [`DocFlags::ALL`] spelling.
    ///
    /// The exported bytes come back in [`BatchResult::payload`] with
    /// [`BatchResult::spans`] index-aligned to [`BatchResult::rows`] —
    /// `payload[s.off..s.off + s.len]` IS row i's line — and the
    /// emitted-empty versus declined distinction is `Some(empty)` versus
    /// `None` + [`BatchResult::export_declined`]. The C buffer is copied and
    /// freed (same library's `chs_free`) before this returns; no ownership
    /// crosses the boundary.
    ///
    /// Same errors as [`Schema::rows`] — the verdict is in the `Ok` value.
    pub fn rows_export<K: AsRef<str>, V: AsRef<str>>(
        &self,
        format: Format,
        body: &[u8],
        settings: &[(K, V)],
        export: Option<Format>,
        doc_flags: DocFlags,
    ) -> Result<BatchResult> {
        let export_code = export.map_or(EXPORT_NONE, Format::code);
        self.rows_through(format, body, settings, export_code, doc_flags)
    }

    /// The ONE `chs_rows` call site — [`Schema::rows`] and
    /// [`Schema::rows_export`] are both single invocations of it.
    fn rows_through<K: AsRef<str>, V: AsRef<str>>(
        &self,
        format: Format,
        body: &[u8],
        settings: &[(K, V)],
        export_code: i32,
        doc_flags: DocFlags,
    ) -> Result<BatchResult> {
        let json = settings_json(settings)?;
        let (doc, payload): (BatchDoc, Option<Vec<u8>>) = {
            let _guard = self.lib.lock();
            // SAFETY: our own handle, under the library lock; the byte slice
            // outlives the call.
            let (out, payload) = unsafe {
                self.lib.api().rows(
                    self.handle,
                    format.code(),
                    body,
                    &json,
                    export_code,
                    doc_flags.bits(),
                )?
            };
            (parse_batch_doc(&out)?, payload)
        };
        let mut res = batch_result_of(doc);
        res.payload = payload;
        Ok(res)
    }

    /// Compile one boolean SQL expression over this schema's PHYSICAL columns
    /// (`chs_filter_compile`; `docs/reference/c-abi.md` §Filters is the full contract) —
    /// the same `TreeRewriter` + `ExpressionAnalyzer` pipeline the
    /// CONSTRAINT CHECK path runs, so comparison semantics are WHERE-side by
    /// construction: `x = 256` over `UInt8` promotes (false for every row),
    /// it never wraps. Naming an `ALIAS`/`EPHEMERAL` column fails with
    /// ClickHouse's own `UNKNOWN_IDENTIFIER`, exactly where a real CREATE
    /// fails.
    ///
    /// # Query parameters (revision 4)
    ///
    /// The expression may contain `{name:Type}` query parameters, bound by
    /// `params` — name → value **string** pairs, positionally like every
    /// settings slice in this crate ([`NO_PARAMS`] for none). Substitution
    /// is the server's own `ReplaceQueryParameterVisitor`, run before
    /// analysis exactly where a real server runs it: each value is
    /// deserialized by the DECLARED type's own reader and injected as a
    /// typed literal AFTER SQL parsing, so a value is never SQL text and
    /// NEVER needs hand-escaping — injection safety is by construction, not
    /// by escaping. Do not render values into the expression yourself.
    ///
    /// CHOOSE THE BRACE TYPE FOR THE VALUE'S DOMAIN: the declared type's
    /// own reader WRAPS an out-of-domain integer — `{p:UInt8}` given `"256"`
    /// binds `0` and matches every genuine zero (measured, uniform
    /// 24.8-26.7) — while the same constant as a literal PROMOTES
    /// (`x = 256` is never true). Size the brace type for the
    /// tenant-supplied domain or validate the value first; malformed
    /// spellings refuse loudly (457 for `"-1"`/`"+7"`/`"007"` as `UInt8`,
    /// 32 for `""`). A name bound twice takes the LAST binding — the
    /// server's own `insert_or_assign` rule (REACHABLE here: this is a
    /// slice of pairs, and the last pair wins).
    ///
    /// The compiled handle bakes the values in: identity is per
    /// (schema, expr, params), so changing a value means compiling a new
    /// `Filter`. A caller compiling filters from tenant-influenced values
    /// MUST bound its cache (an LRU keyed on schema generation + expr +
    /// params-hash) and its compile rate per principal — the key is
    /// attacker-influencable, so an unbounded cache is a memory DoS and an
    /// unmetered compile path is a CPU DoS (`docs/reference/c-abi.md` §Filters).
    ///
    /// # Lifetime
    ///
    /// The returned [`Filter`] BORROWS this schema, which is the C layer's
    /// lifetime rule made structural: a `chs_filter` references its
    /// `chs_schema` without a refcount, filters must be freed before their
    /// schema, and here the borrow checker enforces both — a `Schema` cannot
    /// be dropped (or mutated via `set_engine`/`set_ttl`) while a `Filter`
    /// on it is alive, and `Filter`'s `Drop` runs first by construction.
    /// A filter answers for THIS handle: recompile filters when the schema
    /// is recompiled. Freeing the schema first is not a runtime error — it
    /// does not compile:
    ///
    /// ```compile_fail
    /// # use chtypes::{Format, Registry, NO_PARAMS, NO_SETTINGS};
    /// # fn main() -> Result<(), Box<dyn std::error::Error>> {
    /// # let lib = Registry::from_env_or_default()?.for_version("25.8")?;
    /// let schema = lib.compile("x UInt8").compile()?;
    /// let filter = schema.compile_filter("x = 1", NO_PARAMS)?;
    /// drop(schema); // ERROR: `schema` is borrowed by `filter`
    /// filter.rows(Format::JsonEachRow, b"{\"x\":1}\n", NO_SETTINGS)?;
    /// # Ok(()) }
    /// ```
    ///
    /// # Errors
    ///
    /// Rule 12's split (`docs/reference/bindings.md`):
    ///
    /// * [`crate::Error::Schema`] — ClickHouse itself refuses the expression:
    ///   unknown identifier (47), unknown function, a `NO_COMMON_TYPE` the
    ///   analyzer raises — and, since revision 4, the server's own parameter
    ///   refusals: an UNBOUND `{name:Type}` is **456** UNKNOWN_QUERY_PARAMETER
    ///   ("Substitution `name` is not set"), a value the declared type cannot
    ///   parse completely is **457** BAD_QUERY_PARAMETER — the server's own
    ///   code and message, verbatim. A bound name the expression never uses
    ///   is ignored, as a live server ignores an unused `param_*`.
    /// * [`crate::Error::Unsupported`] — this build declines: a
    ///   non-deterministic expression (clock reads — `now() > ts` —, `rand()`,
    ///   server-constants, stateful functions; the scan runs AFTER
    ///   substitution, so a parameter value can never smuggle one in).
    /// * [`crate::Error::PredatesFeature`] — the artifact predates the filter
    ///   trio; a decline at call time, never a load failure.
    /// * [`crate::Error::Nul`] — the expression contained an interior NUL.
    pub fn compile_filter<K: AsRef<str>, V: AsRef<str>>(
        &self,
        expr_sql: &str,
        params: &[(K, V)],
    ) -> Result<Filter<'_>> {
        let e = cstring(expr_sql, "filter expression")?;
        let p = settings_json(params)?;
        let _guard = self.lib.lock();
        // SAFETY: our own handle, under the library lock; the returned filter
        // handle is owned by the Filter below, whose borrow of self keeps the
        // schema handle alive for its whole life.
        let handle = unsafe { self.lib.api().filter_compile(self.handle, &e, &p)? };
        Ok(Filter {
            schema: self,
            handle,
            _not_sync: PhantomData,
        })
    }

    /// Parse a body ONCE into a [`Block`] (`chs_block_parse`) — the parse
    /// half of [`Filter::rows`], exported so K filters can evaluate one
    /// event with no re-parse ([`Filter::eval`]; `docs/reference/c-abi.md` §Blocks).
    /// Same formats and settings contract as [`Schema::rows`] (`settings` is
    /// the PARSE-side map: format settings, clock keys; evaluation takes
    /// none). Volatile DEFAULTs resolve against THIS call's clock instant,
    /// so `filter.eval(&schema.parse_block(...)?)` ≡ `filter.rows(...)`
    /// exactly when the clock is pinned ([`SETTING_NOW_EPOCH_NANOS`]) or the
    /// schema has no volatile DEFAULT.
    ///
    /// Per-row parse failures do NOT error — they are recorded IN the block
    /// and answer [`crate::Verdict::Decline`] from every filter, with the
    /// recorded error.
    ///
    /// # Lifetime
    ///
    /// The returned [`Block`] BORROWS this schema exactly as a [`Filter`]
    /// does: the borrow checker rejects dropping (or mutating) the schema
    /// while a block on it is alive, and `Block`'s `Drop` runs first by
    /// construction — the C-required free order, enforced at compile time.
    ///
    /// # Errors
    ///
    /// A call-level failure — an unknown setting's 115
    /// ([`crate::Error::Schema`]), an unsplittable body, a binary decode
    /// fault, the deferred JSONEachRow framing verdict — yields NO block and
    /// no partial answers. [`crate::Error::PredatesFeature`] when the
    /// artifact predates the block twin.
    pub fn parse_block<K: AsRef<str>, V: AsRef<str>>(
        &self,
        format: Format,
        body: &[u8],
        settings: &[(K, V)],
    ) -> Result<Block<'_>> {
        let json = settings_json(settings)?;
        let _guard = self.lib.lock();
        // SAFETY: our own handle, under the library lock; the returned block
        // handle is owned by the Block below, whose borrow of self keeps the
        // schema handle alive for its whole life.
        let handle = unsafe {
            self.lib
                .api()
                .block_parse(self.handle, format.code(), body, &json)?
        };
        Ok(Block {
            schema: self,
            handle,
            _not_sync: PhantomData,
        })
    }
}

/// One boolean SQL expression compiled against a [`Schema`]'s columns —
/// see [`Schema::compile_filter`] for the compile contract and the lifetime
/// argument (the borrow IS the free-order enforcement).
///
/// # Thread-safety
///
/// `chtypes.h`, verbatim: *"one chs_filter must not be used from two threads
/// at once, and a chs_filter call is ALSO a use of its schema handle"* — two
/// filters over ONE schema must not run concurrently either. The type system
/// already forbids all of it: `Filter` borrows its `Schema`, and `Schema` is
/// `!Sync`, so neither the filter nor its schema can be shared across
/// threads in the first place. Every call additionally takes the library's
/// own mutex, the crate's standing discipline.
///
/// # Enforcement gate
///
/// NOTHING may enforce read-side security on this surface until the
/// WHERE-truth rig gates green (zero over-admit, zero over-hide); until then
/// it is a shadow/replay surface (`docs/reference/c-abi.md` §Filters). In particular:
/// [`crate::Verdict::Error`] and [`crate::Verdict::Decline`] are NOT
/// answers, and an enforcing caller MUST fail closed on both.
pub struct Filter<'s> {
    schema: &'s Schema,
    handle: *mut ChsFilter,
    /// Same statement of intent as [`Schema`]: the raw pointer already makes
    /// this `!Sync`; the marker keeps it that way if the pointer ever moves.
    _not_sync: PhantomData<Cell<()>>,
}

impl Filter<'_> {
    /// The schema this filter was compiled against.
    pub fn schema(&self) -> &Schema {
        self.schema
    }

    /// Evaluate the filter over a body of rows (`chs_filter_rows`) — same
    /// formats and settings contract as [`Schema::rows`], one C call. Rows
    /// are evaluated INDEPENDENTLY (there is no INSERT to abort):
    /// `input_format_allow_errors_*` does not apply, a bad text row declines
    /// ([`crate::Verdict::Decline`], itemized) and the tail resyncs so
    /// verdict indexes keep matching input rows, and volatile DEFAULTs
    /// resolve against one clock instant per call.
    ///
    /// **The verdict is in the `Ok` value, not the `Err`** — the call-level
    /// verdict is [`FilterResult::outcome`] (an unknown setting name's 115
    /// answers [`crate::FilterOutcome::Rejected`] with empty verdicts: a
    /// malformed body yields no partial answers). The `Err` arm is the
    /// machinery: [`crate::Error::BadDocument`], [`crate::Error::Nul`],
    /// [`crate::Error::PredatesFeature`].
    pub fn rows<K: AsRef<str>, V: AsRef<str>>(
        &self,
        format: Format,
        body: &[u8],
        settings: &[(K, V)],
    ) -> Result<FilterResult> {
        let json = settings_json(settings)?;
        let doc: FilterDoc = {
            let _guard = self.schema.lib.lock();
            // SAFETY: our own filter handle; its schema handle is alive for
            // our whole life (we borrow the Schema); under the library lock.
            let out = unsafe {
                self.schema
                    .lib
                    .api()
                    .filter_rows(self.handle, format.code(), body, &json)?
            };
            crate::doc::filter_doc(&quote_bare_denormals(&out))?
        };
        Ok(filter_result_of(doc))
    }

    /// Evaluate this filter over an already-parsed [`Block`]
    /// (`chs_filter_eval`) — the SAME result document [`Filter::rows`]
    /// returns: same [`FilterResult`] fields, same verdicts, same `errors`
    /// rule (a row the parse recorded as unparseable answers
    /// [`crate::Verdict::Decline`] with the recorded error). Evaluation is a
    /// pure function of (filter, block): no settings, and the block is
    /// neither consumed nor mutated, so one block can be evaluated by K
    /// filters sequentially with no re-parse — the live-SSE call shape.
    ///
    /// Filter and block MUST come from the SAME schema handle: a mismatched
    /// pair from two schemas of ONE library answers a REJECTED result (code
    /// 1002) — the C layer's loud refusal, never undefined behavior. A pair
    /// from two different LIBRARIES is [`crate::Error::CrossLibrary`]: no
    /// handle ever crosses a `dlopen`'d image boundary.
    ///
    /// # Errors
    ///
    /// [`crate::Error::CrossLibrary`], [`crate::Error::BadDocument`],
    /// [`crate::Error::PredatesFeature`]. The verdict is in the `Ok` value.
    pub fn eval(&self, block: &Block<'_>) -> Result<FilterResult> {
        if !Arc::ptr_eq(&self.schema.lib, &block.schema.lib) {
            return Err(Error::CrossLibrary {
                filter_version: self.schema.lib.version().to_string(),
                block_version: block.schema.lib.version().to_string(),
            });
        }
        let doc: FilterDoc = {
            let _guard = self.schema.lib.lock();
            // SAFETY: our own filter handle and the block's own handle, both
            // from this library; both schema handles are alive (each object
            // borrows its Schema); under the library lock.
            let out = unsafe {
                self.schema
                    .lib
                    .api()
                    .filter_eval(self.handle, block.handle)?
            };
            crate::doc::filter_doc(&quote_bare_denormals(&out))?
        };
        Ok(filter_result_of(doc))
    }
}

impl Drop for Filter<'_> {
    fn drop(&mut self) {
        let _guard = self.schema.lib.lock();
        // SAFETY: our own filter handle, released exactly once, under the
        // library lock; the borrowed schema is still alive by construction.
        unsafe { self.schema.lib.api().filter_free(self.handle) }
    }
}

/// One body, parsed ONCE under one schema handle and one clock instant —
/// see [`Schema::parse_block`] for the parse contract and [`Filter::eval`]
/// for the evaluation side. The borrow IS the free-order enforcement,
/// exactly as for [`Filter`]: a `Block` cannot outlive its `Schema`, and its
/// `Drop` runs first by construction.
///
/// # Thread-safety
///
/// `chtypes.h`, verbatim: one `chs_block` *"must not be used from two
/// threads at once"*, and an eval is a use of BOTH handles. The type system
/// already forbids all of it — `Block` borrows its `Schema`, which is
/// `!Sync` — and every call additionally takes the library's own mutex.
pub struct Block<'s> {
    schema: &'s Schema,
    handle: *mut ChsBlock,
    /// Same statement of intent as [`Schema`]: the raw pointer already makes
    /// this `!Sync`; the marker keeps it that way if the pointer ever moves.
    _not_sync: PhantomData<Cell<()>>,
}

impl Block<'_> {
    /// The schema this block was parsed under.
    pub fn schema(&self) -> &Schema {
        self.schema
    }
}

impl Drop for Block<'_> {
    fn drop(&mut self) {
        let _guard = self.schema.lib.lock();
        // SAFETY: our own block handle, released exactly once, under the
        // library lock; the borrowed schema is still alive by construction.
        unsafe { self.schema.lib.api().block_free(self.handle) }
    }
}

impl std::fmt::Debug for Block<'_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Block")
            .field("version", &self.schema.lib.version())
            .finish()
    }
}

impl std::fmt::Debug for Filter<'_> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Filter")
            .field("version", &self.schema.lib.version())
            .finish()
    }
}

impl Drop for Schema {
    fn drop(&mut self) {
        let _guard = self.lib.lock();
        // SAFETY: our own handle, released exactly once, under the library lock.
        unsafe { self.lib.api().schema_free(self.handle) }
    }
}

impl std::fmt::Debug for Schema {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Schema")
            .field("version", &self.lib.version())
            .field("columns", &self.columns.len())
            .finish()
    }
}

/// Build the `settings_json` object. **Every value crosses as a JSON string**:
/// `chtypes_now_epoch_nanos` is a 19-digit nanosecond epoch that does not survive
/// an IEEE double, and as a JSON number it is silently ignored. Nothing here ever
/// touches a float.
pub(crate) fn settings_json<K: AsRef<str>, V: AsRef<str>>(settings: &[(K, V)]) -> Result<CString> {
    if settings.is_empty() {
        return cstring("{}", "settings");
    }
    let mut map = serde_json::Map::with_capacity(settings.len());
    for (k, v) in settings {
        map.insert(
            k.as_ref().to_string(),
            serde_json::Value::String(v.as_ref().to_string()),
        );
    }
    cstring(&serde_json::Value::Object(map).to_string(), "settings")
}

/// Repair ClickHouse's bare denormals — `inf` / `-inf` / `nan` are faithful
/// ClickHouse output and not valid JSON — and read the document over **bytes**.
///
/// There is deliberately no lossy fallback. A result document holding a
/// non-UTF-8 `String` value is not valid UTF-8, and the previous retry through
/// `String::from_utf8_lossy` destroyed exactly the bytes `docs/reference/c-abi.md` calls
/// authoritative. `crate::doc` reads bytes, so the repair is unnecessary; a
/// document it cannot read is [`Error::BadDocument`], never an approximation.
fn parse_row_doc(bytes: &[u8]) -> Result<RowDoc> {
    crate::doc::row_doc(&quote_bare_denormals(bytes))
}

/// [`parse_row_doc`] for a batch document.
fn parse_batch_doc(bytes: &[u8]) -> Result<BatchDoc> {
    crate::doc::batch_doc(&quote_bare_denormals(bytes))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn settings_values_are_always_json_strings() {
        let json = settings_json(&[(SETTING_NOW_EPOCH_NANOS, "1700000000000000000")]).unwrap();
        assert_eq!(
            json.to_str().unwrap(),
            r#"{"chtypes_now_epoch_nanos":"1700000000000000000"}"#
        );
        assert_eq!(settings_json(NO_SETTINGS).unwrap().to_str().unwrap(), "{}");
        // Owned strings work too, so a caller can build the map at runtime.
        let owned = vec![("input_format_null_as_default".to_string(), "0".to_string())];
        assert_eq!(
            settings_json(&owned).unwrap().to_str().unwrap(),
            r#"{"input_format_null_as_default":"0"}"#
        );
    }

    #[test]
    fn a_nanosecond_epoch_survives_verbatim() {
        // The exact 19 digits must appear; a float round-trip would print
        // 1.7000000001234568e18 and the setting would be silently ignored.
        let json = settings_json(&[(SETTING_NOW_EPOCH_NANOS, "1700000000123456789")]).unwrap();
        assert!(json.to_str().unwrap().contains("1700000000123456789"));
    }

    #[test]
    fn documents_with_bare_denormals_still_parse() {
        let doc = parse_row_doc(
            br#"{"outcome":"accepted","cols":[{"name":"f","base":"Float64","src":"input","stored":inf}]}"#,
        )
        .unwrap();
        assert_eq!(doc.cols[0].stored_raw(), "\"inf\"");
    }

    #[test]
    fn a_document_holding_non_utf8_bytes_is_read_exactly_not_repaired() {
        // The one case that used to take the lossy fallback. `x String` fed the
        // bytes 0xC3 0x28 renders as those bytes inside a JSON string.
        let mut doc: Vec<u8> =
            br#"{"outcome":"accepted","cols":[{"name":"x","base":"String","src":"input","stored":""#
                .to_vec();
        doc.extend_from_slice(&[0xc3, b'(']);
        doc.extend_from_slice(br#""}]}"#);
        let parsed = parse_row_doc(&doc).unwrap();
        assert_eq!(
            parsed.cols[0].stored_raw().as_bytes(),
            [b'"', 0xc3, b'(', b'"']
        );
    }
}
