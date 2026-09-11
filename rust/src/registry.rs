//! The artifact registry: a directory of one subdirectory per ClickHouse minor
//! line, each holding a `manifest.json` and the shared library it names.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, RwLock};

use serde::Deserialize;

use crate::error::{Error, Result};
use crate::library::{DEFAULT_TIMEZONE, Library, minor_of, minor_order};

/// The environment variable a host may point at a registry directory.
pub const REGISTRY_ENV: &str = "CHTYPES_REGISTRY";

/// `CHTYPES_AUTOFETCH=1` turns lazy fetch on for a search-path registry
/// ([`Registry::from_search_path`]): opening a missing line runs
/// [`crate::ensure`] first (`docs/guides/fetch.md` §6). Off by default, because a
/// production process must not begin a 250 MB download inside a request.
pub const AUTOFETCH_ENV: &str = "CHTYPES_AUTOFETCH";

/// The system registry roots, `docs/guides/fetch.md` §1 item 4 — searched after the
/// per-user cache, never written by fetch. `<os>-<arch>` is appended.
pub const SYSTEM_ARTIFACT_ROOTS: [&str; 2] = [
    "/usr/local/share/chtypes/artifacts",
    "/opt/chtypes/artifacts",
];

/// One artifact's `manifest.json`.
///
/// Unknown fields are ignored and absent fields default: the file grows
/// additively, and a loader that required a field would break on the next
/// addition.
#[derive(Debug, Clone, Deserialize)]
pub struct Manifest {
    /// **The shared library's file name, and the loader's only source of it.**
    /// Not a hard-coded `libchtypes.so`, not a glob, not the platform: the Linux
    /// artifacts in the current matrix still carry the historical
    /// `libchtypes_s1.so`, so a constructed name finds nothing on the shipping
    /// platform.
    pub library: String,
    /// Size of that file. A cheap first-pass integrity check, and the one that
    /// catches a move that reported success and truncated a 232 MB library.
    #[serde(default)]
    pub library_bytes: u64,
    /// SHA-256 of that file — the only integrity check that means anything. This
    /// crate does not hash (it takes no crypto dependency); use
    /// `chtypes-core/ci/cache.sh verify_artifacts` / `just verify-artifacts`, and verify before
    /// load when the artifact came over a network.
    #[serde(default)]
    pub library_sha256: String,
    /// The exact release. Cross-checked against `chs_clickhouse_version()`, never
    /// trusted over it.
    #[serde(default)]
    pub clickhouse_version: String,
    /// The minor line. Informational: the minor is derived from the library's own
    /// reported version instead.
    #[serde(default)]
    pub clickhouse_minor: String,
    /// The upstream commit the tree was built from — the only field tying an
    /// artifact to a specific ClickHouse source state.
    #[serde(default)]
    pub clickhouse_commit: String,
    /// `linux` | `darwin`, lowercased.
    #[serde(default)]
    pub os: String,
    /// `arm64` | `amd64`, normalized.
    #[serde(default)]
    pub arch: String,
    /// The generated refuse-list, inline. `unsafe_families.txt` next to the
    /// library is what is actually passed to `chs_init`.
    #[serde(default)]
    pub unsafe_families: String,
}

/// Every artifact under one directory, indexed by version — or, built with
/// [`Registry::from_search_path`], the `docs/guides/fetch.md` §1 search path opened
/// one line at a time.
///
/// Each library is loaded with `RTLD_NOW | RTLD_LOCAL`, which is what lets two
/// builds that both define `DB::DataTypeFactory` answer in one process. Cost,
/// measured: roughly 120 MB resident per loaded version.
///
/// Two shapes, one type:
///
/// * **One directory, eager** — [`Registry::new`] and its `from_env*`
///   variants scan the directory and load every artifact in it up front.
///   [`Registry::for_version`] answers from what is loaded and fails with
///   [`Error::NoSuchVersion`], naming what is loaded.
/// * **The search path, lazy** — [`Registry::from_search_path`] loads nothing
///   until a line is asked for, then takes the first directory on the §1
///   search path that holds it. A line found nowhere is
///   [`Error::ArtifactMissing`] (§7), or — with autofetch on — is fetched
///   first.
pub struct Registry {
    /// Eager: the one directory. Lazy: where fetch writes ([`install_dir`]).
    dir: PathBuf,
    /// Eager: `[dir]`. Lazy: the §1 search path, in order.
    search: Vec<PathBuf>,
    lazy: bool,
    /// `docs/guides/fetch.md` §1 item 1, kept for autofetch's `dest`.
    #[cfg_attr(not(feature = "fetch"), allow(dead_code))]
    explicit: Option<PathBuf>,
    timezone: String,
    autofetch: bool,
    #[cfg(feature = "fetch")]
    fetch: crate::fetch::EnsureOptions,
    loaded: RwLock<Loaded>,
    /// Serializes the lazy open path, so two threads asking for one line load
    /// it once.
    opening: Mutex<()>,
}

