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
//!
//! **An open never waits for another request's fetch** (public issue #491). The
//! registry's lock guards the memo and the attempts in progress, and is never
//! held across steps 2 to 5. Concurrent opens of one request share one attempt,
//! and so one fetch, and each gets that attempt's `Library` or its error; a
//! failed attempt is never remembered, so the next open starts a new one.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};

use crate::error::{Error, Refusal, Result};
use crate::library::{Library, open_image, settle_failed_open};
use crate::ocifetch::constants::ENV_AUTOFETCH_NAME;
use crate::ocifetch::ensure::{self, Options, Resolved};
use crate::ocifetch::layout;
use crate::ocifetch::oci::VersionRequest;
use crate::setup;

/// What the fetch layer is configured with, under its own names
/// (`docs/guides/fetch-v1.md`). Every field defaults to the fetch layer's own
/// default (the environment, then the built-in value); the test hooks of the
/// fetch layer's own options are not reachable from here.
///
/// **2.0.0-dev.** This SDK speaks the ABI v2 dev channel (spec/abi-v2/docs.md,
/// rules r5 and r6): `bases`, `trusted_keys` and `allow_unsigned` (and
/// `CHTYPES_ARTIFACTS_URL`, `CHTYPES_TRUSTED_KEYS` and `CHTYPES_ALLOW_UNSIGNED`)
/// are ignored, each with one warning per process; `frozen`, `lock_path`,
/// `lock_write` and `update` are refused as [`Error::Usage`] before any
/// network call; and `cache_dir` (or `CHTYPES_CACHE`) is used through its
/// subroot `<cache_dir>/v2-dev`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct FetchOptions {
    /// Base URLs, most-preferred first. `None` is `$CHTYPES_ARTIFACTS_URL`, else
    /// the built-in default. Ignored by this 2.0.0-dev SDK, which fetches only
    /// from `https://registry-staging.wavehouse.dev/chtypes/v2-dev`.
    pub bases: Option<Vec<String>>,
    /// The cache. `None` is `$CHTYPES_CACHE`, else the per-user cache
    /// (`${XDG_CACHE_HOME:-~/.cache}/chtypes/v2-dev`); an explicit cache is used
    /// through its subroot `<cache_dir>/v2-dev`.
    pub cache_dir: Option<String>,
    /// Read-only system directories, searched after the cache. `None` is the
    /// built-in list.
    pub system_dirs: Option<Vec<PathBuf>>,
    /// Never touch the network; read the cache only. `CHTYPES_OFFLINE=1` is the
    /// same mode (public issue #528); offline is on when this is set OR the variable is `1`; neither turns the other off.
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
    /// only by naming it here. Ignored by this 2.0.0-dev SDK, which trusts only
    /// the staging key.
    pub trusted_keys: Option<Vec<String>>,
    /// An access token; `None` is `$CHTYPES_DOWNLOAD_TOKEN`.
    pub token: Option<String>,
    /// Strict mode: every fault of the cache and of an existing system dir is
    /// [`Error::CacheUnusable`] naming the path, never "not installed", and
    /// never a fall-through to a system dir. `None` reads
    /// `CHTYPES_CACHE_STRICT` (`1` is on), else off.
    pub strict_cache: Option<bool>,
}

impl FetchOptions {
    fn to_options(&self) -> Options {
        let defaults = Options::default();
        Options {
            // A registry opens libraries for this host only (the doc's
            // sequence, docs/reference/bindings-v1.md §6); `chtypes fetch
            // --platform` sets the fetch layer's own option.
            platform: None,
            bases: self.bases.clone(),
            cache_dir: self.cache_dir.clone(),
            system_dirs: self.system_dirs.clone().unwrap_or(defaults.system_dirs),
            offline: self.offline,
            frozen: self.frozen,
            lock_path: self.lock_path.clone(),
            lock_write: self.lock_write,
            update: self.update,
            allow_unsigned: self.allow_unsigned,
            strict_cache: self.strict_cache,
            trusted_keys: self.trusted_keys.clone(),
            token: self.token.clone(),
            clock: None,
            before_index_rename: None,
        }
    }
}

