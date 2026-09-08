//! The raw `chs_*` boundary: `dlopen` with the flags the spec mandates, the
//! symbol table, and the single place `chs_free` is ever called.
//!
//! # Safety invariants this module owns
//!
//! * **`RTLD_NOW | RTLD_LOCAL`, explicitly.** `RTLD_LOCAL` is the entire
//!   mechanism by which two builds that both define `DB::DataTypeFactory` live
//!   in one process. A loader that uses `RTLD_GLOBAL` appears to work and then
//!   answers with the wrong version's semantics. `libloading`'s own
//!   `Library::new` opens with `RTLD_LAZY | RTLD_LOCAL`, so this module calls
//!   `os::unix::Library::open` with the flags spelled out rather than inheriting
//!   a default that could change.
//! * **Nothing is ever `dlclose`d.** `chs_init` deliberately leaks ClickHouse's
//!   global `Context` (its destructor tears down `AccessControl` after Poco's
//!   logger is gone — measured as a process exiting 139 *after* printing every
//!   correct answer), and it registers `chs_shutdown` with `atexit`. Unloading a
//!   library whose threads and statics outlive it is therefore not safe, so the
//!   handle is held in a `ManuallyDrop` and the OS reclaims it with the process.
//!   [`Api::shutdown`] is exposed for a host that controls its own teardown.
//! * **Every `char *` is copied and released with the *same library's*
//!   `chs_free`.** [`Api::take`] is the only function in this crate that calls
//!   `chs_free`, and it only ever calls the one resolved from the same `dlopen`.
//! * **Callers hold the library lock.** The functions here do no locking; the
//!   `Library` wrapper serialises every call, including `chs_schema_compile` and
//!   `chs_free`, exactly as the reference implementation's `dlopen` path does.

use std::ffi::{CStr, CString, c_char, c_int};
use std::mem::ManuallyDrop;
use std::path::Path;

use libloading::os::unix::{Library as UnixLibrary, RTLD_LOCAL, RTLD_NOW, Symbol};

use crate::error::{ABI_REVISION, Error, Result};

/// An opaque `chs_schema *`.
#[repr(C)]
pub(crate) struct ChsSchema {
    _opaque: [u8; 0],
}

/// An opaque `chs_filter *` (revision 3).
#[repr(C)]
pub(crate) struct ChsFilter {
    _opaque: [u8; 0],
}

/// An opaque `chs_block *` (revision 4).
#[repr(C)]
pub(crate) struct ChsBlock {
    _opaque: [u8; 0],
}

/// `chs_bytes` — the `chs_rows` export channel's out-param. `data` is
/// malloc'd by the library and NOT NUL-terminated by contract (the length is
/// the contract), so it is copied counted and freed with the same library's
/// `chs_free`, never read as a C string.
#[repr(C)]
struct ChsBytes {
    data: *mut c_char,
    len: usize,
}

/// `CHS_EXPORT_NONE` — the "no export requested" sentinel `chs_rows` takes.
pub(crate) const EXPORT_NONE: c_int = -1;

type FnVersion = unsafe extern "C" fn() -> *const c_char;
type FnAbiRevision = unsafe extern "C" fn() -> c_int;
type FnInit = unsafe extern "C" fn(*const c_char, *const c_char, *mut *mut c_char) -> c_int;
type FnSetDefaults = unsafe extern "C" fn(*const c_char, *mut *mut c_char) -> c_int;
type FnFree = unsafe extern "C" fn(*mut c_char);
type FnShutdown = unsafe extern "C" fn();
type FnStr = unsafe extern "C" fn() -> *mut c_char;
type FnValidate =
    unsafe extern "C" fn(*const c_char, *mut *mut c_char, *mut c_int, *mut *mut c_char) -> c_int;
