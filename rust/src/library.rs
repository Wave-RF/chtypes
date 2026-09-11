//! One loaded ClickHouse build.

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use crate::compile::{CompileMode, CompileRequest};
use crate::error::{Error, Result};
use crate::ffi::{Api, cstring};
use crate::schema::settings_json;

/// The server timezone assumed for bare `DateTime` / `DateTime64` columns.
///
/// A binding must default this to `UTC` — what a stock ClickHouse container uses
/// — and must **not** read the host's `TZ`, or the host's environment leaks into
/// results. [`Library::load`] takes it explicitly so the default is visible.
pub const DEFAULT_TIMEZONE: &str = "UTC";

/// What a column's DEFAULT clause is.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DefaultKind {
    /// No DEFAULT clause.
    None,
    /// A `DEFAULT` expression: applied when the row omits the column, and
    /// overridable by the row.
    Default,
    /// Durable, and not part of `SELECT *`.
    Materialized,
    /// Computed at read time; never presented as a stored value.
    Alias,
    /// No value at all; visible only through the DEFAULT columns referencing it.
    Ephemeral,
    /// A kind added after this crate was written. The ABI grows additively, so an
    /// unknown spelling is passed through rather than rejected.
    Other(String),
}

impl DefaultKind {
    fn parse(s: &str) -> DefaultKind {
        match s {
            "" => DefaultKind::None,
            "DEFAULT" => DefaultKind::Default,
            "MATERIALIZED" => DefaultKind::Materialized,
            "ALIAS" => DefaultKind::Alias,
            "EPHEMERAL" => DefaultKind::Ephemeral,
            other => DefaultKind::Other(other.to_string()),
        }
    }
}

/// One column of a compiled schema, as this build canonicalized it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Column {
    /// The column name.
    pub name: String,
    /// The canonical type in **ClickHouse's own spelling** — `Decimal(18, 4)`,
    /// `Enum8('a' = 1, 'b' = 2)`, `Map(String, Array(UInt8))`, with a space after
    /// each comma. Pass it through verbatim; a binding must not normalize
    /// whitespace of its own.
    pub ty: String,
    /// Which clause the column carries, if any.
    pub default_kind: DefaultKind,
    /// The DEFAULT/MATERIALIZED/ALIAS expression as ClickHouse canonicalized it,
    /// empty when there is none.
    pub default_expr: String,
    /// True when the DEFAULT is a plain literal applicable without the
    /// expression interpreter.
    pub default_is_literal: bool,
}

/// One `dlopen`'d vendored ClickHouse build.
///
/// The library names itself: [`Library::version`] comes from
/// `chs_clickhouse_version()`, never from the directory or file name.
///
/// Thread-safety follows the reference implementation's `dlopen` path and is
/// deliberately more conservative than the ABI requires: one mutex per loaded
/// IMAGE guards every call, including `chs_schema_compile` and `chs_free`. The
/// header allows concurrent calls on *distinct* handles; per-handle parallelism
/// inside one version is a change a binding must prove with the rigs rather than
/// by reasoning, so this crate does not take it.
///
/// Per loaded IMAGE, not per `Library` value, and the difference is
/// load-bearing (2026-08-26). `dlopen` refcounts one mapping per file, so two
/// `Registry` instances over one directory produce two `Library` values that
/// share ONE set of the wrapper's process-globals — the same reason `chs_init`
/// is deduplicated by path just below. `set_default_settings` REPLACES the
/// seeded settings list while the row path reads it by reference
/// (the C ABI contract §Thread-safety: it "MUST be serialized against all other
/// calls"), and two `Mutex<()>` values, one per `Library`, would have excluded
/// nothing at all. The mutex is therefore interned on the canonicalized path,
/// exactly as `INITED` is; `docs/reference/bindings.md` §Concurrency states the rule.
pub struct Library {
    version: String,
    minor: String,
    path: PathBuf,
    api: Api,
    /// Serializes every call into this library. See the type docs.
    lock: Arc<Mutex<()>>,
}

/// The one mutex per loaded image, keyed the way `chs_init` is keyed. See
/// `Library`'s docs for why this cannot live in the `Library` value.
fn image_lock(path: &Path) -> Arc<Mutex<()>> {
    static LOCKS: Mutex<Option<std::collections::BTreeMap<PathBuf, Arc<Mutex<()>>>>> =
        Mutex::new(None);
    let key = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    let mut guard = match LOCKS.lock() {
        Ok(g) => g,
        Err(poisoned) => poisoned.into_inner(),
    };
    Arc::clone(
        guard
            .get_or_insert_with(std::collections::BTreeMap::new)
            .entry(key)
            .or_insert_with(|| Arc::new(Mutex::new(()))),
    )
}

