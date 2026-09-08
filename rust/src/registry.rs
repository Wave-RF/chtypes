//! The artifact registry: a directory of one subdirectory per ClickHouse minor
//! line, each holding a `manifest.json` and the shared library it names.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde::Deserialize;

use crate::error::{Error, Result};
use crate::library::{DEFAULT_TIMEZONE, Library, minor_of, minor_order};

/// The environment variable a host may point at a registry directory.
pub const REGISTRY_ENV: &str = "CHTYPES_REGISTRY";

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
    /// `arm64` | `amd64`, normalised.
    #[serde(default)]
    pub arch: String,
    /// The generated refuse-list, inline. `unsafe_families.txt` next to the
    /// library is what is actually passed to `chs_init`.
    #[serde(default)]
    pub unsafe_families: String,
}

/// Every artifact under one directory, indexed by version.
///
/// Each library is loaded with `RTLD_NOW | RTLD_LOCAL`, which is what lets two
/// builds that both define `DB::DataTypeFactory` answer in one process. Cost,
/// measured: roughly 120 MB resident per loaded version.
pub struct Registry {
    dir: PathBuf,
    /// Indexed under both the exact patch and the minor line.
    by_id: HashMap<String, Arc<Library>>,
    libraries: Vec<Arc<Library>>,
}

impl Registry {
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

        let mut reg = Registry {
            dir: dir.clone(),
            by_id: HashMap::new(),
            libraries: Vec::new(),
        };

        for sub in entries {
            if !sub.is_dir() {
                continue;
            }
            // A registry may legitimately hold scratch directories, and a
            // .DS_Store is not a version: a missing or unparseable manifest is
            // skipped silently.
            let Ok(text) = std::fs::read_to_string(sub.join("manifest.json")) else {
                continue;
            };
            let Ok(manifest) = serde_json::from_str::<Manifest>(&text) else {
                continue;
            };
            if manifest.library.is_empty() {
                continue;
            }
            let path = sub.join(&manifest.library);

            // A directory that has a manifest and does not load is broken, not
            // absent, so everything from here on is an error rather than a skip.
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
            if !manifest.clickhouse_version.is_empty()
                && manifest.clickhouse_version != library.version()
            {
                return Err(Error::VersionMismatch {
                    path,
                    reported: library.version().to_string(),
                    manifest: manifest.clickhouse_version,
                });
            }

            // Indexed under BOTH the exact version and the minor line: docker tags
            // drift, and an exact-match-only lookup silently loses a whole version
            // column.
            reg.by_id
                .insert(library.version().to_string(), Arc::clone(&library));
            reg.by_id
                .insert(library.minor().to_string(), Arc::clone(&library));
            reg.libraries.push(library);
        }

        if reg.libraries.is_empty() {
            return Err(Error::EmptyRegistry { dir });
        }
        // Release order, not scan order: the directory listing is lexical,
        // which put 25.10 before 25.8 (spec/bindings.md §Version selection,
        // rule 2 — every ordered surface uses numeric release order; fixed
        // 2026-08-26).
        reg.libraries.sort_by_key(|l| minor_order(l.minor()));
        Ok(reg)
    }

    /// The directory this registry was loaded from.
    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// The ClickHouse minor lines this registry can answer for, ordered
    /// numerically — `25.10` is a *later* line than `25.8`, so lexical order would
    /// be wrong.
    pub fn versions(&self) -> Vec<String> {
        let mut out: Vec<String> = self
            .libraries
            .iter()
            .map(|l| l.minor().to_string())
            .collect();
        out.sort_by_key(|m| minor_order(m));
        out.dedup();
        out
    }

    /// Every loaded library, in release order (oldest minor line first —
    /// `25.10` after `25.8`, whatever the directory listing said).
    pub fn libraries(&self) -> &[Arc<Library>] {
        &self.libraries
    }

    /// Resolve a version to its library. A minor line (`"25.8"`) or an exact patch
    /// (`"25.8.28.1-lts"`) both work, and a *drifted* patch resolves to its minor
    /// line — asking for `"25.8.30.16"` finds the loaded `25.8`.
    ///
    /// Failure names what is loaded and never falls back to the nearest version:
    /// answering 26.7 semantics from a 25.8 artifact would be a lie, and version
    /// behaviour is not monotonic (25.10 rejects a mixed-type DEFAULT that 25.8
    /// and 26.6 both accept).
    ///
    /// # Errors
    ///
    /// * [`Error::NoSuchVersion`] — no loaded artifact answers for `version`;
    ///   the message names the minor lines that are loaded.
    pub fn for_version(&self, version: &str) -> Result<Arc<Library>> {
        if let Some(l) = self.by_id.get(version) {
            return Ok(Arc::clone(l));
        }
        if let Some(l) = self.by_id.get(&minor_of(version)) {
            return Ok(Arc::clone(l));
        }
        Err(Error::NoSuchVersion {
            requested: version.to_string(),
            loaded: self.versions().join(", "),
        })
    }

    /// Join every loaded library's DEFAULT-evaluator threads. `chs_init`
    /// registers this with `atexit`, so an ordinary process needs no call.
    pub fn shutdown(&self) {
        for l in &self.libraries {
            l.shutdown();
        }
    }
}

impl std::fmt::Debug for Registry {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Registry")
            .field("dir", &self.dir)
            .field("versions", &self.versions())
            .finish()
    }
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
    fn the_historical_linux_library_name_is_honoured() {
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

/// The per-user artifact cache for this host —
/// `${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/<os>-<arch>`, `<arch>`
/// spelled the artifact way (`amd64`, `arm64`). Where `scripts/fetch.sh`
/// installs, where a core-repository build lands, and what every SDK's tests
/// and playgrounds fall back to when `$CHTYPES_REGISTRY` is unset — one
/// directory the four SDKs agree on. A path, not a promise: [`Registry::new`]
/// still errors if nothing is there.
pub fn default_registry_dir() -> PathBuf {
    let base = std::env::var_os("XDG_CACHE_HOME")
        .map(PathBuf::from)
        .filter(|p| !p.as_os_str().is_empty())
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".cache")))
        .unwrap_or_else(|| PathBuf::from(".cache"));
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
    base.join("chtypes")
        .join("artifacts")
        .join(format!("{os}-{arch}"))
}
