//! The compile builder. `Library::compile` returns a [`CompileRequest`]; the
//! terminal `.compile()` call is where the C boundary (`chs_schema_compile`)
//! is actually crossed. One entry point, one terminal method — chosen over
//! `Option` arguments at every call site (`compile(ddl, None, 0)` on the
//! common path) because Rust has no named/optional arguments, and a builder
//! keeps the settings case reading as a sentence:
//!
//! ```no_run
//! # use chtypes::{CompileMode, Registry};
//! # fn main() -> Result<(), Box<dyn std::error::Error>> {
//! # let lib = Registry::from_env_or_default()?.for_version("25.8")?;
//! let schema = lib.compile("a UInt8, b String").compile()?;                       // common case
//! let schema = lib.compile("n Nested(a Int64, b String)")
//!     .settings([("flatten_nested", "0")])
//!     .mode(CompileMode::Declared)
//!     .compile()?;
//! # Ok(()) }
//! ```

use std::sync::Arc;

use crate::error::Result;
use crate::ffi::cstring;
use crate::library::{Library, columns_of};
use crate::schema::{Schema, settings_json};

/// The compile MODE — mirrors `enum chs_compile_mode` in `chtypes.h`. Numeric
/// values are part of the ABI, exactly like [`crate::Format`].
///
/// [`CompileMode::Declared`] is the only defined mode, so this enum has no
/// way to construct an out-of-range value through [`CompileRequest`]: a
/// caller that reached past this builder with a raw escape hatch and passed
/// one anyway would be refused by the library itself with the `-2`
/// [`crate::CODE_UNSUPPORTED`] sentinel, never guessed at. This crate exposes
/// no such escape hatch today.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
#[repr(i32)]
pub enum CompileMode {
    /// Every setting the profile names takes the caller's value; every
    /// setting it does not name keeps the library's own compile base (build
    /// defaults plus the derived permissive type-gate list — see
    /// `docs/reference/c-abi.md` §Compile-time settings).
    #[default]
    Declared = 0,
}

impl CompileMode {
    /// The `chs_compile_mode` integer this mode crosses the C boundary as.
    pub fn code(self) -> i32 {
        self as i32
    }
}

/// A compile request, built with [`Library::compile`]. See the module docs
/// for the common case and the settings-profile case side by side.
pub struct CompileRequest<'a> {
    pub(crate) lib: &'a Arc<Library>,
    pub(crate) columns_sql: String,
    pub(crate) settings: Vec<(String, String)>,
    pub(crate) mode: CompileMode,
}

impl<'a> CompileRequest<'a> {
    /// A DECLARED settings profile — the settings the deployment's server
    /// runs, fixed into the handle exactly as a real `CREATE TABLE` fixes
    /// them into the table. Unset (the default) crosses the C boundary as
    /// `NULL`/`"{}"`: "compile under this build's own compile base", the
    /// common case, which takes a code path that makes no `Context` copy at
    /// all. Replaces any settings from an earlier call.
    pub fn settings<I, K, V>(mut self, settings: I) -> Self
    where
        I: IntoIterator<Item = (K, V)>,
        K: Into<String>,
        V: Into<String>,
    {
        self.settings = settings
            .into_iter()
            .map(|(k, v)| (k.into(), v.into()))
            .collect();
        self
    }

    /// The compile mode. Defaults to [`CompileMode::Declared`], the only
    /// defined mode.
    pub fn mode(mut self, mode: CompileMode) -> Self {
        self.mode = mode;
        self
    }

    /// Compile, consuming the request. Crosses the C boundary
    /// (`chs_schema_compile`) and returns the compiled [`Schema`] handle,
    /// its columns canonicalised by this build.
    ///
    /// # Errors
    ///
    /// * [`crate::Error::Schema`] — **ClickHouse itself rejected**, with its
    ///   own code and message: an unknown setting name in the profile — the
    ///   `chtypes_*` per-call keys included — is the server's own `115`;
    ///   a type gate DECLARED at a refusing value fails here exactly as that
    ///   server's `CREATE` would (`455`, `44`); an invalid type or DEFAULT is
    ///   the server's own code (`50`, `386`, …); an
    ///   `Enum … DEFAULT <out-of-domain integer>` is refused with `691` on
    ///   every line, because older servers accept the DDL and then poison the
    ///   table (`docs/reference/c-abi.md` §Appendix).
    /// * [`crate::Error::Unsupported`] — **this build declines**
    ///   ([`crate::CODE_UNSUPPORTED`]): a DEFAULT that is a property of the
    ///   server or session (`hostName()`, `currentUser()`), one that would
    ///   block (`sleep`), one that exceeds the admission budgets, or a `mode`
    ///   this build does not define. A real server might well have accepted
    ///   the schema — fall back to it rather than reporting a tenant error.
    /// * [`crate::Error::Nul`] — the DDL or a setting contained an interior
    ///   NUL byte.
    pub fn compile(self) -> Result<Schema> {
        let ddl = cstring(&self.columns_sql, "column list")?;
        let json = settings_json(&self.settings)?;
        let (handle, columns) = {
            let _guard = self.lib.lock();
            let handle = self.lib.api().compile(&ddl, &json, self.mode.code())?;
            // SAFETY: the handle was just returned by this library's compile
            // and is not shared yet.
            let columns = unsafe { self.lib.api().columns(handle) };
            (handle, columns)
        };
        Ok(Schema::new(
            Arc::clone(self.lib),
            handle,
            columns_of(columns),
        ))
    }
}