/// `chs_schema_compile` — the consolidated entry point (2026-08-24): DDL,
/// settings profile and mode in one call. There is no separate settings-less
/// signature any more; `NULL`/`"{}"` settings and mode `0` is the whole of
/// the old shape, expressed as arguments rather than as a different symbol.
type FnCompile = unsafe extern "C" fn(
    *const c_char,
    *const c_char,
    c_int,
    *mut c_int,
    *mut *mut c_char,
) -> *mut ChsSchema;
type FnSchemaFree = unsafe extern "C" fn(*mut ChsSchema);
/// `chs_schema_engine` — the consolidated entry point: engine, sorting key
/// and the MergeTree-namespace `SETTINGS` clause in one call. `NULL`/`"{}"`
/// for `merge_tree_settings_json` is the whole of the old engine-only shape.
type FnEngine = unsafe extern "C" fn(
    *mut ChsSchema,
    *const c_char,
    *const c_char,
    *const c_char,
    *mut *mut c_char,
) -> c_int;
type FnTtl = unsafe extern "C" fn(*mut ChsSchema, *const c_char, *mut *mut c_char) -> c_int;
type FnRow = unsafe extern "C" fn(
    *const ChsSchema,
    c_int,
    *const c_char,
    usize,
    *const c_char,
) -> *mut c_char;
/// `chs_rows` at revision 3: the revision-2 shape plus `export_format`,
/// `doc_flags` and the `chs_bytes` out-param. The revision gate in
/// [`Api::open`] is what guarantees an artifact answering revision 3 was
/// built against this exact declaration.
type FnRows = unsafe extern "C" fn(
    *const ChsSchema,
    c_int,
    *const c_char,
    usize,
    *const c_char,
    c_int,
    std::ffi::c_uint,
    *mut ChsBytes,
) -> *mut c_char;
/// `chs_filter_compile` at revision 4: expr, `params_json` (`{name:Type}`
/// query-parameter bindings — a JSON object of name -> value STRING, `"{}"`
/// for none). The revision gate in [`Api::open`] is what guarantees an
/// artifact answering revision 4 was built against this 5-argument shape;
/// the revision-3 4-argument artifact is refused at load.
type FnFilterCompile = unsafe extern "C" fn(
    *const ChsSchema,
    *const c_char,
    *const c_char,
    *mut c_int,
    *mut *mut c_char,
) -> *mut ChsFilter;
type FnFilterFree = unsafe extern "C" fn(*mut ChsFilter);
type FnFilterRows = unsafe extern "C" fn(
    *const ChsFilter,
    c_int,
    *const c_char,
    usize,
    *const c_char,
) -> *mut c_char;
/// The revision-4 block twin: parse a body once (`chs_block_parse`), evaluate
/// K filters against the block (`chs_filter_eval`), free it
/// (`chs_block_free`). `spec/c-abi.md` §Blocks.
type FnBlockParse = unsafe extern "C" fn(
    *const ChsSchema,
    c_int,
    *const c_char,
    usize,
    *const c_char,
    *mut c_int,
    *mut *mut c_char,
) -> *mut ChsBlock;
type FnBlockFree = unsafe extern "C" fn(*mut ChsBlock);
type FnFilterEval = unsafe extern "C" fn(*const ChsFilter, *const ChsBlock) -> *mut c_char;
type FnColCount = unsafe extern "C" fn(*const ChsSchema) -> c_int;
type FnColStr = unsafe extern "C" fn(*const ChsSchema, c_int) -> *const c_char;
type FnColInt = unsafe extern "C" fn(*const ChsSchema, c_int) -> c_int;
type FnReferenceType = unsafe extern "C" fn(*const c_char) -> *mut c_char;

/// The column-introspection group. It shipped as a unit, so it is resolved as a
/// unit: any missing member means the whole group is absent and a schema's
/// column list is left empty rather than partially populated.
struct ColumnApi {
    count: Symbol<FnColCount>,
    name: Symbol<FnColStr>,
    ty: Symbol<FnColStr>,
    default_expr: Symbol<FnColStr>,
    default_kind: Symbol<FnColStr>,
    is_literal: Symbol<FnColInt>,
}

/// One `dlopen`'d artifact's resolved symbols.
pub(crate) struct Api {
    /// Held so the mapping outlives every symbol; never dropped, see module docs.
    _lib: ManuallyDrop<UnixLibrary>,

    // The four mandatory symbols.
    f_version: Symbol<FnVersion>,
    /// The chs_* ABI revision this artifact was built from, or 0 when it
    /// predates the probe. Resolved once at open; a MISMATCHING nonzero
    /// revision never gets this far, because [`Api::open`] refuses it.
    abi_revision: c_int,
    f_init: Symbol<FnInit>,
    f_compile: Symbol<FnCompile>,
    f_rows: Symbol<FnRows>,

    // Everything else is optional: a missing symbol means "this artifact
    // predates the feature" and degrades to unsupported at call time.
    f_free: Option<Symbol<FnFree>>,
    f_schema_free: Option<Symbol<FnSchemaFree>>,
    f_shutdown: Option<Symbol<FnShutdown>>,
    f_set_defaults: Option<Symbol<FnSetDefaults>>,
    f_validate: Option<Symbol<FnValidate>>,
    f_row: Option<Symbol<FnRow>>,
    f_engine: Option<Symbol<FnEngine>>,
    f_ttl: Option<Symbol<FnTtl>>,
    f_reference_type: Option<Symbol<FnReferenceType>>,
    f_registered_families: Option<Symbol<FnStr>>,
    f_function_flags: Option<Symbol<FnStr>>,
    // The revision-3 filter trio shipped as a unit; each is still resolved
    // individually so absence degrades per symbol, never at load.
    f_filter_compile: Option<Symbol<FnFilterCompile>>,
    f_filter_free: Option<Symbol<FnFilterFree>>,
    f_filter_rows: Option<Symbol<FnFilterRows>>,
    // The revision-4 block twin, same rule.
    f_block_parse: Option<Symbol<FnBlockParse>>,
    f_block_free: Option<Symbol<FnBlockFree>>,
    f_filter_eval: Option<Symbol<FnFilterEval>>,
    cols: Option<ColumnApi>,
}