/// What a registry has loaded, indexed under both the exact patch and the
/// minor line; `libraries` is kept in release order.
#[derive(Default)]
struct Loaded {
    by_id: HashMap<String, Arc<Library>>,
    libraries: Vec<Arc<Library>>,
}

impl Loaded {
    fn insert(&mut self, library: Arc<Library>) {
        // Indexed under BOTH the exact version and the minor line: docker tags
        // drift, and an exact-match-only lookup silently loses a whole version
        // column.
        self.by_id
            .insert(library.version().to_string(), Arc::clone(&library));
        self.by_id
            .insert(library.minor().to_string(), Arc::clone(&library));
        self.libraries.push(library);
        // Release order, not scan order: the directory listing is lexical,
        // which put 25.10 before 25.8 (docs/reference/bindings.md §Version selection,
        // rule 2 — every ordered surface uses numeric release order; fixed
        // 2026-08-26).
        self.libraries.sort_by_key(|l| minor_order(l.minor()));
    }

    fn get(&self, version: &str) -> Option<Arc<Library>> {
        if let Some(l) = self.by_id.get(version) {
            return Some(Arc::clone(l));
        }
        self.by_id.get(&minor_of(version)).map(Arc::clone)
    }
}

/// Options for a search-path registry ([`Registry::from_search_path_with`]).
/// The default is the §1 search path for this host, `UTC`, and autofetch
/// from `CHTYPES_AUTOFETCH`.
#[derive(Debug, Clone, Default)]
pub struct RegistryOptions {
    /// An explicit registry directory — `docs/guides/fetch.md` §1 item 1, searched
    /// first and written to by fetch.
    pub dir: Option<PathBuf>,
    /// The server timezone for bare `DateTime` columns; `UTC` when `None`.
    /// Never the host's `TZ`.
    pub timezone: Option<String>,
    /// Lazy fetch on first open (`docs/guides/fetch.md` §6). `None` reads
    /// `CHTYPES_AUTOFETCH`; `Some(true)` turns it on regardless.
    pub autofetch: Option<bool>,
    /// How an autofetch fetches — source, tag, lock, trust policy. Its `dest`
    /// is overridden by [`RegistryOptions::dir`], so the fetched line lands
    /// where this registry looks first.
    #[cfg(feature = "fetch")]
    pub fetch: crate::fetch::EnsureOptions,
}

impl Registry {
    /// A registry over the `docs/guides/fetch.md` §1 search path for this host, with
    /// the defaults of [`RegistryOptions`]. Nothing is loaded until
    /// [`Registry::for_version`] asks for a line; see the type docs.
    pub fn from_search_path() -> Registry {
        Registry::from_search_path_with(RegistryOptions::default())
    }

    /// [`Registry::from_search_path`] with an explicit directory, timezone or
    /// autofetch setting. Infallible: the search path is a list of places to
    /// look, and a missing line is reported when it is asked for.
    pub fn from_search_path_with(opts: RegistryOptions) -> Registry {
        let autofetch = opts.autofetch.unwrap_or_else(|| {
            std::env::var(AUTOFETCH_ENV)
                .map(|v| v == "1")
                .unwrap_or(false)
        });
        Registry {
            dir: install_dir(opts.dir.as_deref()),
            search: registry_search_path(opts.dir.as_deref()),
            lazy: true,
            explicit: opts.dir,
            timezone: opts
                .timezone
                .unwrap_or_else(|| DEFAULT_TIMEZONE.to_string()),
            autofetch,
            #[cfg(feature = "fetch")]
            fetch: opts.fetch,
            loaded: RwLock::new(Loaded::default()),
            opening: Mutex::new(()),
        }
    }

    /// Load every artifact under `dir`, with the `UTC` server timezone the spec
    /// requires. See [`Registry::with_timezone`] for the errors.
    pub fn new(dir: impl AsRef<Path>) -> Result<Registry> {
        Registry::with_timezone(dir, DEFAULT_TIMEZONE)
    }