/// The cache root a fetch, list or `chtypes where` with `options` would use:
/// `options.cache_dir`, else `CHTYPES_CACHE` (each used through its `v2-dev`
/// subroot, `spec/abi-v2/docs.md` rule r5), else
/// `${XDG_CACHE_HOME:-~/.cache}/chtypes/v2-dev`.
///
/// It is the fetch layer's own resolution and the first entry of
/// [`search_dirs`]. It creates nothing and reads no cache.
///
/// # Errors
/// [`Error::SourceIncompatible`] when no directory can be named: no explicit
/// directory, no `CHTYPES_CACHE`, no `XDG_CACHE_HOME` and no `HOME`.
pub fn cache_root(options: &FetchOptions) -> Result<PathBuf> {
    Ok(layout::cache_root(options.cache_dir.as_deref())?)
}

/// Every directory a lookup reads for installed builds, in the order it reads
/// them: the cache root first, then each read-only system directory
/// (`options.system_dirs`; `None` is the built-in list, an empty list none).
/// The order is the fetch layer's own, and on a tie the earlier directory wins.
/// It creates nothing and touches no file.
///
/// # Errors
/// As [`cache_root`].
pub fn search_dirs(options: &FetchOptions) -> Result<Vec<PathBuf>> {
    Ok(ensure::search_dirs(&options.to_options())?)
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

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    // A poisoned mutex only means another thread panicked while holding it;
    // what it guards is still whole.
    m.lock().unwrap_or_else(|e| e.into_inner())
}

struct Opened {
    request: String,
    library: Arc<Library>,
}

/// One attempt to open one request: the installed lookup, the fetch when there
/// is one, and the load. Every open of that request made while it is in
/// progress waits on it and gets its answer.
#[derive(Default)]
struct Flight {
    landing: Mutex<Landing>,
    landed: Condvar,
}

#[derive(Default)]
enum Landing {
    #[default]
    InFlight,
    Landed(Result<Arc<Library>>),
    /// The leading open panicked: every open waiting on the attempt starts over.
    Abandoned,
}

impl Flight {
    fn land(&self, landing: Landing) {
        *lock(&self.landing) = landing;
        self.landed.notify_all();
    }

    /// The attempt's answer once it lands, or `None` if it was abandoned.
    fn wait(&self) -> Option<Result<Arc<Library>>> {
        let landing = self
            .landed
            .wait_while(lock(&self.landing), |l| matches!(l, Landing::InFlight))
            .unwrap_or_else(|e| e.into_inner());
        match &*landing {
            Landing::Landed(outcome) => Some(outcome.clone()),
            Landing::InFlight | Landing::Abandoned => None,
        }
    }
}

/// What the registry's lock guards: the memo and the attempts in progress.
#[derive(Default)]
struct State {
    opened: Vec<Opened>,
    flights: HashMap<String, Arc<Flight>>,
}

impl State {
    fn remembered(&self, request: &str) -> Option<Arc<Library>> {
        self.opened
            .iter()
            .find(|o| o.request == request)
            .map(|o| Arc::clone(&o.library))
    }
}

/// Abandons an attempt whose leading open unwinds, so the opens waiting on it
/// start over instead of waiting forever.
struct AbandonOnUnwind<'a> {
    registry: &'a Registry,
    flight: &'a Flight,
    request: &'a str,
    armed: bool,
}

impl Drop for AbandonOnUnwind<'_> {
    fn drop(&mut self) {
        if self.armed {
            lock(&self.registry.state).flights.remove(self.request);
            self.flight.land(Landing::Abandoned);
        }
    }
}

/// The test hook's type: called with the request each time an open starts
/// waiting on an attempt.
#[cfg(test)]
type OnWait = Box<dyn Fn(&str) + Send + Sync>;