/// One column, as the introspection group reports it.
pub(crate) struct RawColumn {
    pub name: String,
    pub ty: String,
    pub default_kind: String,
    pub default_expr: String,
    pub default_is_literal: bool,
}

impl Api {
    /// `dlopen(path, RTLD_NOW | RTLD_LOCAL)` and resolve the symbol table.
    ///
    /// A library that has a manifest and does not load is broken, not absent, so
    /// this returns an error rather than skipping.
    pub(crate) fn open(path: &Path) -> Result<Api> {
        // SAFETY: dlopen runs the library's initialisers. The artifact is
        // self-contained (it exports only chs_*, keeps libc++ statically inside,
        // and links nothing but libc plus CoreFoundation on macOS), which is
        // what makes loading several of them safe.
        let lib = unsafe { UnixLibrary::open(Some(path), RTLD_NOW | RTLD_LOCAL) }.map_err(|e| {
            Error::Load {
                path: path.to_path_buf(),
                message: e.to_string(),
            }
        })?;

        // SAFETY: each symbol is fetched with the signature declared in
        // include/chtypes.h, which is a frozen ABI.
        unsafe {
            let f_version = require(&lib, b"chs_clickhouse_version\0", path)?;
            let f_init = require(&lib, b"chs_init\0", path)?;
            let f_compile = require(&lib, b"chs_schema_compile\0", path)?;
            let f_rows = require(&lib, b"chs_rows\0", path)?;

            // The ABI identity gate (spec/c-abi.md §ABI identity). Optional:
            // 0 means the artifact predates the probe, which is ignorance and
            // keeps the per-symbol degradation rules. A DIFFERENT nonzero
            // revision positively states that these declarations do not
            // describe this artifact, so calling through them is undefined.
            let abi_revision = optional::<FnAbiRevision>(&lib, b"chs_abi_revision\0")
                .map(|f| f())
                .unwrap_or(0);
            if abi_revision != 0 && abi_revision != ABI_REVISION {
                return Err(Error::Load {
                    path: path.to_path_buf(),
                    message: format!(
                        "artifact reports ABI revision {abi_revision}, this crate speaks \
                         {ABI_REVISION}; refusing to call through mismatched declarations"
                    ),
                });
            }

            let cols = (|| {
                Some(ColumnApi {
                    count: optional(&lib, b"chs_schema_column_count\0")?,
                    name: optional(&lib, b"chs_schema_column_name\0")?,
                    ty: optional(&lib, b"chs_schema_column_type\0")?,
                    default_expr: optional(&lib, b"chs_schema_column_default_expr\0")?,
                    default_kind: optional(&lib, b"chs_schema_column_default_kind\0")?,
                    is_literal: optional(&lib, b"chs_schema_column_default_is_literal\0")?,
                })
            })();

            Ok(Api {
                f_version,
                abi_revision,
                f_init,
                f_compile,
                f_rows,
                f_free: optional(&lib, b"chs_free\0"),
                f_schema_free: optional(&lib, b"chs_schema_free\0"),
                f_shutdown: optional(&lib, b"chs_shutdown\0"),
                f_set_defaults: optional(&lib, b"chs_set_default_settings\0"),
                f_validate: optional(&lib, b"chs_validate_type\0"),
                f_row: optional(&lib, b"chs_row\0"),
                f_engine: optional(&lib, b"chs_schema_engine\0"),
                f_ttl: optional(&lib, b"chs_schema_ttl\0"),
                f_reference_type: optional(&lib, b"chs_reference_type\0"),
                f_registered_families: optional(&lib, b"chs_registered_families\0"),
                f_function_flags: optional(&lib, b"chs_function_flags\0"),
                f_filter_compile: optional(&lib, b"chs_filter_compile\0"),
                f_filter_free: optional(&lib, b"chs_filter_free\0"),
                f_filter_rows: optional(&lib, b"chs_filter_rows\0"),
                f_block_parse: optional(&lib, b"chs_block_parse\0"),
                f_block_free: optional(&lib, b"chs_block_free\0"),
                f_filter_eval: optional(&lib, b"chs_filter_eval\0"),
                cols,
                _lib: ManuallyDrop::new(lib),
            })
        }
    }

    /// The ABI revision this artifact reported at open, or 0 when the symbol
    /// was absent. See [`crate::ABI_REVISION`].
    pub(crate) fn abi_revision(&self) -> i32 {
        self.abi_revision
    }

    /// Copy a `char *` the library returned, then release it with **this
    /// library's** `chs_free`.
    ///
    /// This is the only place in the crate that calls `chs_free`, so a pointer
    /// from one loaded version can never be handed to another's allocator.
    ///
    /// # Safety
    /// `p` must be null or a `char *` returned by *this* library.
    pub(crate) unsafe fn take(&self, p: *mut c_char) -> Option<Vec<u8>> {
        // SAFETY: the caller's `# Safety` clause guarantees `p` is null or a `char *`
        // this library malloc'd, which is exactly what `CStr::from_ptr` and the
        // artifact's own `chs_free` require.
        unsafe {
            if p.is_null() {
                return None;
            }
            let bytes = CStr::from_ptr(p).to_bytes().to_vec();
            if let Some(free) = &self.f_free {
                free(p);
            }
            // No chs_free cannot happen with a published artifact (all 21 symbols
            // ship together), and a missing optional symbol must never be a load
            // failure or a panic — so in that impossible case the C allocation is
            // left to the process rather than freed by the wrong allocator.
            Some(bytes)
        }
    }