    /// Load every artifact under `$CHTYPES_REGISTRY`.
    ///
    /// # Errors
    ///
    /// [`Error::NoRegistryEnv`] when the variable is unset; otherwise as
    /// [`Registry::with_timezone`].
    pub fn from_env() -> Result<Registry> {
        let dir =
            std::env::var_os(REGISTRY_ENV).ok_or(Error::NoRegistryEnv { var: REGISTRY_ENV })?;
        Registry::new(PathBuf::from(dir))
    }

    /// [`Registry::from_env_or`] with [`default_registry_dir`] as the fallback:
    /// `$CHTYPES_REGISTRY`, else the per-user artifact cache for this host.
    pub fn from_env_or_default() -> Result<Registry> {
        Registry::from_env_or(default_registry_dir())
    }

    /// `$CHTYPES_REGISTRY` when it is set, otherwise `fallback`. See
    /// [`Registry::with_timezone`] for the errors.
    pub fn from_env_or(fallback: impl AsRef<Path>) -> Result<Registry> {
        match std::env::var_os(REGISTRY_ENV) {
            Some(dir) => Registry::new(PathBuf::from(dir)),
            None => Registry::new(fallback),
        }
    }

    /// [`Registry::new`] with an explicit server timezone for bare `DateTime`
    /// columns. Only pass something other than `UTC` when you know the target
    /// server's timezone; the host's `TZ` must never decide it.
    ///
    /// A subdirectory without a readable `manifest.json` is skipped silently
    /// (a registry may hold scratch directories); a directory that HAS a
    /// manifest and then fails to load is broken, not absent, and aborts the
    /// scan with an error.
    ///
    /// # Errors
    ///
    /// * [`Error::Registry`] — the directory (or a manifested library file)
    ///   could not be read.
    /// * [`Error::CorruptArtifact`] — a library's size on disk disagrees with
    ///   its manifest's `library_bytes`.
    /// * [`Error::VersionMismatch`] — `chs_clickhouse_version()` disagrees
    ///   with the manifest's `clickhouse_version`.
    /// * [`Error::EmptyRegistry`] — the directory held no loadable artifact;
    ///   an empty registry is a configuration mistake, not an empty result.
    /// * Everything [`Library::load`] can return, per artifact.
    pub fn with_timezone(dir: impl AsRef<Path>, timezone: &str) -> Result<Registry> {
        let dir = dir.as_ref().to_path_buf();
        let mut entries: Vec<PathBuf> = std::fs::read_dir(&dir)
            .map_err(|source| Error::Registry {
                dir: dir.clone(),
                source,
            })?
            .filter_map(|e| e.ok())
            .map(|e| e.path())
            .collect();
        entries.sort();

        let mut loaded = Loaded::default();
        for sub in entries {
            if !sub.is_dir() {
                continue;
            }
            if let Some(library) = load_artifact_dir(&sub, timezone)? {
                loaded.insert(library);
            }
        }

        if loaded.libraries.is_empty() {
            return Err(Error::EmptyRegistry { dir });
        }
        Ok(Registry {
            search: vec![dir.clone()],
            dir,
            lazy: false,
            explicit: None,
            timezone: timezone.to_string(),
            autofetch: false,
            #[cfg(feature = "fetch")]
            fetch: crate::fetch::EnsureOptions::default(),
            loaded: RwLock::new(loaded),
            opening: Mutex::new(()),
        })
    }

    /// The directory this registry was loaded from — for a search-path
    /// registry, the directory fetch writes to ([`install_dir`]).
    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// Where this registry looks, in order: the one directory, or the §1
    /// search path of a [`Registry::from_search_path`] registry.
    pub fn search_path(&self) -> &[PathBuf] {
        &self.search
    }

    /// Whether opening a missing line fetches it first (`docs/guides/fetch.md` §6).
    /// Always `false` for a one-directory registry.
    pub fn autofetch(&self) -> bool {
        self.autofetch
    }

    /// The ClickHouse minor lines this registry can answer for, ordered
    /// numerically — `25.10` is a *later* line than `25.8`, so lexical order would
    /// be wrong. For a search-path registry: every line installed somewhere on
    /// the path (loaded or not), each answered from its first directory.
    pub fn versions(&self) -> Vec<String> {
        let mut out: Vec<String> = self
            .loaded()
            .libraries
            .iter()
            .map(|l| l.minor().to_string())
            .collect();
        if self.lazy {
            out.extend(installed_lines(&self.search).into_iter().map(|(m, _)| m));
        }
        out.sort_by_key(|m| minor_order(m));
        out.dedup();
        out
    }

