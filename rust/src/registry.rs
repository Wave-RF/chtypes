//! The registry: a request for a version, to a loaded [`Library`], over the v1
//! fetch layer (`docs/reference/bindings-v1.md` §6, the sequence).
//!
//! ```text
//! registry.for_version(request)
//!   1. the registry's memo: a request it has opened before returns the same
//!      Library, for the registry's life
//!   2. resolved = resolve_installed(request, host platform, fetch options)   never the network
//!   3. on a miss:  autofetch on  -> resolved = ensure(request, fetch options)  may use the network
//!                  autofetch off -> ArtifactMissing, naming the request and the platform
//!   4. load the image from resolved.library_path with resolved.predicate, verbatim
//!   5. return the image's Library, whose resolved() is the record that first opened it
//! ```
//!
//! **Construction opens nothing.** Nothing opens an artifact except a request
//! for a version or `preload`, which opens each listed request at construction,
//! in list order, and never fetches, even with autofetch on.
//!
//! The memo keeps a line request from moving mid-process: `26.8` resolves once
//! per registry, and a newer `26.8` patch installed later is picked up by a new
//! registry, not by the old one. Which installed build a floating request means,
//! and the spelling rules, are the fetch layer's; this crate orders and matches
//! no versions itself.

use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use crate::error::{Error, Result};
use crate::library::{Library, open_image, settle_failed_open};
use crate::ocifetch::constants::ENV_AUTOFETCH_NAME;
use crate::ocifetch::ensure::{self, Options, Resolved};
use crate::setup;

/// What the fetch layer is configured with, under its own names
/// (`docs/guides/fetch-v1.md`). Every field defaults to the fetch layer's own
/// default (the environment, then the built-in value); the test hooks of the
/// fetch layer's own options are not reachable from here.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct FetchOptions {
    /// The platform key (`linux-arm64`, ...). `None` is this host's.
    pub platform: Option<String>,
    /// Base URLs, most-preferred first. `None` is `$CHTYPES_ARTIFACTS_URL`, else
    /// the built-in default.
    pub bases: Option<Vec<String>>,
    /// The OCI-layout cache root. `None` is `$CHTYPES_CACHE`, else the per-user
    /// cache.
    pub cache_dir: Option<String>,
    /// Read-only system directories, searched after the cache. `None` is the
    /// built-in list.
    pub system_dirs: Option<Vec<PathBuf>>,
    /// Never touch the network; read the cache only.
    pub offline: bool,
    /// Perform no discovery: fetch exactly the lock's pinned digests.
    pub frozen: bool,
    /// Where the lock lives. `frozen` or `lock_write` without one is a
    /// configuration error.
    pub lock_path: Option<PathBuf>,
    /// After a successful online resolve, record it in the lock.
    pub lock_write: bool,
    /// Re-resolve even if the lock already pins the request.
    pub update: bool,
    /// Proceed, with a warning, when no signed statement verifies.
    pub allow_unsigned: bool,
    /// The trust list: raw 32-byte ed25519 public keys, each as 64 hex digits.
    /// A non-empty list REPLACES the default trust (the release key); `None`
    /// reads `$CHTYPES_TRUSTED_KEYS` (comma-separated), else the release key.
    /// It never appends to the default. The SDK's own fixture key is trusted
    /// only by naming it here.
    pub trusted_keys: Option<Vec<String>>,
    /// An access token; `None` is `$CHTYPES_DOWNLOAD_TOKEN`.
    pub token: Option<String>,
}

impl FetchOptions {
    fn to_options(&self) -> Options {
        let defaults = Options::default();
        Options {
            platform: self.platform.clone(),
            bases: self.bases.clone(),
            cache_dir: self.cache_dir.clone(),
            system_dirs: self.system_dirs.clone().unwrap_or(defaults.system_dirs),
            offline: self.offline,
            frozen: self.frozen,
            lock_path: self.lock_path.clone(),
            lock_write: self.lock_write,
            update: self.update,
            allow_unsigned: self.allow_unsigned,
            trusted_keys: self.trusted_keys.clone(),
            token: self.token.clone(),
            clock: None,
            before_index_rename: None,
        }
    }
}

/// What a [`Registry`] is constructed with.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RegistryOptions {
    /// The fetch layer's options.
    pub fetch: FetchOptions,
    /// Whether a request nothing installed answers is fetched first. `None`
    /// reads `CHTYPES_AUTOFETCH` (`1` turns it on) and is off when that is
    /// unset.
    pub autofetch: Option<bool>,
    /// Requests to open at construction, in list order. Preloading never
    /// fetches, even with autofetch on.
    pub preload: Vec<String>,
}

/// A host's platform key in the fetch layer's spelling (`darwin-arm64`,
/// `linux-amd64`).
fn host_platform() -> String {
    let arch = match std::env::consts::ARCH {
        "x86_64" => "amd64",
        "aarch64" => "arm64",
        other => other,
    };
    let os = match std::env::consts::OS {
        "macos" => "darwin",
        other => other,
    };
    format!("{os}-{arch}")
}

struct Opened {
    request: String,
    library: Arc<Library>,
}

/// A request for a version, to a loaded library. `Send + Sync`.
pub struct Registry {
    fetch: FetchOptions,
    autofetch: bool,
    opened: Mutex<Vec<Opened>>,
}

impl std::fmt::Debug for Registry {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Registry")
            .field("autofetch", &self.autofetch)
            .field("fetch", &self.fetch)
            .finish_non_exhaustive()
    }
}