    /// The ClickHouse release this library was vendored from. Borrowed string
    /// constant — never freed, callable before `chs_init`.
    pub(crate) fn clickhouse_version(&self) -> String {
        // SAFETY: documented never to return NULL, and the returned pointer is a
        // compiled-in string constant valid for the library's lifetime.
        unsafe {
            CStr::from_ptr((self.f_version)())
                .to_string_lossy()
                .into_owned()
        }
    }

    /// One-time per-library setup. `0` on success; on failure the message is
    /// ClickHouse's own — the one reachable failure is an unknown `timezone`,
    /// and a bare code cannot say which name was rejected.
    pub(crate) fn init(&self, timezone: &CStr, unsafe_families: &CStr) -> (i32, String) {
        let mut err: *mut c_char = std::ptr::null_mut();
        // SAFETY: both pointers are valid NUL-terminated strings for the call;
        // the out-param is live for the call, and the returned string goes
        // through take().
        unsafe {
            let rc = (self.f_init)(timezone.as_ptr(), unsafe_families.as_ptr(), &mut err);
            let message = self.take(err);
            (rc, string_of(message))
        }
    }

    /// Seed the settings every later call starts from. `None` when the
    /// artifact predates the entry point; otherwise the C return code and, on
    /// failure, ClickHouse's own message — including its did-you-mean hint on
    /// an unknown setting name.
    pub(crate) fn set_default_settings(&self, settings_json: &CStr) -> Option<(i32, String)> {
        let f = self.f_set_defaults.as_ref()?;
        let mut err: *mut c_char = std::ptr::null_mut();
        // SAFETY: valid NUL-terminated JSON object text; the out-param is live
        // for the call, and the returned string goes through take().
        unsafe {
            let rc = f(settings_json.as_ptr(), &mut err);
            let message = self.take(err);
            Some((rc, string_of(message)))
        }
    }

    /// Join the DEFAULT evaluator's background threads. Idempotent, and safe to
    /// call when `chs_init` was never reached.
    pub(crate) fn shutdown(&self) {
        if let Some(f) = &self.f_shutdown {
            // SAFETY: no arguments; documented idempotent.
            unsafe { f() }
        }
    }

    /// `chs_validate_type`. Returns the canonical spelling, or the code/message
    /// pair ClickHouse rejected it with.
    pub(crate) fn validate_type(&self, expr: &CStr) -> Result<String> {
        let Some(f) = self.f_validate.as_ref() else {
            return Err(Error::PredatesFeature {
                feature: "chs_validate_type",
            });
        };
        let mut canonical: *mut c_char = std::ptr::null_mut();
        let mut code: c_int = 0;
        let mut err: *mut c_char = std::ptr::null_mut();
        // SAFETY: all out-params are live for the call; both returned strings are
        // released through take().
        unsafe {
            let rc = f(expr.as_ptr(), &mut canonical, &mut code, &mut err);
            let canon = self.take(canonical);
            let message = self.take(err);
            if rc == 0 {
                Ok(string_of(canon))
            } else {
                Err(Error::from_code(code, string_of(message)))
            }
        }
    }

    /// `chs_schema_compile` — DDL, an optional DECLARED settings profile, and
    /// the compile mode, in one call. `settings_json` NULL/`"{}"` plus
    /// `mode` `0` is "compile under this build's own compile base", the
    /// common case, and takes a code path that makes no `Context` copy at
    /// all. The handle is owned by the caller and must be released with
    /// [`Api::schema_free`].
    ///
    /// An unknown setting name is the server's own `115`
    /// ([`Error::Schema`], code visible, via [`Error::from_code`]); a `mode`
    /// this build does not define is the `-2` sentinel
    /// ([`Error::Unsupported`]).
    pub(crate) fn compile(
        &self,
        columns_sql: &CStr,
        settings_json: &CStr,
        mode: i32,
    ) -> Result<*mut ChsSchema> {
        let mut code: c_int = 0;
        let mut err: *mut c_char = std::ptr::null_mut();
        // SAFETY: valid NUL-terminated DDL and JSON text; out-params live for
        // the call; the error string goes through take().
        unsafe {
            let handle = (self.f_compile)(
                columns_sql.as_ptr(),
                settings_json.as_ptr(),
                mode as c_int,
                &mut code,
                &mut err,
            );
            let message = self.take(err);
            if handle.is_null() {
                return Err(Error::from_code(code, string_of(message)));
            }
            Ok(handle)
        }
    }

