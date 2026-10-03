//! A loaded library: one image, opened once per process.
//!
//! [`Library`] is shared as `Arc<Library>` and is `Send + Sync`: the library
//! makes every call on a compiled handle safe to run concurrently with any
//! other call, so the public layer takes no lock around a call. A library is
//! never unloaded (there is no `dlclose`, and nothing here calls
//! `chs_shutdown`), so dropping the last `Arc` releases only Rust memory.
//!
//! The loader keys an image on its resolved path plus `dev:ino`, so two
//! registries, two spellings or a hardlink of one artifact share one image and
//! one `Library`. An already-open image is still checked against every new
//! signed statement a request brings (loader steps 1, 4 and 5): a mismatch
//! refuses that request, and the image stays open for the requests it did
//! match.

use std::collections::{BTreeMap, HashSet};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};

use serde_json::{Map, Value as Json};

use crate::abi1::calls_gen::SchemaHandle;
use crate::abi1::decls::Api;
use crate::abi1::errmap_gen::UNVERIFIED_ENV;
use crate::abi1::loader::{self, LoadError, LoadInput, Refusal};
use crate::decode;
use crate::error::{Error, Result};
use crate::ocifetch::ensure::Resolved;
use crate::raw::RawText;
use crate::result::{BuildInfo, Discovery, ErrorCodeTable};
use crate::schema::{CompileOptions, Schema, settings_object};
use crate::setup;

/// One loaded library image.
pub struct Library {
    pub(crate) api: Arc<Api>,
    build_info: BuildInfo,
    build_info_map: Map<String, Json>,
    path: PathBuf,
    resolved: Option<Resolved>,
    error_codes: OnceLock<ErrorCodeTable>,
}

impl std::fmt::Debug for Library {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Library")
            .field("version", &self.build_info.clickhouse_version)
            .field("path", &self.path)
            .finish()
    }
}

struct Image {
    key: (u64, u64),
    library: Arc<Library>,
}

/// Every image this process has opened. Held across a load, so two threads
/// asking for one file load it once.
static IMAGES: Mutex<Vec<Image>> = Mutex::new(Vec::new());

/// Paths already warned about by an unverified open.
static WARNED: Mutex<Option<HashSet<PathBuf>>> = Mutex::new(None);

fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    // A poisoned mutex here only means another thread panicked while holding
    // it; the data (a list of opened images) is still a valid list.
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// Open the image at `path`, or return the one this process already opened.
/// `predicate` is the fetch layer's verified statement, verbatim; `None` is an
/// unverified open.
pub(crate) fn open_image(
    path: &Path,
    predicate: Option<&Json>,
    resolved: Option<Resolved>,
) -> Result<Arc<Library>> {
    let refusal = |reason: &str, detail: String| {
        Error::from_refusal(Refusal {
            reason: reason.to_string(),
            path: path.to_path_buf(),
            want: None,
            got: None,
            detail: Some(detail),
        })
    };
    let canonical = std::fs::canonicalize(path).map_err(|e| refusal("dlopen", e.to_string()))?;
    let meta = std::fs::metadata(&canonical).map_err(|e| refusal("dlopen", e.to_string()))?;
    let key = (meta.dev(), meta.ino());

    let mut images = lock(&IMAGES);
    if let Some(image) = images.iter().find(|i| i.key == key) {
        if let Some(predicate) = predicate {
            loader::recheck(&image.library.build_info_map, predicate, &canonical)
                .map_err(Error::from_refusal)?;
        }
        return Ok(Arc::clone(&image.library));
    }

    let setup = setup::commit();
    let loaded = loader::load(LoadInput {
        library_path: &canonical,
        predicate,
        timezone: &setup.timezone,
        defaults: setup.defaults.as_deref(),
    })
    .map_err(|e| match e {
        LoadError::Refused(r) => Error::from_refusal(r),
        LoadError::Call(c) => Error::from_call(c),
    })?;
    let build_info = decode::build_info(&loaded.build_info, &loaded.build_info_raw)
        .map_err(|detail| refusal("build_info_malformed", detail))?;
    let library = Arc::new(Library {
        api: loaded.api,
        build_info,
        build_info_map: loaded.build_info,
        path: canonical,
        resolved,
        error_codes: OnceLock::new(),
    });
    images.push(Image {
        key,
        library: Arc::clone(&library),
    });
    Ok(library)
}

impl Library {
    /// Open a local build without a signed statement: for the artifact
    /// producer's own suites over an unpublished library.
    ///
    /// It refuses with [`Error::Usage`] unless `allow` is true **and**
    /// `CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1` is set, warns once per path on
    /// standard error, and skips loader steps 1 and 5, because there is no
    /// signed statement. It runs step 7 under the process setup like any open.
    /// It is not reachable through a [`crate::Registry`], and the library it
    /// returns has no [`Library::resolved`].
    pub fn open_unverified(path: impl AsRef<Path>, allow: bool) -> Result<Arc<Library>> {
        let path = path.as_ref();
        let env_on = std::env::var(UNVERIFIED_ENV).is_ok_and(|v| v == "1");
        if !(allow && env_on) {
            return Err(Error::usage(format!(
                "an unverified open needs both opt-ins: the caller's `allow` (was {allow}) and {UNVERIFIED_ENV}=1 (was {})",
                if env_on { "set" } else { "not set" }
            )));
        }
        {
            let mut warned = lock(&WARNED);
            if warned
                .get_or_insert_with(HashSet::new)
                .insert(path.to_path_buf())
            {
                eprintln!(
                    "chtypes: opening {} WITHOUT a signed statement (an unverified local build)",
                    path.display()
                );
            }
        }
        open_image(path, None, None)
    }