impl Library {
    /// `dlopen` one artifact, ask it its own version, and `chs_init` it exactly
    /// once with `timezone` and the contents of the artifact's own
    /// `unsafe_families.txt`.
    ///
    /// The refuse-list is generated at build time by probing the build's own
    /// registry and must never be hard-coded. An absent or empty file means an
    /// empty list — every artifact in the current matrix ships an empty one, so
    /// treating "empty" as "missing, bail out" would be wrong.
    ///
    /// # Errors
    ///
    /// * [`Error::Load`] — `dlopen` failed, or the artifact reports a `chs_*`
    ///   ABI revision different from this crate's [`crate::ABI_REVISION`]
    ///   (both nonzero ⇒ refuse; `0` means the artifact predates the probe
    ///   and loads with per-symbol degradation).
    /// * [`Error::NotAnArtifact`] — the library loaded but lacks one of the
    ///   four mandatory `chs_*` symbols.
    /// * [`Error::Init`] — `chs_init` returned nonzero; the one reachable
    ///   cause is an unknown `timezone`, and the message names it.
    /// * [`Error::InitConflict`] — this artifact image is already initialized
    ///   with a different timezone (one image per path; `chs_init` runs at
    ///   most once).
    /// * [`Error::Nul`] — `timezone` or the refuse-list contained an interior
    ///   NUL byte.
    pub fn load(path: impl AsRef<Path>, timezone: &str) -> Result<Library> {
        let path = path.as_ref();
        let api = Api::open(path)?;

        // The library names itself; nothing is inferred from the path.
        let version = api.clickhouse_version();
        let minor = minor_of(&version);

        let families = std::fs::read_to_string(
            path.parent()
                .unwrap_or(Path::new("."))
                .join("unsafe_families.txt"),
        )
        .map(|s| s.trim().to_string())
        .unwrap_or_default();

        // chs_init AT MOST ONCE per artifact image. dlopen refcounts one image
        // per path, so a second Library over the same artifact shares its C
        // globals — re-running chs_init would rebuild the refuse-list and
        // re-set DateLUT under the first instance's live readers (the same
        // hazard the Go binding's loadedLibs map and the TS binding's
        // realpath-keyed map guard). Keyed on the canonicalized path so two
        // spellings of one file cannot slip past; a second init with a
        // DIFFERENT timezone is refused rather than silently re-timezoning
        // the survivor's live libraries.
        static INITED: std::sync::Mutex<std::collections::BTreeMap<std::path::PathBuf, String>> =
            std::sync::Mutex::new(std::collections::BTreeMap::new());
        let key = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
        {
            let mut inited = INITED.lock().expect("init registry poisoned");
            match inited.get(&key) {
                Some(prev_tz) if prev_tz == timezone => {} // already initialized, same config
                Some(prev_tz) => {
                    return Err(Error::InitConflict {
                        path: path.to_path_buf(),
                        have: prev_tz.clone(),
                        want: timezone.to_string(),
                    });
                }
                None => {
                    let tz = cstring(timezone, "timezone")?;
                    let fams = cstring(&families, "unsafe_families")?;
                    let (rc, message) = api.init(&tz, &fams);
                    if rc != 0 {
                        return Err(Error::Init {
                            path: path.to_path_buf(),
                            rc,
                            message,
                        });
                    }
                    inited.insert(key, timezone.to_string());
                }
            }
        }

        Ok(Library {
            version,
            minor,
            path: path.to_path_buf(),
            api,
            lock: image_lock(path),
        })
    }

    /// The exact ClickHouse release, e.g. `25.8.28.1-lts`, as the library
    /// reports it.
    pub fn version(&self) -> &str {
        &self.version
    }

    /// The `chs_*` ABI revision this ARTIFACT was built from, or `0` when it
    /// predates `chs_abi_revision`.
    ///
    /// A `Library` that exists reports either [`crate::ABI_REVISION`] or `0`:
    /// a different nonzero revision is refused at load time, because it is a
    /// positive statement that this crate's declarations do not describe the
    /// artifact.
    pub fn abi_revision(&self) -> i32 {
        self.api.abi_revision()
    }

    /// The minor line, e.g. `25.8` — the first two dot-separated components of
    /// [`Library::version`]. Note `25.10` is a *later* line than `25.8`: string
    /// comparison of minor lines is meaningless and must not be used for
    /// ordering.
    pub fn minor(&self) -> &str {
        &self.minor
    }