    /// Whether this artifact exports the settings-aware compile
    /// (`chs_schema_compile`, consolidated 2026-08-24). Always `true`: the
    /// symbol is one of the four mandatory entry points, so an `Api` that
    /// opened at all exports it. Kept as a probe, rather than removed,
    /// because the conformance driver's `caps.compile_settings` handshake
    /// keys on it, and because a future `Registry` loading a
    /// third-party-built artifact should still ask rather than assume.
    pub(crate) fn has_compile_settings(&self) -> bool {
        true
    }

    /// # Safety
    /// `handle` must come from this library's [`Api::compile`] and must not be
    /// used afterwards.
    pub(crate) unsafe fn schema_free(&self, handle: *mut ChsSchema) {
        // SAFETY: the caller's `# Safety` clause guarantees `handle` came from this
        // library's `compile` and is not used afterwards, which is `chs_schema_free`'s
        // whole contract.
        unsafe {
            if let Some(f) = &self.f_schema_free {
                f(handle);
            }
        }
    }

    /// `chs_schema_engine` — the engine, the sorting key, and the table's
    /// MergeTree-namespace settings (the `SETTINGS` clause after the engine),
    /// in one call. `merge_tree_settings_json` NULL/`"{}"` declares none.
    ///
    /// Per `spec/c-abi.md`'s error model, two DIFFERENT KINDS of verdict, told
    /// apart by the SIGN of the return: any POSITIVE rc is a real ClickHouse
    /// error code — the server's own refusal of this DDL, which can therefore
    /// never exist — and crosses as [`Error::Schema`] with that code and the
    /// server's message (`115` with its "Maybe you meant ..." hint is today's
    /// only instance). Any NEGATIVE rc is this library declining
    /// ([`Error::Unsupported`]) — `-2` for a non-default declared value or an
    /// unmodelled engine/key, `-1` for a guarded exception — and a caller must
    /// validate cautiously rather than tell the tenant its DDL is wrong.
    ///
    /// # Safety
    /// `handle` must come from this library's [`Api::compile`].
    pub(crate) unsafe fn engine(
        &self,
        handle: *mut ChsSchema,
        engine: &CStr,
        order_by: &CStr,
        merge_tree_settings_json: &CStr,
    ) -> Result<()> {
        // SAFETY: the caller's `# Safety` clause guarantees `handle` came from this
        // library's `compile`; the `&CStr` arguments are NUL-terminated by
        // construction, so every pointer handed across is valid for the call.
        unsafe {
            let Some(f) = self.f_engine.as_ref() else {
                return Err(Error::PredatesFeature {
                    feature: "engine support",
                });
            };
            let mut err: *mut c_char = std::ptr::null_mut();
            let rc = f(
                handle,
                engine.as_ptr(),
                order_by.as_ptr(),
                merge_tree_settings_json.as_ptr(),
                &mut err,
            );
            let message = string_of(self.take(err));
            // The SIGN of rc is the rule. Positive is a real ClickHouse error code:
            // the server's own engine validation REFUSED this DDL, so it can never
            // exist and the tenant must be told — `Error::Schema`, carrying the
            // server's code and message (which holds the "Maybe you meant ..."
            // hint). Negative is this library declining: -2 "I will not guess", -1
            // a guarded exception — `Error::Unsupported`, which a caller must
            // validate cautiously rather than blame on the tenant. Matched on the
            // sign, not on the literal 115, so a code a future era returns here
            // cannot silently be demoted to a decline.
            match rc {
                0 => Ok(()),
                code if code > 0 => Err(Error::Schema {
                    code,
                    message,
                    column: None,
                }),
                _ => Err(Error::Unsupported { message }),
            }
        }
    }

    /// # Safety
    /// `handle` must come from this library's [`Api::compile`].
    pub(crate) unsafe fn ttl(&self, handle: *mut ChsSchema, ttl_sql: &CStr) -> Result<()> {
        // SAFETY: the caller's `# Safety` clause guarantees `handle` came from this
        // library's `compile`; `ttl_sql` is a `&CStr`, so it is NUL-terminated.
        unsafe {
            let Some(f) = self.f_ttl.as_ref() else {
                return Err(Error::PredatesFeature {
                    feature: "TTL support",
                });
            };
            let mut err: *mut c_char = std::ptr::null_mut();
            let rc = f(handle, ttl_sql.as_ptr(), &mut err);
            let message = string_of(self.take(err));
            // Same rule as engine: nonzero is "this build refuses to model it".
            if rc == 0 {
                Ok(())
            } else {
                Err(Error::Unsupported { message })
            }
        }
    }

    /// `chs_row`. `raw` is passed counted, not NUL-terminated: binary formats
    /// contain NUL bytes.
    ///
    /// # Safety
    /// `handle` must come from this library's [`Api::compile`].
    pub(crate) unsafe fn row(
        &self,
        handle: *mut ChsSchema,
        format: i32,
        raw: &[u8],
        settings_json: &CStr,
    ) -> Result<Vec<u8>> {
        // SAFETY: the caller's `# Safety` clause guarantees `handle` came from this
        // library's `compile`. `raw` is passed as pointer+length, so it needs no NUL
        // and may contain interior NULs, which the binary formats do.
        unsafe {
            let Some(f) = self.f_row.as_ref() else {
                return Err(Error::PredatesFeature { feature: "chs_row" });
            };
            let out = f(
                handle,
                format as c_int,
                counted_ptr(raw),
                raw.len(),
                settings_json.as_ptr(),
            );
            self.take(out)
                .ok_or(Error::PredatesFeature { feature: "chs_row" })
        }
    }