impl Registry {
    /// Construct a registry. It opens nothing, except each `preload` request,
    /// which is opened (never fetched) in list order.
    pub fn new(options: RegistryOptions) -> Result<Registry> {
        let autofetch = options
            .autofetch
            .unwrap_or_else(|| std::env::var(ENV_AUTOFETCH_NAME).is_ok_and(|v| v == "1"));
        let registry = Registry {
            fetch: options.fetch,
            autofetch,
            opened: Mutex::new(Vec::new()),
        };
        for request in &options.preload {
            registry.open(request, false)?;
        }
        Ok(registry)
    }

    /// Open a version (`26.8`, `26.8.15`, `26.8.15.10`: two, three or four
    /// parts, no `v` prefix and no channel suffix; a spelling the fetch layer
    /// refuses is [`Error::Usage`]). The first request opens the library; every
    /// later one for the same spelling returns the same `Arc<Library>`.
    pub fn for_version(&self, request: &str) -> Result<Arc<Library>> {
        self.open(request, true)
    }

    /// What is installed, from the fetch layer's `list_installed`.
    pub fn installed(&self) -> Result<Vec<Resolved>> {
        Ok(ensure::list_installed(self.fetch.to_options())?)
    }

    /// The libraries this registry has opened, in the order it opened them.
    pub fn libraries(&self) -> Vec<Arc<Library>> {
        let opened = self.opened.lock().unwrap_or_else(|e| e.into_inner());
        let mut out: Vec<Arc<Library>> = Vec::new();
        for o in opened.iter() {
            if !out.iter().any(|l| Arc::ptr_eq(l, &o.library)) {
                out.push(Arc::clone(&o.library));
            }
        }
        out
    }

    fn open(&self, request: &str, may_fetch: bool) -> Result<Arc<Library>> {
        // One request at a time: the memo, the resolve and the load are one
        // decision, and a registry is opened rarely and never on a hot path.
        let mut opened = self.opened.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(o) = opened.iter().find(|o| o.request == request) {
            return Ok(Arc::clone(&o.library));
        }
        // A failed open clears the setup record while no image has completed
        // load step 7, whatever failed: the spelling, the fetch, the signature
        // or any load step (bindings-v1.md section 6, rule 4).
        let began = setup::generation();
        let library = match self.resolve_and_open(request, may_fetch) {
            Ok(library) => library,
            Err(e) => {
                settle_failed_open(began);
                return Err(e);
            }
        };
        opened.push(Opened {
            request: request.to_string(),
            library: Arc::clone(&library),
        });
        Ok(library)
    }

    /// Resolve a request, fetching when allowed, and open its image: the part
    /// of [`Registry::open`] whose failure settles the setup.
    fn resolve_and_open(&self, request: &str, may_fetch: bool) -> Result<Arc<Library>> {
        let platform = self.fetch.platform.clone().unwrap_or_else(host_platform);
        let resolved = match ensure::resolve_installed(request, &platform, self.fetch.to_options())?
        {
            Some(r) => r,
            None if self.autofetch && may_fetch => {
                ensure::ensure(request, self.fetch.to_options())?
            }
            None => {
                return Err(Error::ArtifactMissing(format!(
                    "nothing installed answers {request} for {platform}{}",
                    if self.autofetch {
                        ""
                    } else {
                        " (autofetch is off: set CHTYPES_AUTOFETCH=1 or RegistryOptions::autofetch)"
                    }
                )));
            }
        };
        // The adapter: the fetch layer's record to the loader's input. The
        // predicate goes across exactly as returned, never re-encoded.
        open_image(
            &resolved.library_path.clone(),
            Some(&resolved.predicate.clone()),
            Some(resolved),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_registry_is_shareable_and_constructing_one_opens_nothing() {
        fn send_sync<T: Send + Sync>() {}
        send_sync::<Registry>();
        let r = Registry::new(RegistryOptions {
            autofetch: Some(false),
            ..Default::default()
        })
        .expect("construction opens nothing");
        assert!(r.libraries().is_empty());
    }

    #[test]
    fn a_refused_spelling_is_misuse_raised_before_anything_is_read() {
        let r = Registry::new(RegistryOptions {
            autofetch: Some(false),
            ..Default::default()
        })
        .unwrap();
        for bad in ["v25.8", "25.8-lts", "26.8-stable"] {
            let err = r.for_version(bad).unwrap_err();
            assert!(matches!(err, Error::Usage(_)), "{bad:?} gave {err:?}");
        }
    }

    #[test]
    fn a_missing_request_names_the_request_and_the_platform() {
        let dir =
            std::env::temp_dir().join(format!("chtypes_registry_test_{}", std::process::id()));
        let r = Registry::new(RegistryOptions {
            fetch: FetchOptions {
                platform: Some("linux-amd64".to_string()),
                cache_dir: Some(dir.to_string_lossy().into_owned()),
                system_dirs: Some(Vec::new()),
                offline: true,
                ..Default::default()
            },
            autofetch: Some(false),
            ..Default::default()
        })
        .unwrap();
        let err = r.for_version("25.8").unwrap_err();
        let Error::ArtifactMissing(m) = &err else {
            panic!("want ArtifactMissing, got {err:?}")
        };
        assert!(m.contains("25.8") && m.contains("linux-amd64"), "{m}");
        let _ = std::fs::remove_dir_all(dir);
    }
}