    /// Every loaded library, in release order (oldest minor line first —
    /// `25.10` after `25.8`, whatever the directory listing said). A
    /// search-path registry lists what has been opened so far.
    pub fn libraries(&self) -> Vec<Arc<Library>> {
        self.loaded().libraries.clone()
    }

    fn loaded(&self) -> std::sync::RwLockReadGuard<'_, Loaded> {
        self.loaded.read().unwrap_or_else(|p| p.into_inner())
    }

    /// Resolve a version to its library. A minor line (`"25.8"`) or an exact patch
    /// (`"25.8.28.1-lts"`) both work, and a *drifted* patch resolves to its minor
    /// line — asking for `"25.8.30.16"` finds the loaded `25.8`.
    ///
    /// Failure names what is loaded and never falls back to the nearest version:
    /// answering 26.7 semantics from a 25.8 artifact would be a lie, and version
    /// behavior is not monotonic (25.10 rejects a mixed-type DEFAULT that 25.8
    /// and 26.6 both accept).
    ///
    /// # Errors
    ///
    /// * [`Error::NoSuchVersion`] — no loaded artifact answers for `version`;
    ///   the message names the minor lines that are loaded.
    ///
    /// A search-path registry resolves a line it has not loaded yet by taking
    /// the first directory on its path that holds `<minor>/manifest.json`
    /// (`docs/guides/fetch.md` §1) and loading that one artifact. A line found
    /// nowhere is [`Error::ArtifactMissing`] — or, with autofetch on, is
    /// fetched first through [`crate::ensure`] (once per process per line,
    /// under one process-wide lock, so concurrent opens fetch once) and the
    /// fetch's own error surfaces when that fails.
    pub fn for_version(&self, version: &str) -> Result<Arc<Library>> {
        if let Some(l) = self.loaded().get(version) {
            return Ok(l);
        }
        if !self.lazy {
            return Err(Error::NoSuchVersion {
                requested: version.to_string(),
                loaded: self.versions().join(", "),
            });
        }
        let _opening = self.opening.lock().unwrap_or_else(|p| p.into_inner());
        // Another thread may have opened it while this one waited.
        if let Some(l) = self.loaded().get(version) {
            return Ok(l);
        }
        let minor = minor_of(version);
        let dir = match locate_in(&self.search, &minor) {
            Some(dir) => dir,
            None => self.autofetch_or_missing(&minor)?,
        };
        let library = load_artifact_dir(&dir, &self.timezone)?.ok_or_else(|| Error::Registry {
            dir: dir.clone(),
            source: std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "manifest.json is present but names no library",
            ),
        })?;
        self.loaded
            .write()
            .unwrap_or_else(|p| p.into_inner())
            .insert(Arc::clone(&library));
        Ok(library)
    }

    /// The miss path of a search-path registry: fetch (when autofetch is on)
    /// or the §7 error.
    fn autofetch_or_missing(&self, minor: &str) -> Result<PathBuf> {
        if !self.autofetch {
            return Err(self.missing(minor));
        }
        #[cfg(feature = "fetch")]
        {
            self.autofetch_line(minor)
        }
        #[cfg(not(feature = "fetch"))]
        {
            Err(Error::Fetch {
                message: format!(
                    "autofetch was requested for ClickHouse {minor} but this build of chtypes \
                     has the `fetch` feature disabled"
                ),
            })
        }
    }

    /// `docs/guides/fetch.md` §6: one process-wide lock, one attempt per line.
    #[cfg(feature = "fetch")]
    fn autofetch_line(&self, minor: &str) -> Result<PathBuf> {
        static GUARD: Mutex<std::collections::BTreeSet<String>> =
            Mutex::new(std::collections::BTreeSet::new());
        let mut attempted = GUARD.lock().unwrap_or_else(|p| p.into_inner());
        // Whoever held the lock before may have installed it.
        if let Some(dir) = locate_in(&self.search, minor) {
            return Ok(dir);
        }
        if !attempted.insert(minor.to_string()) {
            return Err(self.missing(minor));
        }
        let opts = crate::fetch::EnsureOptions {
            dest: self.explicit.clone(),
            ..self.fetch.clone()
        };
        Ok(crate::fetch::ensure(minor, &opts)?.dir)
    }

    fn missing(&self, minor: &str) -> Error {
        Error::ArtifactMissing {
            line: minor.to_string(),
            platform: host_platform(),
            looked_in: self.search.clone(),
        }
    }

    /// Join every loaded library's DEFAULT-evaluator threads. `chs_init`
    /// registers this with `atexit`, so an ordinary process needs no call.
    pub fn shutdown(&self) {
        for l in &self.loaded().libraries {
            l.shutdown();
        }
    }
}