    /// `chs_rows`, which is not `chs_row` in a loop: row separation is
    /// format-specific and `input_format_allow_errors_*` decides whether a bad
    /// row is skipped or aborts the batch. It is also the unit of the
    /// volatile-DEFAULT clock guarantee — one batch is one clock instant.
    ///
    /// Revision 3: `export_format` is [`EXPORT_NONE`] or an `enum chs_format`
    /// value the artifact can serialize; `doc_flags` selects the document
    /// groups (7 = all, today's full document). The export buffer rides only
    /// when an export is requested; the returned pair is (document bytes,
    /// export payload). The payload is `None` when no export was requested
    /// or the library emitted nothing (`{NULL,0}` — a decline), and
    /// `Some(empty)` for the EMITTED-EMPTY case (`data` non-NULL, `len` 0);
    /// its bytes are copied counted — never read as a C string, the buffer is
    /// not NUL-terminated by contract — and freed with this library's own
    /// `chs_free` before returning, so no ownership crosses this boundary.
    ///
    /// # Safety
    /// `handle` must come from this library's [`Api::compile`].
    pub(crate) unsafe fn rows(
        &self,
        handle: *mut ChsSchema,
        format: i32,
        body: &[u8],
        settings_json: &CStr,
        export_format: i32,
        doc_flags: u32,
    ) -> Result<(Vec<u8>, Option<Vec<u8>>)> {
        // SAFETY: the caller's `# Safety` clause guarantees `handle` came from this
        // library's `compile`. `body` is passed as pointer+length, so it needs no NUL
        // and may contain interior NULs, which the binary formats do. The export
        // buffer is a local the library initializes to {NULL,0} at entry.
        unsafe {
            let mut buf = ChsBytes {
                data: std::ptr::null_mut(),
                len: 0,
            };
            let want_export = export_format != EXPORT_NONE;
            let out = (self.f_rows)(
                handle,
                format as c_int,
                counted_ptr(body),
                body.len(),
                settings_json.as_ptr(),
                export_format as c_int,
                doc_flags,
                if want_export {
                    &mut buf
                } else {
                    std::ptr::null_mut()
                },
            );
            // Copy-then-free the export buffer FIRST, whatever happens to the
            // document: this is the one place that sees the pointer.
            let payload = if want_export && !buf.data.is_null() {
                let bytes = std::slice::from_raw_parts(buf.data.cast::<u8>(), buf.len).to_vec();
                if let Some(free) = &self.f_free {
                    free(buf.data);
                }
                Some(bytes)
            } else {
                None
            };
            let doc = self.take(out).ok_or(Error::PredatesFeature {
                feature: "chs_rows",
            })?;
            Ok((doc, payload))
        }
    }

    /// Whether this artifact exports the revision-3 filter trio. All three
    /// symbols shipped together; asking for any of them individually would
    /// let a half-present trio produce a compile with no way to free.
    pub(crate) fn has_filter(&self) -> bool {
        self.f_filter_compile.is_some()
            && self.f_filter_free.is_some()
            && self.f_filter_rows.is_some()
    }

    /// `chs_filter_compile`. A NULL return carries ClickHouse's own code and
    /// message: the SIGN decides the error kind ([`Error::from_code`] — rule
    /// 12), so a positive code is the server's own refusal (including 456 for
    /// an unbound `{name:Type}` and 457 for an unparseable value, since
    /// revision 4) and a negative one is this library declining (clock
    /// reads). `params_json` is the `{name:Type}` bindings — a JSON object of
    /// name -> value STRING, `"{}"` for none.
    ///
    /// # Safety
    /// `handle` must come from this library's [`Api::compile`] and outlive the
    /// returned filter.
    pub(crate) unsafe fn filter_compile(
        &self,
        handle: *mut ChsSchema,
        expr_sql: &CStr,
        params_json: &CStr,
    ) -> Result<*mut ChsFilter> {
        if !self.has_filter() {
            return Err(Error::PredatesFeature {
                feature: "chs_filter_compile",
            });
        }
        let f = self
            .f_filter_compile
            .as_ref()
            .expect("checked by has_filter");
        let mut code: c_int = 0;
        let mut err: *mut c_char = std::ptr::null_mut();
        // SAFETY: the caller's `# Safety` clause guarantees `handle`; the
        // expression and params are NUL-terminated; out-params are live for
        // the call and the error string goes through take().
        unsafe {
            let filter = f(
                handle,
                expr_sql.as_ptr(),
                params_json.as_ptr(),
                &mut code,
                &mut err,
            );
            let message = self.take(err);
            if filter.is_null() {
                return Err(Error::from_code(code, string_of(message)));
            }
            Ok(filter)
        }
    }