    /// The shared library's path.
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Parse and canonicalize one type expression.
    ///
    /// Canonicalization is not a spelling normalizer: `DECIMAL(18,4)` and
    /// `Decimal64(4)` both become `Decimal(18, 4)`, `BIGINT` becomes `Int64`,
    /// `Variant(UInt8, String)` becomes `Variant(String, UInt8)` with members
    /// sorted, and `Int8(3)` drops the surplus parameter rather than failing.
    ///
    /// It is also not sufficient on its own: `x Int64 DEFAULT NULL` compiles to
    /// `Nullable(Int64)`, which only [`Library::compile`] can see.
    ///
    /// Returns the canonical spelling in **ClickHouse's own text**, verbatim —
    /// `Decimal(18, 4)` with the space after the comma. String-compare against
    /// it exactly; never re-normalize whitespace.
    ///
    /// # Errors
    ///
    /// * [`Error::Schema`] — ClickHouse rejected the expression, with its own
    ///   code and message (`50` `Unknown data type family: NotAType`).
    /// * [`Error::Unsupported`] — this build declines to answer for the
    ///   expression ([`crate::CODE_UNSUPPORTED`]); not a rejection.
    /// * [`Error::PredatesFeature`] — the artifact does not export
    ///   `chs_validate_type`.
    /// * [`Error::Nul`] — the expression contained an interior NUL byte.
    pub fn validate_type(&self, type_expr: &str) -> Result<String> {
        let expr = cstring(type_expr, "type expression")?;
        let _guard = self.lock();
        self.api.validate_type(&expr)
    }