/// A request for a version, to a loaded library. `Send + Sync`.
///
/// An open never waits for another request's fetch, and concurrent opens of
/// one request share one fetch (public issue #491).
pub struct Registry {
    fetch: FetchOptions,
    autofetch: bool,
    /// Never held across a lookup, a fetch or a load.
    state: Mutex<State>,
    /// A test hook: called each time an open starts waiting on an attempt,
    /// its own or another open's.
    #[cfg(test)]
    pub(crate) on_wait: Option<OnWait>,
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
            state: Mutex::new(State::default()),
            #[cfg(test)]
            on_wait: None,
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
        let state = lock(&self.state);
        let mut out: Vec<Arc<Library>> = Vec::new();
        for o in &state.opened {
            if !out.iter().any(|l| Arc::ptr_eq(l, &o.library)) {
                out.push(Arc::clone(&o.library));
            }
        }
        out
    }

    /// The memo's answer, else the answer of the request's attempt, which this
    /// open starts when none is in progress.
    fn open(&self, request: &str, may_fetch: bool) -> Result<Arc<Library>> {
        if let Some(library) = lock(&self.state).remembered(request) {
            return Ok(library);
        }
        // A refused version spelling is the caller's own misuse, refused before
        // anything is attempted, and unlocks nothing.
        VersionRequest::parse(request)?;
        loop {
            let mut lead = false;
            let flight = {
                let mut state = lock(&self.state);
                if let Some(library) = state.remembered(request) {
                    return Ok(library);
                }
                let flight = state.flights.entry(request.to_string()).or_insert_with(|| {
                    lead = true;
                    Arc::new(Flight::default())
                });
                Arc::clone(flight)
            };
            self.waiting(request);
            if lead {
                return self.attempt(&flight, request, may_fetch);
            }
            if let Some(outcome) = flight.wait() {
                return outcome;
            }
        }
    }

    /// Run an attempt on the leading open's thread, with no lock held, then
    /// release every open waiting on it. An attempt that tried a load and
    /// failed unlocks the setup record while no image has completed load step
    /// 7, whatever failed: the resolve, the fetch, the signature or any load
    /// step (bindings-v1.md section 6, rule 4). It is never remembered.
    fn attempt(&self, flight: &Flight, request: &str, may_fetch: bool) -> Result<Arc<Library>> {
        let mut abandon = AbandonOnUnwind {
            registry: self,
            flight,
            request,
            armed: true,
        };
        let began = setup::generation();
        let outcome = self.resolve_and_open(request, may_fetch);
        if outcome.is_err() {
            settle_failed_open(began);
        }
        {
            let mut state = lock(&self.state);
            state.flights.remove(request);
            if let Ok(library) = &outcome {
                state.opened.push(Opened {
                    request: request.to_string(),
                    library: Arc::clone(library),
                });
            }
        }
        abandon.armed = false;
        flight.land(Landing::Landed(outcome.clone()));
        outcome
    }

    /// The test hook (`on_wait`), when one is set.
    #[cfg(test)]
    fn waiting(&self, request: &str) {
        if let Some(on_wait) = &self.on_wait {
            on_wait(request);
        }
    }

    #[cfg(not(test))]
    fn waiting(&self, _request: &str) {}

    /// Resolve a request, fetching when allowed, and open its image: the part
    /// of [`Registry::open`] whose failure settles the setup.
    fn resolve_and_open(&self, request: &str, may_fetch: bool) -> Result<Arc<Library>> {
        let platform = host_platform();
        let resolved = match ensure::resolve_installed(request, &platform, self.fetch.to_options())?
        {
            Some(r) => r,
            None if self.autofetch && may_fetch => {
                ensure::ensure(request, self.fetch.to_options())?
            }
            None => {
                let message = format!(
                    "nothing installed answers {request} for {platform}{}",
                    if self.autofetch {
                        ""
                    } else {
                        " (autofetch is off: set CHTYPES_AUTOFETCH=1 or RegistryOptions::autofetch)"
                    }
                );
                // The fetch layer's own sentences about the cache (the 0.x
                // hint), the same ones its offline fetch adds.
                let notes = ensure::missing_notes(&self.fetch.to_options());
                return Err(Error::ArtifactMissing(ensure::with_notes(&message, &notes)));
            }
        };
        // The adapter: the fetch layer's record to the loader's input. The
        // predicate goes across exactly as returned, never re-encoded.
        let library = open_image(
            &resolved.library_path.clone(),
            Some(&resolved.predicate.clone()),
            Some(resolved),
        )?;
        check_within_request(library.version(), library.path(), request)?;
        Ok(library)
    }
}