    /// # Safety
    /// `filter` must come from this library's [`Api::filter_compile`], must
    /// not be used afterwards, and its schema handle must still be alive.
    pub(crate) unsafe fn filter_free(&self, filter: *mut ChsFilter) {
        // SAFETY: the caller's `# Safety` clause is chs_filter_free's whole
        // contract.
        unsafe {
            if let Some(f) = &self.f_filter_free {
                f(filter);
            }
        }
    }

    /// `chs_filter_rows` — one filter over a body of rows; the per-row
    /// verdicts live in the returned document.
    ///
    /// # Safety
    /// `filter` must come from this library's [`Api::filter_compile`] and its
    /// schema handle must still be alive.
    pub(crate) unsafe fn filter_rows(
        &self,
        filter: *mut ChsFilter,
        format: i32,
        body: &[u8],
        settings_json: &CStr,
    ) -> Result<Vec<u8>> {
        let Some(f) = self.f_filter_rows.as_ref() else {
            return Err(Error::PredatesFeature {
                feature: "chs_filter_rows",
            });
        };
        // SAFETY: the caller's `# Safety` clause guarantees `filter`; `body`
        // is passed counted and may contain interior NULs.
        unsafe {
            let out = f(
                filter,
                format as c_int,
                counted_ptr(body),
                body.len(),
                settings_json.as_ptr(),
            );
            self.take(out).ok_or(Error::PredatesFeature {
                feature: "chs_filter_rows",
            })
        }
    }

    /// Whether this artifact exports the revision-4 block twin. The three
    /// symbols shipped together; asking for any individually would let a
    /// half-present twin produce a block with no way to free or evaluate it.
    pub(crate) fn has_block(&self) -> bool {
        self.f_block_parse.is_some() && self.f_block_free.is_some() && self.f_filter_eval.is_some()
    }

    /// `chs_block_parse` — parse a body ONCE into a block (the parse half of
    /// `chs_filter_rows`). A NULL return is a call-level failure with
    /// ClickHouse's own code/message (unknown setting 115, framing, a binary
    /// decode fault): a malformed body yields no block and no partial
    /// answers.
    ///
    /// # Safety
    /// `handle` must come from this library's [`Api::compile`] and outlive
    /// the returned block.
    pub(crate) unsafe fn block_parse(
        &self,
        handle: *mut ChsSchema,
        format: i32,
        body: &[u8],
        settings_json: &CStr,
    ) -> Result<*mut ChsBlock> {
        if !self.has_block() {
            return Err(Error::PredatesFeature {
                feature: "chs_block_parse",
            });
        }
        let f = self.f_block_parse.as_ref().expect("checked by has_block");
        let mut code: c_int = 0;
        let mut err: *mut c_char = std::ptr::null_mut();
        // SAFETY: the caller's `# Safety` clause guarantees `handle`; `body`
        // is passed counted and may contain interior NULs; out-params are
        // live for the call and the error string goes through take().
        unsafe {
            let block = f(
                handle,
                format as c_int,
                counted_ptr(body),
                body.len(),
                settings_json.as_ptr(),
                &mut code,
                &mut err,
            );
            let message = self.take(err);
            if block.is_null() {
                return Err(Error::from_code(code, string_of(message)));
            }
            Ok(block)
        }
    }

    /// # Safety
    /// `block` must come from this library's [`Api::block_parse`], must not
    /// be used afterwards, and its schema handle must still be alive.
    pub(crate) unsafe fn block_free(&self, block: *mut ChsBlock) {
        // SAFETY: the caller's `# Safety` clause is chs_block_free's whole
        // contract.
        unsafe {
            if let Some(f) = &self.f_block_free {
                f(block);
            }
        }
    }

    /// `chs_filter_eval` — one compiled filter over an already-parsed block;
    /// a pure function of (filter, block), answering the same document
    /// `chs_filter_rows` answers. A cross-schema pair answers a rejected
    /// document (1002) from the C layer.
    ///
    /// # Safety
    /// `filter` and `block` must come from THIS library's
    /// [`Api::filter_compile`] / [`Api::block_parse`], and both schema
    /// handles must still be alive.
    pub(crate) unsafe fn filter_eval(
        &self,
        filter: *mut ChsFilter,
        block: *mut ChsBlock,
    ) -> Result<Vec<u8>> {
        let Some(f) = self.f_filter_eval.as_ref() else {
            return Err(Error::PredatesFeature {
                feature: "chs_filter_eval",
            });
        };
        // SAFETY: the caller's `# Safety` clause guarantees both handles.
        unsafe {
            let out = f(filter, block);
            self.take(out).ok_or(Error::PredatesFeature {
                feature: "chs_filter_eval",
            })
        }
    }