    /// Compile a ClickHouse **column-declaration list** — not a `CREATE TABLE`:
    /// `"a UInt8, b Nullable(String) DEFAULT 'x', c DateTime MATERIALIZED now()"`.
    /// It is parsed by ClickHouse's own `ParserColumnDeclarationList`, so DEFAULT
    /// expressions are validated as real SQL.
    ///
    /// Returns a [`CompileRequest`] builder. The common case needs only the
    /// terminal call:
    ///
    /// ```no_run
    /// # use chtypes::Registry;
    /// # fn main() -> Result<(), Box<dyn std::error::Error>> {
    /// # let lib = Registry::from_env_or_default()?.for_version("25.8")?;
    /// let schema = lib.compile("a UInt8, b String").compile()?;
    /// # Ok(()) }
    /// ```
    ///
    /// and a DECLARED settings profile — the settings the deployment's server
    /// runs, fixed into the handle exactly as a real `CREATE TABLE` fixes them
    /// into the table (the C ABI contract §Compile-time vs per-call settings) —
    /// reads as a sentence:
    ///
    /// ```no_run
    /// # use chtypes::{CompileMode, Registry};
    /// # fn main() -> Result<(), Box<dyn std::error::Error>> {
    /// # let lib = Registry::from_env_or_default()?.for_version("25.8")?;
    /// let schema = lib.compile("n Nested(a Int64, b String)")
    ///     .settings([("flatten_nested", "0")])
    ///     .mode(CompileMode::Declared)
    ///     .compile()?;
    /// # Ok(()) }
    /// ```
    ///
    /// [`CompileMode::Declared`] (the default) is the only defined mode:
    /// declared names take the caller's values, undeclared settings keep the
    /// library's permissive compile base. An unknown setting name — the
    /// `chtypes_*` per-call keys included — fails with the server's own code
    /// `115` ([`crate::Error::Schema`], code visible). Values cross as
    /// strings, as everywhere on this boundary.
    ///
    /// What a settings profile changes at compile, this revision:
    /// `flatten_nested` — at `1` (the default) a `Nested(a, b)` column
    /// compiles to its flattened `n.a`/`n.b` Array columns; at `0` it stays
    /// one column `n` of type `Nested(...)`, and every downstream shape
    /// (column introspection, JSONEachRow name lookup, positional arity, the
    /// RowBinary wire) follows the compiled shape. Per-call settings still
    /// govern row parsing, and only row parsing; a handle's compile profile
    /// is immutable for the handle's life.
    ///
    /// Column-level `TTL` clauses belong in the DDL string; [`crate::Schema::set_ttl`]
    /// is only for the table-level rows TTL.
    pub fn compile(self: &Arc<Self>, columns_sql: &str) -> CompileRequest<'_> {
        CompileRequest {
            lib: self,
            columns_sql: columns_sql.to_string(),
            settings: Vec::new(),
            mode: CompileMode::default(),
        }
    }

    /// Whether this artifact exports the settings-aware compile
    /// (`chs_schema_compile`, consolidated 2026-08-24). Always `true`: the
    /// entry point is one of the four mandatory symbols, so any artifact
    /// this crate loaded at all exports it. Kept as a probe because the
    /// conformance driver's `caps.compile_settings` handshake keys on it, and
    /// because a future `Registry` loading a third-party-built artifact
    /// should still ask rather than assume.
    pub fn has_compile_settings(&self) -> bool {
        self.api.has_compile_settings()
    }

    /// Seed the settings every later call starts from, for the server-level type
    /// gates a gateway knows once — `allow_suspicious_low_cardinality_types`,
    /// `allow_experimental_json_type` and friends. Per-call settings still win.
    ///
    /// Prefer [`Library::compile`]'s `.settings(...)` for a gate that belongs
    /// to a particular tenant's table: a gate declared in the compile profile
    /// binds where a real server binds it — once, at CREATE — and then
    /// outranks the per-call map for that handle (measured on live 25.10.7.6
    /// and 26.7.3.19; the C ABI contract, "Server-level type gates"). This
    /// process-wide seed stays the right channel only for gateway-uniform
    /// policy.
    ///
    /// The admission budgets (`chtypes_default_eval_memory_bytes`,
    /// `chtypes_default_eval_wall_nanos`) are process-wide and can **only** be
    /// set here; admission deliberately takes no per-call settings.
    ///
    /// Values cross as strings, as everywhere on this boundary — see
    /// [`crate::SETTING_NOW_EPOCH_NANOS`].
    ///
    /// Thread-safety: the ABI requires this call be serialized against every
    /// other call on the same loaded image (`docs/reference/bindings.md` §Concurrency,
    /// rule 3 — the row path reads the seeded settings by reference). This
    /// crate satisfies that with the per-image mutex every call takes, so no
    /// caller-side exclusion is needed.
    ///
    /// # Errors
    ///
    /// * [`Error::Schema`] — the payload was refused **wholesale** and nothing
    ///   was committed. An unknown setting name is the server's own code `115`
    ///   with its did-you-mean hint; unrecognized `chtypes_*` names take the
    ///   same `115`.
    /// * [`Error::PredatesFeature`] — the artifact does not export
    ///   `chs_set_default_settings`.
    /// * [`Error::Nul`] — a name or value contained an interior NUL byte.
    pub fn set_default_settings<K: AsRef<str>, V: AsRef<str>>(
        &self,
        settings: &[(K, V)],
    ) -> Result<()> {
        let json = settings_json(settings)?;
        let _guard = self.lock();
        match self.api.set_default_settings(&json) {
            None => Err(Error::PredatesFeature {
                feature: "chs_set_default_settings",
            }),
            Some((0, _)) => Ok(()),
            Some((rc, message)) => Err(Error::Schema {
                code: rc,
                message,
                column: None,
            }),
        }
    }

    /// Diagnostic: the widened reference type this build would use for a type
    /// expression (`UInt8` -> `Int256`, `UUID` -> `String`). `None` for a type
    /// with no wider type to compare against (`String`, `Float64`).
    ///
    /// Not needed to function — the reference parse already happens inside
    /// `chs_row` and is reported per column — but it makes a transformation
    /// finding explainable.
    ///
    /// # Errors
    ///
    /// * [`Error::PredatesFeature`] — the artifact does not export
    ///   `chs_reference_type`.
    /// * [`Error::Nul`] — the expression contained an interior NUL byte.
    pub fn reference_type(&self, type_expr: &str) -> Result<Option<String>> {
        let expr = cstring(type_expr, "type expression")?;
        let _guard = self.lock();
        self.api.reference_type(&expr)
    }

    /// Every type family in this build's runtime registry, newline-separated.
    /// This is the answer to "does this build track upstream type families
    /// without a table to maintain?" — it does.
    ///
    /// # Errors
    ///
    /// * [`Error::PredatesFeature`] — the artifact does not export
    ///   `chs_registered_families`.
    pub fn registered_families(&self) -> Result<Vec<String>> {
        let _guard = self.lock();
        Ok(self
            .api
            .registered_families()?
            .lines()
            .filter(|l| !l.is_empty())
            .map(str::to_string)
            .collect())
    }

    /// TSV audit of every registered function's volatility flags, one per line:
    /// `name \t deterministic \t deterministic_in_query \t server_constant \t
    /// stateful \t resolver_error_code`. ClickHouse's own answers off this
    /// build's own registry.
    ///
    /// # Errors
    ///
    /// * [`Error::PredatesFeature`] — the artifact does not export
    ///   `chs_function_flags`.
    pub fn function_flags(&self) -> Result<String> {
        let _guard = self.lock();
        self.api.function_flags()
    }

    /// Join the DEFAULT evaluator's background threads.
    ///
    /// `chs_init` registers this with `atexit`, so an ordinary process needs no
    /// call. Call it when the host controls its own teardown order, or from a
    /// test that must not depend on `atexit`. Idempotent.
    pub fn shutdown(&self) {
        let _guard = self.lock();
        self.api.shutdown();
    }

    pub(crate) fn api(&self) -> &Api {
        &self.api
    }

    /// The per-library lock every call takes. A poisoned mutex cannot leave the
    /// native library in a bad state — every C entry point is a pure function of
    /// (build, schema text, row bytes, settings, clock instant) with no
    /// cross-call state — so the guard is recovered rather than propagated.
    pub(crate) fn lock(&self) -> std::sync::MutexGuard<'_, ()> {
        match self.lock.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        }
    }
}