/// Load the artifact in one registry subdirectory, the way every registry
/// shape does it: `Ok(None)` when there is no usable `manifest.json` (a
/// registry may hold scratch directories, and a `.DS_Store` is not a version);
/// an error when there IS a manifest and the library does not load — broken,
/// not absent.
fn load_artifact_dir(sub: &Path, timezone: &str) -> Result<Option<Arc<Library>>> {
    let Ok(text) = std::fs::read_to_string(sub.join("manifest.json")) else {
        return Ok(None);
    };
    let Ok(manifest) = serde_json::from_str::<Manifest>(&text) else {
        return Ok(None);
    };
    if manifest.library.is_empty() {
        return Ok(None);
    }
    let path = sub.join(&manifest.library);

    if manifest.library_bytes > 0 {
        let actual = std::fs::metadata(&path)
            .map_err(|source| Error::Registry {
                dir: path.clone(),
                source,
            })?
            .len();
        if actual != manifest.library_bytes {
            return Err(Error::CorruptArtifact {
                path,
                expected: manifest.library_bytes,
                actual,
            });
        }
    }

    let library = Arc::new(Library::load(&path, timezone)?);

    // The one class of corruption a checksum cannot catch: the right bytes
    // in the wrong directory.
    if !manifest.clickhouse_version.is_empty() && manifest.clickhouse_version != library.version() {
        return Err(Error::VersionMismatch {
            path,
            reported: library.version().to_string(),
            manifest: manifest.clickhouse_version,
        });
    }
    Ok(Some(library))
}

impl std::fmt::Debug for Registry {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Registry")
            .field("dir", &self.dir)
            .field("versions", &self.versions())
            .finish()
    }
}

/// This host's platform key, `<os>-<arch>` in the artifact spelling: `linux`
/// or `darwin`, `arm64` or `amd64` (`docs/guides/fetch.md` §1).
pub fn host_platform() -> String {
    let arch = match std::env::consts::ARCH {
        "x86_64" => "amd64",
        "aarch64" => "arm64",
        other => other,
    };
    // The artifact key spells macOS `darwin` (uname -s, lowercased), where Rust
    // says `macos`; every other OS name agrees between the two.
    let os = match std::env::consts::OS {
        "macos" => "darwin",
        other => other,
    };
    format!("{os}-{arch}")
}

/// The per-user artifact cache for one platform —
/// `${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/<platform>` (`docs/guides/fetch.md`
/// §1 item 3). Where fetch installs, where a core-repository build lands.
pub fn cache_dir_for(platform: &str) -> PathBuf {
    let base = std::env::var_os("XDG_CACHE_HOME")
        .map(PathBuf::from)
        .filter(|p| !p.as_os_str().is_empty())
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".cache")))
        .unwrap_or_else(|| PathBuf::from(".cache"));
    base.join("chtypes").join("artifacts").join(platform)
}

/// The per-user artifact cache for this host —
/// `${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/<os>-<arch>`, `<arch>`
/// spelled the artifact way (`amd64`, `arm64`). Where `scripts/fetch.sh` and
/// [`crate::ensure`] install, where a core-repository build lands, and what
/// every SDK's tests and playgrounds fall back to when `$CHTYPES_REGISTRY` is
/// unset — one directory the four SDKs agree on. A path, not a promise:
/// [`Registry::new`] still errors if nothing is there.
pub fn default_registry_dir() -> PathBuf {
    cache_dir_for(&host_platform())
}

/// The `docs/guides/fetch.md` §1 search path for this host, in order: `explicit`,
/// `$CHTYPES_REGISTRY`, the per-user cache, then the system locations
/// ([`SYSTEM_ARTIFACT_ROOTS`]). Unset entries are absent; directories that
/// do not exist are kept, so the §7 message can name every place looked in.
pub fn registry_search_path(explicit: Option<&Path>) -> Vec<PathBuf> {
    search_path_for(&host_platform(), explicit)
}