    /// The compiled schema's columns, or `None` when the artifact predates the
    /// introspection group.
    ///
    /// # Safety
    /// `handle` must come from this library's [`Api::compile`].
    pub(crate) unsafe fn columns(&self, handle: *mut ChsSchema) -> Option<Vec<RawColumn>> {
        // SAFETY: the caller's `# Safety` clause guarantees `handle` came from this
        // library's `compile`; every accessor below returns a borrowed `const char *`
        // owned by the handle and valid until `chs_schema_free`.
        unsafe {
            let c = self.cols.as_ref()?;
            let n = (c.count)(handle);
            if n < 0 {
                return None;
            }
            let mut out = Vec::with_capacity(n as usize);
            for i in 0..n {
                out.push(RawColumn {
                    name: borrowed((c.name)(handle, i)),
                    ty: borrowed((c.ty)(handle, i)),
                    default_kind: borrowed((c.default_kind)(handle, i)),
                    default_expr: borrowed((c.default_expr)(handle, i)),
                    default_is_literal: (c.is_literal)(handle, i) != 0,
                });
            }
            Some(out)
        }
    }

    /// Diagnostic: the widened reference type this build would use, or `None`
    /// for a type with no wider type to compare against.
    pub(crate) fn reference_type(&self, type_expr: &CStr) -> Result<Option<String>> {
        let Some(f) = self.f_reference_type.as_ref() else {
            return Err(Error::PredatesFeature {
                feature: "chs_reference_type",
            });
        };
        // SAFETY: valid NUL-terminated type text; the result goes through take().
        let out = unsafe { self.take(f(type_expr.as_ptr())) };
        let s = string_of(out);
        Ok((!s.is_empty()).then_some(s))
    }

    /// Newline-separated list of every type family in this build's runtime
    /// registry.
    pub(crate) fn registered_families(&self) -> Result<String> {
        let Some(f) = self.f_registered_families.as_ref() else {
            return Err(Error::PredatesFeature {
                feature: "chs_registered_families",
            });
        };
        // SAFETY: no arguments; the result goes through take().
        Ok(string_of(unsafe { self.take(f()) }))
    }

    /// TSV audit of every registered function's volatility flags. Requires
    /// `chs_init`.
    pub(crate) fn function_flags(&self) -> Result<String> {
        let Some(f) = self.f_function_flags.as_ref() else {
            return Err(Error::PredatesFeature {
                feature: "chs_function_flags",
            });
        };
        // SAFETY: no arguments; the result goes through take().
        Ok(string_of(unsafe { self.take(f()) }))
    }
}

/// # Safety
/// The caller must have opened `lib` and must use the declared signature.
unsafe fn require<T>(lib: &UnixLibrary, name: &'static [u8], path: &Path) -> Result<Symbol<T>> {
    // SAFETY: `lib` is a library this module dlopen'd and never drops, so a
    // `Symbol` resolved from it outlives every use; `name` is a NUL-terminated
    // byte string literal supplied by this module.
    unsafe {
        optional(lib, name).ok_or_else(|| Error::NotAnArtifact {
            path: path.to_path_buf(),
            // The NUL is part of the literal; trim it for the message.
            symbol: std::str::from_utf8(&name[..name.len() - 1]).unwrap_or("chs_?"),
        })
    }
}

/// # Safety
/// Same as [`require`]. A missing symbol is `None`, never an error: it means the
/// artifact predates the feature.
unsafe fn optional<T>(lib: &UnixLibrary, name: &[u8]) -> Option<Symbol<T>> {
    // SAFETY: same as `require`: `lib` outlives the returned `Symbol`, and `name`
    // is a NUL-terminated literal from this module. A missing symbol is `None`.
    unsafe { lib.get(name).ok() }
}

/// A borrowed `const char *` the library owns until `chs_schema_free`. Copied
/// here, never freed.
///
/// # Safety
/// `p` must be null or a valid NUL-terminated string owned by the library.
unsafe fn borrowed(p: *const c_char) -> String {
    // SAFETY: the caller guarantees `p` is a NUL-terminated `const char *` borrowed
    // from a live handle, so it stays valid for the duration of this read.
    unsafe {
        if p.is_null() {
            return String::new();
        }
        CStr::from_ptr(p).to_string_lossy().into_owned()
    }
}

fn string_of(bytes: Option<Vec<u8>>) -> String {
    match bytes {
        // Lossy on purpose, and only for *messages* and *canonical types*, which
        // ClickHouse writes as UTF-8. Stored values never take this path: they
        // stay raw JSON all the way to the caller.
        Some(b) => String::from_utf8_lossy(&b).into_owned(),
        None => String::new(),
    }
}

/// The pointer for a counted buffer. `raw`/`body` are counted, not
/// NUL-terminated — binary formats contain NUL bytes — but an empty slice's
/// pointer is dangling, so an empty body is handed a real readable byte instead.
fn counted_ptr(bytes: &[u8]) -> *const c_char {
    if bytes.is_empty() {
        c"".as_ptr()
    } else {
        bytes.as_ptr() as *const c_char
    }
}

/// A NUL-terminated copy of `s`, or the interior-NUL error.
pub(crate) fn cstring(s: &str, what: &'static str) -> Result<CString> {
    CString::new(s).map_err(|_| Error::Nul { what })
}