// Library deliberately has NO Drop that calls `chs_shutdown`.
//
// It used to (2026-08-17, briefly): at the time, exiting via the atexit path
// alone aborted once more than one artifact had enumerated its function
// registry. That was re-diagnosed the same day as a 26.7-only wrapper defect
// and fixed IN THE ARTIFACT (chs_init now primes the thread-teardown statics
// before registering its atexit handler); with current artifacts, atexit-only
// teardown across all six loaded versions exits 0 — measured.
//
// Keeping the Drop would have been worse than useless: `dlopen` refcounts one
// image per path, so two `Library`/`Registry` instances over the same artifact
// share process-global C state — dropping the first would have run
// `Context::shutdown()` under the survivor, which then evaluates DEFAULTs
// against a shut-down context. The Go and TS bindings never call
// `chs_shutdown` implicitly for the same reason. A host that controls its own
// teardown (or intends to `dlclose`) calls [`Library::shutdown`] /
// [`Registry::shutdown`] explicitly, exactly as the C header prescribes.
//
// The shared library itself is never `dlclose`d — see `ffi`'s module docs.

impl std::fmt::Debug for Library {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Library")
            .field("version", &self.version)
            .field("minor", &self.minor)
            .field("path", &self.path)
            .finish()
    }
}

/// The introspection group's raw columns, mapped into the public [`Column`]
/// shape. `None` (an artifact predating the group) is an empty list.
pub(crate) fn columns_of(raw: Option<Vec<crate::ffi::RawColumn>>) -> Vec<Column> {
    raw.unwrap_or_default()
        .into_iter()
        .map(|c| Column {
            name: c.name,
            ty: c.ty,
            default_kind: DefaultKind::parse(&c.default_kind),
            default_expr: c.default_expr,
            default_is_literal: c.default_is_literal,
        })
        .collect()
}

/// The minor line of a reported version: the first two dot-separated components.
pub(crate) fn minor_of(version: &str) -> String {
    let mut it = version.splitn(3, '.');
    match (it.next(), it.next()) {
        (Some(a), Some(b)) => format!("{a}.{b}"),
        _ => version.to_string(),
    }
}

/// `(major, minor)` for ordering minor lines numerically. String order would put
/// `25.10` before `25.3`, which is why the spec forbids it.
pub(crate) fn minor_order(minor: &str) -> (u64, u64) {
    let mut it = minor.split('.');
    let major = it.next().and_then(|s| s.parse().ok()).unwrap_or(0);
    let line = it.next().and_then(|s| s.parse().ok()).unwrap_or(0);
    (major, line)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn minor_lines_come_from_the_reported_version() {
        assert_eq!(minor_of("25.8.28.1-lts"), "25.8");
        assert_eq!(minor_of("25.10.7.6-stable"), "25.10");
        assert_eq!(minor_of("26.7"), "26.7");
        assert_eq!(minor_of("head"), "head");
    }

    #[test]
    fn minor_lines_order_numerically_not_lexically() {
        let mut lines = vec!["25.8", "26.7", "25.10", "24.8", "25.3"];
        lines.sort_by_key(|l| minor_order(l));
        assert_eq!(lines, vec!["24.8", "25.3", "25.8", "25.10", "26.7"]);
    }

    #[test]
    fn default_kinds_are_the_abi_spellings() {
        assert_eq!(DefaultKind::parse(""), DefaultKind::None);
        assert_eq!(DefaultKind::parse("DEFAULT"), DefaultKind::Default);
        assert_eq!(
            DefaultKind::parse("MATERIALIZED"),
            DefaultKind::Materialized
        );
        assert_eq!(DefaultKind::parse("ALIAS"), DefaultKind::Alias);
        assert_eq!(DefaultKind::parse("EPHEMERAL"), DefaultKind::Ephemeral);
        assert_eq!(
            DefaultKind::parse("FUTURE"),
            DefaultKind::Other("FUTURE".into())
        );
    }
}