/// [`registry_search_path`] for an arbitrary platform key. `$CHTYPES_REGISTRY`
/// names this host's registry and joins the path only for the host platform;
/// a foreign platform (Linux artifacts fetched on a Mac, for a container) is
/// looked for in its own cache and system directories.
pub fn search_path_for(platform: &str, explicit: Option<&Path>) -> Vec<PathBuf> {
    let mut out = Vec::with_capacity(5);
    if let Some(d) = explicit {
        out.push(d.to_path_buf());
    }
    if platform == host_platform() {
        if let Some(d) = std::env::var_os(REGISTRY_ENV).filter(|v| !v.is_empty()) {
            out.push(PathBuf::from(d));
        }
    }
    out.push(cache_dir_for(platform));
    for root in SYSTEM_ARTIFACT_ROOTS {
        out.push(Path::new(root).join(platform));
    }
    out
}

/// Where fetch writes for this host: the first of `explicit`,
/// `$CHTYPES_REGISTRY` and the per-user cache that is set — never a system
/// location (`docs/guides/fetch.md` §1).
pub fn install_dir(explicit: Option<&Path>) -> PathBuf {
    install_dir_for(&host_platform(), explicit)
}

/// [`install_dir`] for an arbitrary platform key; see [`search_path_for`] for
/// why `$CHTYPES_REGISTRY` only counts for the host platform.
pub fn install_dir_for(platform: &str, explicit: Option<&Path>) -> PathBuf {
    search_path_for(platform, explicit)
        .into_iter()
        .next()
        .unwrap_or_else(|| cache_dir_for(platform))
}

/// The first directory on this host's search path that holds `line` —
/// `<dir>/<minor>/manifest.json` exists — returned as `<dir>/<minor>`. `None`
/// when no directory does. `line` may be a minor line or an exact patch; the
/// minor is what is looked for, as [`Registry::for_version`] resolves it.
pub fn locate(line: &str, explicit: Option<&Path>) -> Option<PathBuf> {
    locate_in(&registry_search_path(explicit), line)
}

/// [`locate`] over an explicit list of registry directories, in order.
pub fn locate_in(dirs: &[PathBuf], line: &str) -> Option<PathBuf> {
    let minor = minor_of(line);
    dirs.iter()
        .map(|d| d.join(&minor))
        .find(|sub| sub.join("manifest.json").is_file())
}

/// Every minor line installed somewhere on `dirs`, each with the FIRST
/// directory that holds it (the one lookup would take), in numeric release
/// order. A subdirectory counts when it holds a `manifest.json`.
pub fn installed_lines(dirs: &[PathBuf]) -> Vec<(String, PathBuf)> {
    let mut seen: Vec<(String, PathBuf)> = Vec::new();
    for dir in dirs {
        let Ok(entries) = std::fs::read_dir(dir) else {
            continue;
        };
        for entry in entries.filter_map(|e| e.ok()) {
            let sub = entry.path();
            if !sub.join("manifest.json").is_file() {
                continue;
            }
            let Some(minor) = sub.file_name().and_then(|n| n.to_str()) else {
                continue;
            };
            if minor.starts_with('.') || seen.iter().any(|(m, _)| m == minor) {
                continue;
            }
            seen.push((minor.to_string(), sub));
        }
    }
    seen.sort_by_key(|(m, _)| minor_order(m));
    seen
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_manifest_parses_and_tolerates_new_fields() {
        let m: Manifest = serde_json::from_str(
            r#"{"arch":"arm64","clickhouse_commit":"cfec8e0","clickhouse_minor":"25.8",
                "clickhouse_version":"25.8.28.1-lts","library":"libchtypes.dylib",
                "library_bytes":232226512,"library_sha256":"275c39","os":"darwin",
                "unsafe_families":"","some_future_field":true}"#,
        )
        .unwrap();
        assert_eq!(m.library, "libchtypes.dylib");
        assert_eq!(m.library_bytes, 232226512);
        assert_eq!(m.clickhouse_version, "25.8.28.1-lts");
    }

    #[test]
    fn the_historical_linux_library_name_is_honored() {
        let m: Manifest =
            serde_json::from_str(r#"{"library":"libchtypes_s1.so","os":"linux"}"#).unwrap();
        assert_eq!(m.library, "libchtypes_s1.so");
    }

    #[test]
    fn an_empty_registry_is_an_error_not_an_empty_result() {
        let dir = std::env::temp_dir().join(format!("chtypes-rs-empty-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let err = Registry::new(&dir).unwrap_err();
        assert!(matches!(err, Error::EmptyRegistry { .. }), "got {err:?}");
        std::fs::remove_dir_all(&dir).ok();
    }
}