/// The load-time assertion (docs/guides/fetch-v1.md section 9; public issue
/// #481): the library opened for `request` must report, in its own
/// `build_info`, a `clickhouse_version` within that request (equal to an exact
/// request, within a line one), whatever the cache answered. Otherwise the
/// open fails as [`Error::ArtifactCorrupt`] with reason
/// `build_info_mismatch:clickhouse_version`, the code section 4 gives a signed
/// version outside the request. A literal (non-numeric) tag names no version
/// and is not checked. The image stays loaded for the requests it does answer.
fn check_within_request(version: &str, path: &Path, request: &str) -> Result<()> {
    let version_request = VersionRequest::parse(request)?;
    if version_request.is_literal() || version_request.matches(version) {
        return Ok(());
    }
    Err(Error::ArtifactCorrupt(Refusal {
        reason: "build_info_mismatch:clickhouse_version".to_string(),
        path: path.to_path_buf(),
        want: Some(request.to_string()),
        got: Some(version.to_string()),
        message: Some(format!(
            "the library opened for ClickHouse {request} reports another version"
        )),
    }))
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
        assert!(m.contains("25.8") && m.contains(&host_platform()), "{m}");
        let _ = std::fs::remove_dir_all(dir);
    }

    /// The registry's own MISSING carries the fetch layer's 0.x hint, the same
    /// sentence the offline fetch gives (public issue #486).
    #[test]
    fn a_missing_request_from_a_zero_x_registry_names_it() {
        // The 0.x upgrade hint is the v1 contract's: a dev SDK reads an
        // explicit cache through its v2-dev subroot, where no 0.x registry sits.
        let _v1 = crate::ocifetch::channel::use_fetch_v1_for_tests();
        let dir = std::env::temp_dir().join(format!(
            "chtypes_registry_zero_x_{}_{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        std::fs::create_dir_all(dir.join("26.1")).unwrap();
        std::fs::write(dir.join("26.1").join("manifest.json"), b"{}").unwrap();
        let r = Registry::new(RegistryOptions {
            fetch: FetchOptions {
                cache_dir: Some(dir.to_string_lossy().into_owned()),
                system_dirs: Some(Vec::new()),
                ..Default::default()
            },
            autofetch: Some(false),
            ..Default::default()
        })
        .unwrap();
        let err = r.for_version("26.1").unwrap_err();
        let Error::ArtifactMissing(m) = &err else {
            panic!("want ArtifactMissing, got {err:?}")
        };
        let hint = format!(
            "{} holds a 0.x registry (26.1/manifest.json); chtypes 1.x uses an OCI layout at",
            dir.display()
        );
        assert!(m.contains(&hint), "{m}");
        assert!(
            !dir.join("oci-layout").exists(),
            "a lookup wrote into the 0.x registry"
        );
        let _ = std::fs::remove_dir_all(dir);
    }

    /// With `strict_cache`, a 0.x registry is `Error::CacheUnusable` naming
    /// the path and the reason, from `for_version` and `installed` alike
    /// (public issue #486).
    #[test]
    fn a_strict_registry_refuses_a_zero_x_registry_as_cache_unusable() {
        // The v1 contract's cache, used whole (see the test above).
        let _v1 = crate::ocifetch::channel::use_fetch_v1_for_tests();
        let dir = std::env::temp_dir().join(format!(
            "chtypes_registry_strict_{}_{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        std::fs::create_dir_all(dir.join("26.1")).unwrap();
        std::fs::write(dir.join("26.1").join("manifest.json"), b"{}").unwrap();
        let r = Registry::new(RegistryOptions {
            fetch: FetchOptions {
                cache_dir: Some(dir.to_string_lossy().into_owned()),
                system_dirs: Some(Vec::new()),
                strict_cache: Some(true),
                ..Default::default()
            },
            autofetch: Some(false),
            ..Default::default()
        })
        .unwrap();
        let err = r.for_version("26.1").unwrap_err();
        let Error::CacheUnusable(f) = &err else {
            panic!("want CacheUnusable, got {err:?}")
        };
        assert_eq!(
            (f.path.as_path(), f.reason.as_str(), f.os_error.as_deref()),
            (dir.as_path(), "layout_0x", None)
        );
        assert_eq!(err.code(), Some("CHTYPES_CACHE_UNUSABLE"));
        assert!(matches!(r.installed(), Err(Error::CacheUnusable(_))));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn a_library_outside_its_request_is_refused_as_corrupt() {
        let path = Path::new("/cache/unpacked/sha256/x/libchtypes.so");
        for ok in ["26.8", "26.8.5", "26.8.5.1", "a-literal-tag"] {
            assert!(check_within_request("26.8.5.1", path, ok).is_ok(), "{ok}");
        }
        for bad in ["26.3", "26.3.4.1", "26.8.5.2", "26.80", "26.8.15"] {
            match check_within_request("26.8.5.1", path, bad) {
                Err(Error::ArtifactCorrupt(r)) => {
                    assert_eq!(r.reason, "build_info_mismatch:clickhouse_version");
                    assert_eq!(r.want.as_deref(), Some(bad));
                    assert_eq!(r.got.as_deref(), Some("26.8.5.1"));
                }
                other => panic!("{bad}: want ArtifactCorrupt, got {other:?}"),
            }
        }
    }
}