    /// What this library is, from `chs_build_info`, read once at load.
    pub fn build_info(&self) -> &BuildInfo {
        &self.build_info
    }

    /// The ClickHouse version (`build_info`'s `clickhouse_version`, read rather
    /// than derived): four parts, no channel.
    pub fn version(&self) -> &str {
        &self.build_info.clickhouse_version
    }

    /// The minor line (`build_info`'s `clickhouse_minor`).
    pub fn minor(&self) -> &str {
        &self.build_info.clickhouse_minor
    }

    /// The canonical path the image was loaded from.
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// The fetch record this image was first opened by; `None` when it was
    /// opened unverified.
    pub fn resolved(&self) -> Option<&Resolved> {
        self.resolved.as_ref()
    }

    /// Canonicalize a type (`chs_type_validate`).
    pub fn validate_type(&self, type_expr: impl AsRef<[u8]>) -> Result<RawText> {
        self.call(|api| api.type_validate(type_expr.as_ref()))
            .map(RawText::from)
    }

    /// Quote an identifier, always (`chs_back_quote`).
    pub fn quote_identifier(&self, name: impl AsRef<[u8]>) -> Result<RawText> {
        self.call(|api| api.back_quote(name.as_ref()))
            .map(RawText::from)
    }

    /// Quote an identifier if it needs it (`chs_back_quote_if_needed`).
    pub fn quote_identifier_if_needed(&self, name: impl AsRef<[u8]>) -> Result<RawText> {
        self.call(|api| api.back_quote_if_needed(name.as_ref()))
            .map(RawText::from)
    }

    /// Quote a string literal (`chs_quote_string`).
    pub fn quote_literal(&self, text: impl AsRef<[u8]>) -> Result<RawText> {
        self.call(|api| api.quote_string(text.as_ref()))
            .map(RawText::from)
    }

    /// This build's error-code table (`chs_error_codes`). Built on the first
    /// call and kept for the library's life, on success only.
    pub fn error_codes(&self) -> Result<&ErrorCodeTable> {
        if let Some(table) = self.error_codes.get() {
            return Ok(table);
        }
        let bytes = self.call(|api| api.error_codes())?;
        let table = decode::error_code_table(&bytes)?;
        Ok(self.error_codes.get_or_init(|| table))
    }

    /// The discovery query (`chs_discover_query`): it selects exactly the
    /// `system.columns` fields [`Library::discover_columns`] reads, `FORMAT
    /// JSONEachRow`, with two ClickHouse query parameters, `{database:String}`
    /// and `{table:String}`. The caller runs it with its own client, binding
    /// the parameters the client's own way.
    pub fn discover_query(&self) -> Result<RawText> {
        self.call(|api| api.discover_query()).map(RawText::from)
    }

    /// Read a server's columns (`chs_discover_columns`) from the answer to
    /// [`Library::discover_query`].
    pub fn discover_columns(&self, rows: &[u8]) -> Result<Discovery> {
        decode::discovery(&self.call(|api| api.discover_columns(rows))?)
    }

    /// Live handle counts by kind (`chs_live_handles`), a diagnostic: after
    /// every handle is dropped, every count is zero.
    pub fn live_handles(&self) -> Result<BTreeMap<String, u64>> {
        decode::live_handles(&self.call(|api| api.live_handles())?)
    }

    /// Compile exactly one `CREATE TABLE` statement (`chs_schema_create`).
    pub fn compile_table(
        self: &Arc<Self>,
        create_table: impl AsRef<[u8]>,
        options: &CompileOptions,
    ) -> Result<Schema> {
        let settings = settings_object(&options.settings, options.session_timezone.as_deref())?;
        let handle: SchemaHandle =
            self.call(|api| api.schema_create(create_table.as_ref(), &settings))?;
        Ok(Schema::new(Arc::clone(self), handle))
    }

    /// Run one generated call and map its error by `sdk.json`'s status table.
    pub(crate) fn call<T>(
        &self,
        f: impl FnOnce(&Arc<Api>) -> std::result::Result<T, crate::abi1::calls_gen::RawCallError>,
    ) -> Result<T> {
        f(&self.api).map_err(Error::from_call)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_unverified_open_needs_both_opt_ins_and_names_what_is_missing() {
        // `allow` false refuses whatever the environment holds, before any file
        // is touched.
        let Err(Error::Usage(c)) = Library::open_unverified("/nonexistent/lib.so", false) else {
            panic!("want a usage error")
        };
        assert!(c.message.to_lossy().contains("allow"), "{c}");
        assert_eq!(c.status, crate::status::INVALID_ARGUMENT);
    }

    #[test]
    fn a_library_is_shareable_across_threads() {
        fn send_sync<T: Send + Sync>() {}
        send_sync::<Library>();
        send_sync::<Arc<Library>>();
    }
}
