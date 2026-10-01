//! The artifact registry: a directory of one subdirectory per ClickHouse minor
//! line, each holding a `manifest.json` and the shared library it names —
//! plus, since #284, every OTHER installed patch of that line, in a
//! `patches/<minor>/<clickhouse_version>/` sibling (docs/guides/fetch.md, THE
//! LAYOUT RULE). The flat `<minor>/` slot is always what a LINE request
//! loads; `patches/` holds everything else. Both are read; only the flat slot
//! is written by a released (pre-#284) SDK sharing the same cache.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock, RwLock};
use std::time::{Duration, Instant};

use serde::Deserialize;

use crate::error::{Error, Result};
use crate::library::{DEFAULT_TIMEZONE, Library, minor_of, minor_order};

/// Version channels a patch spelling may carry (`docs/guides/fetch.md`
/// Decision 7). Duplicated from `fetch::release`'s own copy: this module has
/// to work without the `fetch` feature, so it cannot depend on that
/// feature-gated module.
const CHANNELS: [&str; 4] = ["lts", "stable", "prestable", "testing"];

/// `25.8.28.1-lts` -> `25.8.28.1`; `25.8.28.1` unchanged.
fn strip_channel(version: &str) -> &str {
    match version.rsplit_once('-') {
        Some((bare, ch)) if CHANNELS.contains(&ch) => bare,
        _ => version,
    }
}

/// Decision 7, the one matching rule for every place a patch is matched: a
/// `requested` spelling that carries a channel matches only that channel;
/// one that carries none matches `version` (which always carries one, once
/// resolved) on any channel. Server-driven spellings (`SELECT version()`)
/// carry no channel, so this is what lets them match an installed or served
/// row at all.
fn patch_matches(requested: &str, version: &str) -> bool {
    match requested.rsplit_once('-') {
        Some((_, ch)) if CHANNELS.contains(&ch) => requested == version,
        _ => strip_channel(version) == requested,
    }
}

/// Numeric components for ordering, channel ignored: `25.10.7.6` >
/// `25.8.28.1`.
fn version_key(version: &str) -> Vec<u64> {
    strip_channel(version)
        .split('.')
        .map(|p| p.parse().unwrap_or(0))
        .collect()
}

/// R1: four-or-more numeric dot components (channel aside) is a PATCH
/// spelling; two or three is a LINE. `spelling` is trimmed first.
fn is_patch_spelling(spelling: &str) -> bool {
    strip_channel(spelling.trim()).split('.').count() >= 4
}

/// The `clickhouse_version` a directory's own `manifest.json` claims, or
/// `None` when there is no readable manifest or it names none. The deciding
/// read for two-level scanning (docs/guides/fetch.md §4): a directory NAME is
/// only ever the last resort.
fn manifest_version(dir: &Path) -> Option<String> {
    let text = std::fs::read_to_string(dir.join("manifest.json")).ok()?;
    let m: Manifest = serde_json::from_str(&text).ok()?;
    (!m.clickhouse_version.is_empty()).then_some(m.clickhouse_version)
}

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
    /// SHA-256 of that file — the only integrity check that means anything.
    ///
    /// **This crate hashes.** It used to say the opposite here — "does not
    /// hash (it takes no crypto dependency)" — and that was a divergence
    /// rather than a design: Python and TypeScript have offered a load-time
    /// check for as long as they have existed, so whether an artifact was
    /// re-hashed before `dlopen` depended on which binding a consumer picked
    /// (issue #13, item A2). Security posture is not an API spelling, so the
    /// dependency was taken: `sha2`, pure Rust, no system library, and
    /// unconditional rather than behind `fetch`. Opt in per registry with
    /// [`RegistryOptions::verify_checksums`]; the release pipeline's own
    /// verification is still what proves the bytes at their source.
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
    /// library is tried FIRST; this field is the fallback when that file is
    /// absent (`Library::load`'s `resolve_unsafe_families`). `None` when the
    /// field itself is absent — distinct from `Some(String::new())`, a
    /// PRESENT, valid empty refuse-list.
    #[serde(default)]
    pub unsafe_families: Option<String>,
}

/// Every artifact under one directory, indexed by version — or, built with
/// [`Registry::from_search_path`], the `docs/guides/fetch.md` §1 search path.
///
/// Each library is loaded with `RTLD_NOW | RTLD_LOCAL`, which is what lets two
/// builds that both define `DB::DataTypeFactory` answer in one process. Cost,
/// measured: roughly 120 MB resident per loaded version.
///
/// **Constructing a registry — with a directory or without one — reads
/// `manifest.json` files and `dlopen`s nothing.** Nothing in this crate opens
/// an artifact except a request for a specific version
/// ([`Registry::for_version`]) or an explicit [`RegistryOptions::preload`].
/// That is true of every constructor: [`Registry::new`], its `from_env*`
/// variants, [`Registry::with_timezone`], [`Registry::open`] and
/// [`Registry::from_search_path`].
///
/// Two shapes, one type, and the difference is only WHERE a line may come
/// from:
///
/// * **One directory** — [`Registry::open`] and everything routed through it.
///   The line is taken from that directory or from nowhere.
/// * **The search path** — [`Registry::from_search_path`]. A line is taken
///   from the first directory on the §1 search path that holds it, and one
///   found nowhere is fetched first when autofetch is on.
///
/// Either way a line no directory holds is [`Error::ArtifactMissing`] (§7),
/// naming every directory looked in and the fetch command.
pub struct Registry {
    /// One directory: itself. Search path: where fetch writes ([`install_dir`]).
    dir: PathBuf,
    /// One directory: `[dir]`. Otherwise the §1 search path, in order.
    search: Vec<PathBuf>,
    /// Minor line -> the FIRST directory on `search` that holds it, as the
    /// construction-time manifest scan found it. What [`Registry::versions`]
    /// answers from, and what makes "no artifact anywhere" and a bad
    /// `preload` entry decidable at construction without a single `dlopen`.
    known: Vec<(String, PathBuf)>,
    /// `docs/guides/fetch.md` §1 item 1, kept for autofetch's `dest`.
    #[cfg_attr(not(feature = "fetch"), allow(dead_code))]
    explicit: Option<PathBuf>,
    timezone: String,
    autofetch: bool,
    /// Re-hash each library against its manifest before `dlopen`
    /// ([`RegistryOptions::verify_checksums`]).
    verify: bool,
    #[cfg(feature = "fetch")]
    fetch: crate::fetch::EnsureOptions,
    loaded: RwLock<Loaded>,
    /// Serializes the open path, so two threads asking for one line load it
    /// once.
    opening: Mutex<()>,
}

/// What a registry has loaded: every library that was opened, in release
/// order, plus which one each LINE is PINNED to (§Version selection R3/R8) —
/// set once per line and never moved while the registry lives. A patch-level
/// open never writes a line's pin; only a line resolution (or, for Go's
/// analog, an explicit `Load`) does.
#[derive(Default)]
struct Loaded {
    libraries: Vec<Arc<Library>>,
    line_pin: HashMap<String, Arc<Library>>,
}

impl Loaded {
    /// Index a freshly loaded library. Idempotent by version: loading the
    /// same exact patch twice (a line resolution and a patch resolution
    /// landing on the same file) keeps the first `Arc`.
    fn insert(&mut self, library: Arc<Library>) {
        if self
            .libraries
            .iter()
            .any(|l| l.version() == library.version())
        {
            return;
        }
        self.libraries.push(library);
        // Release order, not scan order: the directory listing is lexical,
        // which put 25.10 before 25.8 (docs/reference/bindings.md §Version selection,
        // rule 2 — every ordered surface uses numeric release order; fixed
        // 2026-08-26).
        self.libraries.sort_by_key(|l| minor_order(l.minor()));
    }

    /// R3/R8: pin `minor` to `library`, but only the FIRST time — the pin
    /// never moves while the registry lives, whatever resolves the line
    /// afterward.
    fn pin_line(&mut self, minor: &str, library: &Arc<Library>) {
        self.line_pin
            .entry(minor.to_string())
            .or_insert_with(|| Arc::clone(library));
    }

    /// The library a LINE request for `minor` already resolved to, if any.
    fn get_line(&self, minor: &str) -> Option<Arc<Library>> {
        self.line_pin.get(minor).cloned()
    }

    /// The open library matching `requested` under Decision 7
    /// ([`patch_matches`]), if any — R4 step 1. Never a line-based fallback:
    /// a patch request only short-circuits on an EXACT match.
    fn get_matching(&self, requested: &str) -> Option<Arc<Library>> {
        self.libraries
            .iter()
            .find(|l| patch_matches(requested, l.version()))
            .cloned()
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
    /// Re-hash every library this registry loads against its own
    /// `manifest.json` BEFORE `dlopen` — `docs/reference/artifact.md`
    /// §Verification, and the same option Python spells `verify_hashes`,
    /// TypeScript `verifyChecksums` and Go `WithVerifyChecksums`.
    ///
    /// `false` by default, exactly as in the other three: hashing a 232 MB
    /// library is not free, and a locally built artifact has nothing to
    /// prove. Turn it on for anything that did not come from a local build —
    /// the spec says a loader SHOULD verify then — and it is a MUST for bytes
    /// that arrived over a network, where a move that reported success and
    /// truncated the library looks identical to one that worked.
    ///
    /// What it ADDS, per line, is the sha256 of the file being loaded
    /// compared against `library_sha256`: a mismatch fails the open with
    /// [`Error::ChecksumMismatch`] and nothing is mapped. (The cheaper
    /// `library_bytes` check that runs just before it is not conditional on
    /// this option — this crate has always made it.) A manifest carrying NO
    /// `library_sha256` is REFUSED rather
    /// than passed: verification asked for and not possible is not
    /// verification — all four bindings refuse it.
    ///
    /// It is a policy on the REGISTRY, not a property of
    /// [`RegistryOptions::preload`]: **a library's checksum is computed
    /// immediately before that library is `dlopen`ed, and at no other time** —
    /// at construction for the preloaded lines, at first use for the rest,
    /// never for a line nobody asks for.
    ///
    /// **The `library_bytes` size check above runs at that exact same
    /// point, unconditionally** (issue #82, decided alongside Go, Python and
    /// TypeScript, which gained this size check outside of verification in
    /// the same change): at construction for a preloaded line, at first use
    /// for the rest, on every load whether or not this option is on. Before
    /// #50 made loading lazy, that meant this crate's size check ran for
    /// every artifact in a registry directory at CONSTRUCTION, whether or
    /// not a caller ever asked for the line — a stronger, eager signal this
    /// crate no longer gives: it now runs only for a line something actually
    /// loads, the same scope the checksum has always had. The size check
    /// stayed unconditional on verification throughout; what changed under
    /// #50 was its SCOPE (every artifact vs. only the ones loaded), not
    /// whether verification gates it.
    ///
    /// [`Registry::new`], its `from_env*` variants and
    /// [`Registry::with_timezone`] take no options — Rust has no default
    /// arguments — so a caller that wants a verified registry over one
    /// explicit directory has two routes: [`Registry::open`], which takes
    /// this struct directly for that one directory, or passing the directory
    /// here as [`RegistryOptions::dir`] and going through
    /// [`Registry::from_search_path_with`], where that directory is the first
    /// thing on the search path and every line it serves is verified as it
    /// opens.
    pub verify_checksums: bool,
    /// Open these lines AT CONSTRUCTION — the one eager path, and the same
    /// option Go spells `WithPreload(...)`, Python `preload=[...]` and
    /// TypeScript `{preload: [...]}`. Honored by BOTH
    /// [`Registry::from_search_path_with`] and [`Registry::open`].
    ///
    /// Each entry is a version spelling resolved exactly as
    /// [`Registry::for_version`] resolves one: a minor line (`"25.8"`) or an
    /// exact patch (`"25.8.28.1-lts"`), never a path. They are opened in the
    /// order given, before the constructor returns, and an entry no directory
    /// holds is [`Error::ArtifactMissing`] — the same §7 error the first
    /// `for_version` would have returned, returned earlier.
    ///
    /// It NEVER fetches, even with autofetch on: autofetch is a first-use
    /// behavior in all four bindings, and a constructor is a worse place than
    /// a request to begin a 250 MB download. An empty list is exactly the
    /// default.
    ///
    /// Deliberately a list of lines rather than "everything in the directory":
    /// a registry directory is whatever a fetch left behind, and each open
    /// costs about 120 MB resident.
    pub preload: Vec<String>,
    /// How an autofetch fetches — source, tag, lock, trust policy. Its `dest`
    /// is overridden by [`RegistryOptions::dir`], so the fetched line lands
    /// where this registry looks first.
    #[cfg(feature = "fetch")]
    pub fetch: crate::fetch::EnsureOptions,
}

/// What [`Registry::resolve`] hands back: the loaded [`Library`], plus what
/// was requested, what actually loaded, and whether that was exact
/// (`docs/guides/fetch.md` §Version selection). [`Registry::for_version`]
/// runs the same resolution, emits the same warning on a fallback, and hands
/// back only [`Resolution::library`].
#[derive(Debug, Clone)]
pub struct Resolution {
    /// The library that was loaded: a line's newest patch, the exact patch
    /// requested, or — when [`Resolution::exact`] is `false` — the newest
    /// installed or published patch of the requested patch's line.
    pub library: Arc<Library>,
    /// The caller's spelling, trimmed.
    pub requested: String,
    /// The loaded library's own report of itself
    /// ([`Library::version`]) — never the requested spelling.
    pub version: String,
    /// `true` for a line request. For a patch request, `true` only when the
    /// loaded version matches the requested patch under Decision 7
    /// ([`patch_matches`]); `false` means the newest installed/published
    /// patch of the line was used instead, and a warning was written once
    /// for this (requested, actual) pair.
    pub exact: bool,
}

impl Resolution {
    fn exact(library: Arc<Library>, requested: &str) -> Resolution {
        let version = library.version().to_string();
        Resolution {
            library,
            requested: requested.to_string(),
            version,
            exact: true,
        }
    }
}

/// How long a fallen-back patch request goes before its directory is read
/// again (R-c, the lead's amendment 5919007724): recovery still needs no
/// restart, just not a scan on every call.
const FALLBACK_RECHECK: Duration = Duration::from_secs(60);

/// R-c's per-(requested patch, process) cache: the last fallback this
/// process computed for `requested`, and when. Deliberately process-wide
/// rather than per-registry — the design names it "per requested patch per
/// process" — so two registries over the same destination in one process
/// share a recheck window; two registries over DIFFERENT destinations
/// sharing a cache entry is the one imprecision that falls out of that,
/// accepted because a process with two differently-rooted registries asking
/// for the same patch is rare and the cost is at most a 60s-stale fallback.
#[derive(Clone)]
struct FallbackEntry {
    version: String,
    dir: PathBuf,
    checked_at: Instant,
}

fn fallback_cache() -> &'static Mutex<HashMap<String, FallbackEntry>> {
    static CACHE: OnceLock<Mutex<HashMap<String, FallbackEntry>>> = OnceLock::new();
    CACHE.get_or_init(|| Mutex::new(HashMap::new()))
}

fn fallback_cache_get(requested: &str) -> Option<FallbackEntry> {
    fallback_cache()
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .get(requested)
        .filter(|e| e.checked_at.elapsed() < FALLBACK_RECHECK)
        .cloned()
}

fn fallback_cache_put(requested: &str, version: &str, dir: &Path) {
    fallback_cache()
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .insert(
            requested.to_string(),
            FallbackEntry {
                version: version.to_string(),
                dir: dir.to_path_buf(),
                checked_at: Instant::now(),
            },
        );
}

/// §3's warning: once per (requested, actual) pair per process, across every
/// registry. The pair is recorded BEFORE the line is written, so a second
/// call — even one that only re-raises because a caller's own handling of
/// the first warning failed somehow — never warns twice for the same pair.
fn warned_pairs() -> &'static Mutex<HashSet<(String, String)>> {
    static WARNED: OnceLock<Mutex<HashSet<(String, String)>>> = OnceLock::new();
    WARNED.get_or_init(|| Mutex::new(HashSet::new()))
}

/// Write §3's warning for a (requested, actual) pair, exactly once per
/// process. `autofetching` picks the wording: installed-only ("not
/// installed … install it with …") or fetch-aware ("not published … at ABI
/// revision …").
fn warn_fallback_once(requested: &str, actual: &str, autofetching: bool) {
    let pair = (requested.to_string(), actual.to_string());
    if !warned_pairs()
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .insert(pair)
    {
        return;
    }
    let minor = minor_of(requested);
    let body = if autofetching {
        format!(
            "ClickHouse {requested} is not published for {} at ABI revision {}; using {actual}, \
             the newest published patch of {minor}. Behavior can differ between patches.",
            host_platform(),
            crate::ABI_REVISION,
        )
    } else {
        format!(
            "ClickHouse {requested} is not installed for {}; using {actual}, the newest \
             installed patch of {minor}. Behavior can differ between patches. If {requested} is \
             published, install it with: {} {requested}",
            host_platform(),
            crate::FETCH_COMMAND,
        )
    };
    eprintln!("chtypes: WARNING: {body}");
}

/// [`Registry::autofetch_patch`]'s outcome: either the directory to load, or
/// a definite "the release does not publish this patch" that
/// [`Registry::resolve_patch`] reads as its cue to fall back within the line.
#[cfg(feature = "fetch")]
enum PatchFetchOutcome {
    Found(PathBuf),
    Unpublished,
}

impl Registry {
    /// A registry over the `docs/guides/fetch.md` §1 search path for this host, with
    /// the defaults of [`RegistryOptions`]. Nothing is `dlopen`ed until
    /// [`Registry::for_version`] asks for a line; see the type docs.
    ///
    /// # Errors
    ///
    /// As [`Registry::from_search_path_with`]. **This became fallible in
    /// 0.3.0**: the manifest scan it now runs can fail on an unreadable
    /// directory, and "no directory on the path holds an artifact" is a
    /// construction error in the other three bindings. Leaving it infallible
    /// would have made Rust the one binding that discovers an empty machine at
    /// first use.
    pub fn from_search_path() -> Result<Registry> {
        Registry::from_search_path_with(RegistryOptions::default())
    }

    /// [`Registry::from_search_path`] with an explicit directory, timezone,
    /// autofetch setting, verification policy or preload list.
    ///
    /// # Errors
    ///
    /// Construction fails only for what manifests can decide:
    ///
    /// * [`Error::Registry`] — [`RegistryOptions::dir`] was NAMED and does not
    ///   exist or cannot be read (suppressed when autofetch is on, where it is
    ///   the destination the first fetch creates).
    /// * [`Error::EmptyRegistry`] — no directory on the search path holds a
    ///   readable `<minor>/manifest.json` (likewise suppressed by autofetch).
    /// * [`Error::ArtifactMissing`] — a [`RegistryOptions::preload`] entry no
    ///   directory holds.
    /// * Everything [`Registry::for_version`] can return, for a preloaded
    ///   line, because preloading IS opening it.
    pub fn from_search_path_with(opts: RegistryOptions) -> Result<Registry> {
        let autofetch = opts.autofetch.unwrap_or_else(|| {
            std::env::var(AUTOFETCH_ENV)
                .map(|v| v == "1")
                .unwrap_or(false)
        });
        // A directory somebody NAMED and cannot be read is a configuration
        // mistake named now, and it is the typo guard: /var/lib/chtyeps fails
        // here rather than three calls later. It costs a directory listing and
        // no dlopen.
        if let Some(dir) = opts.dir.as_deref() {
            if !autofetch {
                std::fs::read_dir(dir).map_err(|source| Error::Registry {
                    dir: dir.to_path_buf(),
                    source,
                })?;
            }
        }
        let search = registry_search_path(opts.dir.as_deref());
        let known = installed_lines(&search);
        if known.is_empty() && !autofetch {
            return Err(Error::EmptyRegistry {
                looked_in: search.clone(),
            });
        }
        let registry = Registry {
            dir: install_dir(opts.dir.as_deref()),
            search,
            known,
            explicit: opts.dir,
            timezone: opts
                .timezone
                .unwrap_or_else(|| DEFAULT_TIMEZONE.to_string()),
            autofetch,
            verify: opts.verify_checksums,
            #[cfg(feature = "fetch")]
            fetch: opts.fetch,
            loaded: RwLock::new(Loaded::default()),
            opening: Mutex::new(()),
        };
        registry.preload(&opts.preload)?;
        Ok(registry)
    }

    /// A registry over `dir`, with the `UTC` server timezone the spec
    /// requires. Reads the manifests under it and `dlopen`s nothing. See
    /// [`Registry::with_timezone`] for the errors.
    pub fn new(dir: impl AsRef<Path>) -> Result<Registry> {
        Registry::with_timezone(dir, DEFAULT_TIMEZONE)
    }

    /// A registry over `$CHTYPES_REGISTRY`.
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

    /// Open one `preload` entry at a time, before the constructor returns,
    /// without fetching (R7): the same resolution [`Registry::resolve`]
    /// runs, minus any fetch. A preloaded patch that falls back warns right
    /// here, at construction, and a preloaded line sets that line's pin.
    fn preload(&self, lines: &[String]) -> Result<()> {
        for version in lines {
            if version.is_empty() {
                return Err(Error::Registry {
                    dir: self.dir.clone(),
                    source: std::io::Error::new(
                        std::io::ErrorKind::InvalidInput,
                        "RegistryOptions.preload: an empty version does not mean 'pick one'",
                    ),
                });
            }
            self.resolve_internal(version, false)?;
        }
        Ok(())
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
    /// A thin wrapper over [`Registry::open`] with everything but `timezone`
    /// at its [`RegistryOptions`] default (so, no verification) — see its
    /// docs for the scan and load behavior.
    ///
    /// # Errors
    ///
    /// See [`Registry::open`].
    pub fn with_timezone(dir: impl AsRef<Path>, timezone: &str) -> Result<Registry> {
        Registry::open(
            dir,
            RegistryOptions {
                timezone: Some(timezone.to_string()),
                ..RegistryOptions::default()
            },
        )
    }

    /// A registry over one directory, honoring the [`RegistryOptions`] that
    /// mean something for a single directory — [`RegistryOptions::timezone`]
    /// (`UTC` when `None`), [`RegistryOptions::verify_checksums`] and
    /// [`RegistryOptions::preload`] — so a caller no longer has to switch to
    /// [`Registry::from_search_path_with`] to get a verified registry over one
    /// explicit directory (issue #35).
    ///
    /// **What it opens is the REGISTRY, not the artifacts.** It reads the
    /// manifests under `dir` and `dlopen`s nothing; the first
    /// [`Registry::for_version`] opens the line it is asked for, and
    /// `preload` is how to open a named set up front. The name is unchanged
    /// and so is the signature: eagerness was inherited from the constructor
    /// this was factored out of, not the capability #35 asked for, which was
    /// verification without being forced onto the search path.
    ///
    /// The fields that only make sense on the §1 search path —
    /// [`RegistryOptions::dir`], [`RegistryOptions::autofetch`] and, with the
    /// `fetch` feature, [`RegistryOptions::fetch`] — are REFUSED, not
    /// silently ignored, when they are not at their default: `dir` is
    /// already this function's first argument, and neither autofetch nor a
    /// fetch policy means anything without a search path to fall back on.
    /// That refusal happens FIRST, before anything on disk is touched.
    ///
    /// Otherwise this scans `dir` for `<minor>/manifest.json`: a subdirectory
    /// without a readable one is skipped silently (a registry may hold scratch
    /// directories), and a directory holding none at all is
    /// [`Error::EmptyRegistry`]. A directory that HAS a manifest and then
    /// fails to LOAD is broken, not absent — and that is reported by the call
    /// that opens it, which is the preload here or the first `for_version`.
    ///
    /// ```no_run
    /// use chtypes::{Registry, RegistryOptions};
    ///
    /// let registry = Registry::open(
    ///     "/var/lib/chtypes/artifacts",
    ///     RegistryOptions {
    ///         verify_checksums: true,
    ///         ..Default::default()
    ///     },
    /// )?;
    /// # Ok::<(), chtypes::Error>(())
    /// ```
    ///
    /// # Errors
    ///
    /// * [`Error::Registry`] with an `io::ErrorKind::InvalidInput` source —
    ///   `opts.dir`, `opts.autofetch` or (with the `fetch` feature)
    ///   `opts.fetch` was not at its default; the message names the field and
    ///   [`Registry::from_search_path_with`], which does honor it.
    /// * [`Error::Registry`] — the registry directory itself could not be read.
    /// * [`Error::EmptyRegistry`] — the directory held no readable
    ///   `<minor>/manifest.json`; an empty registry is a configuration
    ///   mistake, not an empty result.
    /// * [`Error::ArtifactMissing`] — a [`RegistryOptions::preload`] entry the
    ///   directory does not hold.
    /// * For a PRELOADED line, everything [`Registry::for_version`] can
    ///   return, because preloading is opening: [`Error::LibraryRead`],
    ///   [`Error::CorruptArtifact`], [`Error::ChecksumMismatch`],
    ///   [`Error::VersionMismatch`], [`Error::Registry`] for a manifest with
    ///   no `library_sha256` under verification, and everything
    ///   [`Library::load`] can return. Without a preload list those arrive
    ///   from the first `for_version` instead.
    pub fn open(dir: impl AsRef<Path>, opts: RegistryOptions) -> Result<Registry> {
        let dir = dir.as_ref().to_path_buf();

        // Refuse the search-path-only fields FIRST, before touching disk.
        if opts.dir.is_some() {
            return Err(Error::Registry {
                dir,
                source: std::io::Error::new(
                    std::io::ErrorKind::InvalidInput,
                    "RegistryOptions.dir is not honored by Registry::open (its directory is \
                     the first argument); use Registry::from_search_path_with",
                ),
            });
        }
        if opts.autofetch.is_some() {
            return Err(Error::Registry {
                dir,
                source: std::io::Error::new(
                    std::io::ErrorKind::InvalidInput,
                    "RegistryOptions.autofetch is not honored by Registry::open; use \
                     Registry::from_search_path_with",
                ),
            });
        }
        #[cfg(feature = "fetch")]
        if opts.fetch != crate::fetch::EnsureOptions::default() {
            return Err(Error::Registry {
                dir,
                source: std::io::Error::new(
                    std::io::ErrorKind::InvalidInput,
                    "RegistryOptions.fetch is not honored by Registry::open; use \
                     Registry::from_search_path_with",
                ),
            });
        }

        let timezone = opts
            .timezone
            .unwrap_or_else(|| DEFAULT_TIMEZONE.to_string());
        let verify = opts.verify_checksums;

        // The named directory must be readable — this is the typo guard, and
        // it costs a directory listing and no dlopen.
        std::fs::read_dir(&dir).map_err(|source| Error::Registry {
            dir: dir.clone(),
            source,
        })?;
        let search = vec![dir.clone()];
        let known = installed_lines(&search);
        if known.is_empty() {
            return Err(Error::EmptyRegistry { looked_in: search });
        }

        let registry = Registry {
            search,
            dir,
            known,
            explicit: None,
            timezone,
            autofetch: false,
            verify,
            #[cfg(feature = "fetch")]
            fetch: crate::fetch::EnsureOptions::default(),
            loaded: RwLock::new(Loaded::default()),
            opening: Mutex::new(()),
        };
        registry.preload(&opts.preload)?;
        Ok(registry)
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

    /// Every ClickHouse minor line this registry CAN ANSWER FOR, ordered
    /// numerically — `25.10` is a *later* line than `25.8`, so lexical order
    /// would be wrong. The ones it has OPENED plus the ones its
    /// construction-time manifest scan discovered, each answered from its
    /// first directory.
    ///
    /// That is one meaning in all four bindings, and it is the meaning that
    /// survives lazy loading: "the lines that happen to be open" would read as
    /// an empty registry until the first [`Registry::for_version`]. It opens
    /// nothing, which is what keeps `Debug` from costing 120 MB a line.
    pub fn versions(&self) -> Vec<String> {
        let mut out: Vec<String> = self
            .loaded()
            .libraries
            .iter()
            .map(|l| l.minor().to_string())
            .collect();
        out.extend(self.known.iter().map(|(m, _)| m.clone()));
        out.sort_by_key(|m| minor_order(m));
        out.dedup();
        out
    }

    /// The libraries this registry has OPENED, in release order (oldest minor
    /// line first — `25.10` after `25.8`, whatever the directory listing
    /// said).
    ///
    /// What is open right now, never what could be: a discovered line that no
    /// `for_version` and no [`RegistryOptions::preload`] has opened appears in
    /// [`Registry::versions`] and not here. It opens nothing.
    pub fn libraries(&self) -> Vec<Arc<Library>> {
        self.loaded().libraries.clone()
    }

    fn loaded(&self) -> std::sync::RwLockReadGuard<'_, Loaded> {
        self.loaded.read().unwrap_or_else(|p| p.into_inner())
    }

    /// Resolve a version to its library, **opening it if it is not open
    /// yet**, and return only the [`Library`] — [`Registry::resolve`] minus
    /// the rest of the [`Resolution`]. A minor line (`"25.8"`) or an exact
    /// patch (`"25.8.28.1-lts"`) both work; an exact patch that is not
    /// installed (or published) falls back to the newest patch of its line,
    /// with one `chtypes: WARNING:` line per (requested, actual) pair per
    /// process — see [`Registry::resolve`] for the full rule and its errors.
    ///
    /// This is what opens an artifact; construction does not.
    ///
    /// Resolution never crosses lines: answering 26.7 semantics from a 25.8
    /// artifact would be a lie, and version behavior is not monotonic (25.10
    /// rejects a mixed-type DEFAULT that 25.8 and 26.6 both accept).
    pub fn for_version(&self, version: &str) -> Result<Arc<Library>> {
        self.resolve(version).map(|r| r.library)
    }

    /// Resolve a minor line or an exact patch to a [`Resolution`] — the
    /// loaded [`Library`] plus what was requested, what actually loaded, and
    /// whether it was exact (`docs/guides/fetch.md` §Version selection).
    /// **Opens the artifact if it is not open yet.**
    ///
    /// * **A line** (`"25.8"`): the line's PIN, if this registry already has
    ///   one (never moves while the registry lives); otherwise the newest
    ///   patch of the line in the first directory on this registry's path
    ///   that holds ANY patch of it — counting both that directory's flat
    ///   `<minor>/` slot and its `patches/<minor>/` siblings, a nested
    ///   install winning a tie at the same version — fetched first when
    ///   nothing is found and autofetch is on. `Resolution::exact` is always
    ///   `true`: the request asked for "a patch of this line" and got one.
    /// * **An exact patch** (`"25.8.28.1-lts"`, channel-aware per Decision 7
    ///   — `"25.8.28.1"` matches that patch on ANY channel): an already-open
    ///   match, else that patch anywhere on the WHOLE search path, else —
    ///   with autofetch on — a fetch of that exact patch. Failing all three,
    ///   the newest installed (or, with autofetch on, published) patch of the
    ///   SAME line, with `Resolution::exact = false` and a warning; this
    ///   fallback re-checks its directory at most once every 60 seconds per
    ///   requested patch per process rather than on every call (R-c).
    ///
    /// Never falls back to another line — that refusal is unchanged.
    ///
    /// # Errors
    ///
    /// * [`Error::ArtifactMissing`] — no directory holds a patch of the line
    ///   at all; the message names every directory looked in and the fetch
    ///   command. With autofetch on, a line is fetched first (once per
    ///   process per line, under one process-wide lock, so concurrent opens
    ///   fetch once) and the fetch's own error surfaces when that fails.
    /// * [`Error::LibraryRead`], [`Error::CorruptArtifact`],
    ///   [`Error::ChecksumMismatch`], [`Error::VersionMismatch`],
    ///   [`Error::Registry`], and everything [`Library::load`] can return —
    ///   a directory holds the version and the artifact is bad.
    pub fn resolve(&self, version: &str) -> Result<Resolution> {
        self.resolve_internal(version, true)
    }

    /// [`Registry::resolve`], minus any fetch when `allow_fetch` is `false`
    /// (the preload path, R7) — otherwise identical, including the warning.
    fn resolve_internal(&self, version: &str, allow_fetch: bool) -> Result<Resolution> {
        let requested = version.trim();
        if is_patch_spelling(requested) {
            self.resolve_patch(requested, allow_fetch)
        } else {
            self.resolve_line(requested, allow_fetch)
        }
    }

    /// R3: a line request.
    fn resolve_line(&self, requested: &str, allow_fetch: bool) -> Result<Resolution> {
        let minor = minor_of(requested);
        if let Some(l) = self.loaded().get_line(&minor) {
            return Ok(Resolution::exact(l, requested));
        }
        let _opening = self.opening.lock().unwrap_or_else(|p| p.into_inner());
        // Another thread may have pinned it while this one waited.
        if let Some(l) = self.loaded().get_line(&minor) {
            return Ok(Resolution::exact(l, requested));
        }
        let dir = match locate_in(&self.search, &minor) {
            Some(dir) => dir,
            None => {
                if !allow_fetch {
                    return Err(self.missing(&minor));
                }
                self.autofetch_or_missing(&minor)?
            }
        };
        let library = self.load_into(&dir)?;
        self.pin_line(&minor, &library);
        Ok(Resolution::exact(library, requested))
    }

    /// R4: an exact-patch request.
    fn resolve_patch(&self, requested: &str, allow_fetch: bool) -> Result<Resolution> {
        // Step 1: already open, matching under Decision 7 — never a
        // line-based shortcut. Pure in-memory, so this always runs, cache
        // window or not.
        if let Some(l) = self.loaded().get_matching(requested) {
            return Ok(Resolution::exact(l, requested));
        }
        let minor = minor_of(requested);
        let _opening = self.opening.lock().unwrap_or_else(|p| p.into_inner());
        if let Some(l) = self.loaded().get_matching(requested) {
            return Ok(Resolution::exact(l, requested));
        }

        // R-c (the lead's amendment, sharpened): within
        // [`FALLBACK_RECHECK`] of the last fallback computed for this exact
        // requested spelling, the WHOLE re-check is skipped — not just the
        // final fallback pick, but also the exact-match retry (step 2's
        // search-path scan) and a fresh autofetch attempt (step 3) — and the
        // cached fallback is reused directly. Recovery still needs no
        // restart, once the window elapses.
        if let Some(cached) = fallback_cache_get(requested) {
            if let Some(resolution) =
                self.resolve_from_fallback_cache(requested, &cached, allow_fetch)?
            {
                return Ok(resolution);
            }
            // The cached directory is gone (removed, replaced, demoted
            // elsewhere) — fall through and recompute for real, below.
        }

        // Step 2: that patch anywhere on the WHOLE search path — the more
        // specific request wins over the first-directory-only rule a line
        // uses.
        if let Some(dir) = locate_patch_in(&self.search, requested) {
            let library = self.load_into(&dir)?;
            return Ok(Resolution::exact(library, requested));
        }
        // Step 3: autofetch that exact patch, unless this (destination,
        // patch) already came back unpublished in this process.
        if allow_fetch && self.autofetch {
            #[cfg(feature = "fetch")]
            {
                match self.autofetch_patch(requested)? {
                    PatchFetchOutcome::Found(dir) => {
                        let library = self.load_into(&dir)?;
                        return Ok(Resolution::exact(library, requested));
                    }
                    PatchFetchOutcome::Unpublished => {
                        // Falls through to step 4, below.
                    }
                }
            }
            #[cfg(not(feature = "fetch"))]
            {
                return Err(Error::Fetch {
                    message: format!(
                        "autofetch was requested for ClickHouse {requested} but this build of \
                         chtypes has the `fetch` feature disabled"
                    ),
                });
            }
        }
        // Step 4: the fallback within the line — computed fresh, and cached
        // (R-c) so the NEXT call, within the window, skips straight back to
        // `resolve_from_fallback_cache` above.
        self.fallback_within_line(requested, &minor, allow_fetch)
    }

    /// The R-c cache hit path: reuse a fallback computed within the last
    /// [`FALLBACK_RECHECK`] for `requested`, with no directory read and no
    /// network access. `Ok(None)` means the cached directory is gone and the
    /// caller must recompute for real.
    fn resolve_from_fallback_cache(
        &self,
        requested: &str,
        cached: &FallbackEntry,
        allow_fetch: bool,
    ) -> Result<Option<Resolution>> {
        let autofetching = allow_fetch && self.autofetch;
        if let Some(l) = self.loaded().get_matching(&cached.version) {
            warn_fallback_once(requested, &cached.version, autofetching);
            return Ok(Some(Resolution {
                library: l,
                requested: requested.to_string(),
                version: cached.version.clone(),
                exact: false,
            }));
        }
        if cached.dir.join("manifest.json").is_file() {
            warn_fallback_once(requested, &cached.version, autofetching);
            let library = self.load_into(&cached.dir)?;
            return Ok(Some(Resolution {
                library,
                requested: requested.to_string(),
                version: cached.version.clone(),
                exact: false,
            }));
        }
        Ok(None)
    }

    /// R4 step 4: the newest installed (or, with autofetch, published) patch
    /// of `minor`, warned once per (requested, actual) pair, and cached
    /// (R-c) so the next call within [`FALLBACK_RECHECK`] skips the WHOLE
    /// re-check via [`Registry::resolve_from_fallback_cache`] instead of a
    /// directory read (or a fresh autofetch attempt) on every call.
    fn fallback_within_line(
        &self,
        requested: &str,
        minor: &str,
        allow_fetch: bool,
    ) -> Result<Resolution> {
        let autofetching = allow_fetch && self.autofetch;
        let dir = if autofetching {
            #[cfg(feature = "fetch")]
            {
                self.autofetch_line(minor)?
            }
            #[cfg(not(feature = "fetch"))]
            {
                return Err(Error::Fetch {
                    message: format!(
                        "autofetch was requested for ClickHouse {minor} but this build of \
                         chtypes has the `fetch` feature disabled"
                    ),
                });
            }
        } else {
            locate_in(&self.search, minor).ok_or_else(|| self.missing(minor))?
        };
        // The version is read from the manifest BEFORE `dlopen`, so the
        // warning fires even when the artifact then fails to load — a
        // caller's "why is this warning here with no successful resolve"
        // is answered by the Error::Load right after it, not by silence.
        let version = manifest_version(&dir).unwrap_or_else(|| minor.to_string());
        fallback_cache_put(requested, &version, &dir);
        warn_fallback_once(requested, &version, autofetching);
        let library = self.load_into(&dir)?;
        // The library's OWN report is authoritative for the Resolution —
        // the manifest read above is only for the warning and the cache key.
        let version = library.version().to_string();
        Ok(Resolution {
            library,
            requested: requested.to_string(),
            version,
            exact: false,
        })
    }

    /// R3/R8: pin `minor` to `library` — only the first time.
    fn pin_line(&self, minor: &str, library: &Arc<Library>) {
        self.loaded
            .write()
            .unwrap_or_else(|p| p.into_inner())
            .pin_line(minor, library);
    }

    /// `dlopen` the one artifact in `dir`, cross-check it, and index it.
    fn load_into(&self, dir: &Path) -> Result<Arc<Library>> {
        let library = load_artifact_dir(dir, &self.timezone, self.verify)?.ok_or_else(|| {
            Error::Registry {
                dir: dir.to_path_buf(),
                source: std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    "manifest.json is present but names no library",
                ),
            }
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

    /// `docs/guides/fetch.md` §6: one process-wide lock, and — since #284's
    /// fix — one REMEMBERED outcome per (destination, line): a success (found
    /// on the next `locate_in`, so nothing further to remember here) or
    /// [`crate::CODE_ARTIFACT_UNPUBLISHED`] is remembered for the rest of the
    /// process; any other failure (untrusted, corrupt, pinned, unreachable)
    /// is NOT remembered, so the next call retries it. Before this fix every
    /// outcome — including a transient network failure — was remembered
    /// forever, so a process that raced a flaky fetch stayed broken until
    /// restarted.
    #[cfg(feature = "fetch")]
    fn autofetch_line(&self, minor: &str) -> Result<PathBuf> {
        static UNPUBLISHED: Mutex<std::collections::BTreeSet<String>> =
            Mutex::new(std::collections::BTreeSet::new());
        // Whoever held the opening lock before may have installed it.
        if let Some(dir) = locate_in(&self.search, minor) {
            return Ok(dir);
        }
        let key = self.autofetch_key(minor);
        if UNPUBLISHED
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .contains(&key)
        {
            return Err(self.missing(minor));
        }
        let opts = crate::fetch::EnsureOptions {
            dest: self.explicit.clone(),
            ..self.fetch.clone()
        };
        match crate::fetch::ensure(minor, &opts) {
            Ok(installed) => Ok(installed.dir),
            Err(e) => {
                if e.artifact_code() == Some(crate::CODE_ARTIFACT_UNPUBLISHED) {
                    UNPUBLISHED
                        .lock()
                        .unwrap_or_else(|p| p.into_inner())
                        .insert(key);
                }
                Err(e)
            }
        }
    }

    /// [`Registry::resolve_patch`] step 3: the same process-wide memo as
    /// [`Registry::autofetch_line`], but for one exact patch. `Ok(None)`
    /// means the patch is not published (remembered for the rest of the
    /// process, per (destination, patch)); any other failure surfaces as-is
    /// and is never remembered, so it is retried on the next call.
    #[cfg(feature = "fetch")]
    fn autofetch_patch(&self, requested: &str) -> Result<PatchFetchOutcome> {
        static UNPUBLISHED: Mutex<std::collections::BTreeSet<String>> =
            Mutex::new(std::collections::BTreeSet::new());
        if let Some(dir) = locate_patch_in(&self.search, requested) {
            return Ok(PatchFetchOutcome::Found(dir));
        }
        let key = self.autofetch_key(requested);
        if UNPUBLISHED
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .contains(&key)
        {
            return Ok(PatchFetchOutcome::Unpublished);
        }
        let opts = crate::fetch::EnsureOptions {
            dest: self.explicit.clone(),
            ..self.fetch.clone()
        };
        match crate::fetch::ensure(requested, &opts) {
            Ok(installed) => Ok(PatchFetchOutcome::Found(installed.dir)),
            Err(e) => {
                if e.artifact_code() == Some(crate::CODE_ARTIFACT_UNPUBLISHED) {
                    UNPUBLISHED
                        .lock()
                        .unwrap_or_else(|p| p.into_inner())
                        .insert(key);
                    Ok(PatchFetchOutcome::Unpublished)
                } else {
                    Err(e)
                }
            }
        }
    }

    /// The autofetch memo key: this registry's write destination plus the
    /// spelling, so two registries writing to different directories never
    /// share a "remembered unpublished" verdict.
    #[cfg(feature = "fetch")]
    fn autofetch_key(&self, spelling: &str) -> String {
        format!("{:?}|{spelling}", self.explicit)
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
fn load_artifact_dir(sub: &Path, timezone: &str, verify: bool) -> Result<Option<Arc<Library>>> {
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
            .map_err(|source| Error::LibraryRead {
                path: path.clone(),
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

    // The hash decides BEFORE dlopen: an image cannot be unmapped once it is
    // mapped, so refusing has to happen while refusing is still possible. The
    // file hashed is the one about to be loaded — a legacy-name symlink
    // resolves to the same bytes and still passes, while any other file in
    // the directory would leave the loaded bytes unchecked.
    if verify {
        if manifest.library_sha256.is_empty() {
            return Err(Error::Registry {
                dir: sub.to_path_buf(),
                source: std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    "verification was asked for and manifest.json carries no library_sha256",
                ),
            });
        }
        let actual = crate::digest::sha256_file(&path).map_err(|source| Error::LibraryRead {
            path: path.clone(),
            source,
        })?;
        if actual != manifest.library_sha256.to_ascii_lowercase() {
            return Err(Error::ChecksumMismatch {
                path,
                expected: manifest.library_sha256,
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
/// `${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/abi<R>/<platform>`, R this
/// crate's [`crate::ABI_REVISION`] (`docs/guides/fetch.md` §1 item 3). Where fetch
/// installs; keyed by revision so two SDK versions at different revisions never
/// share it.
pub fn cache_dir_for(platform: &str) -> PathBuf {
    let base = std::env::var_os("XDG_CACHE_HOME")
        .map(PathBuf::from)
        .filter(|p| !p.as_os_str().is_empty())
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".cache")))
        .unwrap_or_else(|| PathBuf::from(".cache"));
    base.join("chtypes")
        .join("artifacts")
        .join(format!("abi{}", crate::error::ABI_REVISION))
        .join(platform)
}

/// The per-user artifact cache for this host —
/// `${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/abi<R>/<os>-<arch>`, R this
/// crate's [`crate::ABI_REVISION`] and `<arch>` spelled the artifact way
/// (`amd64`, `arm64`). Where `scripts/fetch.sh` and [`crate::ensure`] install,
/// and what every SDK's tests and playgrounds fall back to when
/// `$CHTYPES_REGISTRY` is unset — one directory the four SDKs at the same
/// revision agree on. A path, not a promise: [`Registry::new`] still errors if
/// nothing is there.
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

/// The directory a LINE request for `line` would load on this host's search
/// path: the newest patch of it in the FIRST directory that holds any (R3) —
/// its flat `<minor>/` slot, or a `patches/<minor>/<version>/` sibling,
/// whichever is newer (a nested install winning a tie at the same version).
/// `None` when no directory holds any patch of the line. `line` may be a
/// minor line or an exact patch; the minor is what is looked for, as
/// [`Registry::resolve`] resolves a line request.
pub fn locate(line: &str, explicit: Option<&Path>) -> Option<PathBuf> {
    locate_in(&registry_search_path(explicit), line)
}

/// [`locate`] over an explicit list of registry directories, in order.
/// Returns a PATCH directory — the flat slot, or a nested one — never
/// necessarily `<dir>/<minor>` itself.
pub fn locate_in(dirs: &[PathBuf], line: &str) -> Option<PathBuf> {
    let minor = minor_of(line);
    dirs.iter().find_map(|d| newest_patch_in(d, &minor))
}

/// The newest patch of `minor` under ONE registry directory `dir`, counting
/// both its flat `<minor>/` slot and its `patches/<minor>/<version>/`
/// children — `None` when neither holds one. A nested install wins a tie at
/// the same version (the flat slot is scanned first, so a nested candidate
/// only has to beat-or-equal it).
fn newest_patch_in(dir: &Path, minor: &str) -> Option<PathBuf> {
    let mut best: Option<(Vec<u64>, PathBuf, bool)> = None;
    let flat = dir.join(minor);
    if let Some(v) = manifest_version(&flat) {
        best = Some((version_key(&v), flat, false));
    } else if flat.join("manifest.json").is_file() {
        // A manifest with no clickhouse_version recorded: still counts, keyed
        // last (empty key sorts lowest), so a nested patch with a real
        // version always outranks it.
        best = Some((Vec::new(), flat, false));
    }
    let patches_dir = dir.join("patches").join(minor);
    if let Ok(entries) = std::fs::read_dir(&patches_dir) {
        for entry in entries.filter_map(|e| e.ok()) {
            let sub = entry.path();
            if !sub.join("manifest.json").is_file() {
                continue;
            }
            let Some(name) = sub.file_name().and_then(|n| n.to_str()) else {
                continue;
            };
            if name.starts_with('.') {
                continue;
            }
            let version = manifest_version(&sub).unwrap_or_else(|| name.to_string());
            let key = version_key(&version);
            let better = match &best {
                None => true,
                Some((bk, _, nested)) => key > *bk || (key == *bk && !*nested),
            };
            if better {
                best = Some((key, sub, true));
            }
        }
    }
    best.map(|(_, path, _)| path)
}

/// The directory holding `exact` (a specific patch, matched with
/// [`patch_matches`]), anywhere on `dirs` — the more specific request R4
/// extends from "the first directory holding the line" to "anywhere on the
/// whole search path". Each directory's flat slot is checked before its
/// `patches/<minor>/` children, in lexical order of their names (the
/// directory names are the patch versions, so this is not a claim about
/// which one is newest — [`locate_in`] answers that question instead).
pub(crate) fn locate_patch_in(dirs: &[PathBuf], exact: &str) -> Option<PathBuf> {
    let minor = minor_of(exact);
    for dir in dirs {
        let flat = dir.join(&minor);
        if let Some(v) = manifest_version(&flat) {
            if patch_matches(exact, &v) {
                return Some(flat);
            }
        }
        let patches_dir = dir.join("patches").join(&minor);
        let Ok(entries) = std::fs::read_dir(&patches_dir) else {
            continue;
        };
        let mut names: Vec<String> = entries
            .filter_map(|e| e.ok())
            .filter_map(|e| e.file_name().into_string().ok())
            .filter(|n| !n.starts_with('.'))
            .collect();
        names.sort();
        for name in names {
            let sub = patches_dir.join(&name);
            if !sub.join("manifest.json").is_file() {
                continue;
            }
            let version = manifest_version(&sub).unwrap_or_else(|| name.clone());
            if patch_matches(exact, &version) {
                return Some(sub);
            }
        }
    }
    None
}

/// Every minor line installed somewhere on `dirs` — counting a line present
/// ONLY as `patches/<minor>/…` siblings, with no flat slot, as installed too
/// (§4: an "is anything installed?" probe must look at both levels) — each
/// paired with the directory [`locate_in`] would hand a LINE request (a
/// PATCH directory, not necessarily `<dir>/<minor>` itself), in numeric
/// release order.
///
/// Superseded, as the fetch-level "what is installed" listing, by
/// [`installed_patches`] (`fetch.list-installed`): this answers "what would a
/// LINE request load, per line", which [`installed_patches`] does not.
pub fn installed_lines(dirs: &[PathBuf]) -> Vec<(String, PathBuf)> {
    let mut minors: Vec<String> = Vec::new();
    for dir in dirs {
        if let Ok(entries) = std::fs::read_dir(dir) {
            for entry in entries.filter_map(|e| e.ok()) {
                let sub = entry.path();
                let Some(name) = sub.file_name().and_then(|n| n.to_str()) else {
                    continue;
                };
                if name.starts_with('.') || name == "patches" {
                    continue;
                }
                if sub.join("manifest.json").is_file() && !minors.iter().any(|m| m == name) {
                    minors.push(name.to_string());
                }
            }
        }
        let patches_root = dir.join("patches");
        if let Ok(entries) = std::fs::read_dir(&patches_root) {
            for entry in entries.filter_map(|e| e.ok()) {
                let sub = entry.path();
                let Some(name) = sub.file_name().and_then(|n| n.to_str()) else {
                    continue;
                };
                if name.starts_with('.') || minors.iter().any(|m| m == name) {
                    continue;
                }
                let has_patch = std::fs::read_dir(&sub)
                    .map(|it| {
                        it.filter_map(|e| e.ok())
                            .any(|e| e.path().join("manifest.json").is_file())
                    })
                    .unwrap_or(false);
                if has_patch {
                    minors.push(name.to_string());
                }
            }
        }
    }
    let mut out: Vec<(String, PathBuf)> = minors
        .into_iter()
        .filter_map(|m| locate_in(dirs, &m).map(|d| (m, d)))
        .collect();
    out.sort_by_key(|(m, _)| minor_order(m));
    out
}

/// One installed patch — the flat line slot, or a `patches/<minor>/<version>`
/// sibling — as `docs/guides/fetch.md` §6 `list` and §5 `verify` report it.
#[derive(Debug, Clone)]
pub struct InstalledPatch {
    /// The minor line.
    pub line: String,
    /// The exact ClickHouse version, from the manifest, or (when a manifest
    /// records none) the directory name — the same last-resort rule §4 uses
    /// everywhere else.
    pub version: String,
    /// The patch's own directory: `<dir>/<minor>` for the flat slot, or
    /// `<dir>/patches/<minor>/<version>` otherwise.
    pub dir: PathBuf,
    /// `true` for the flat slot — what a LINE request would load.
    pub flat: bool,
}

/// Every installed patch on `dirs`, flat slots and `patches/` siblings alike
/// — what `fetch.list-installed` means since #284, superseding the per-line
/// [`installed_lines`]. Directories are walked in the order given; a patch
/// present under more than one is listed once per directory (a registry
/// directory and a foreign-platform cache are not expected to overlap).
pub fn installed_patches(dirs: &[PathBuf]) -> Vec<InstalledPatch> {
    let mut out = Vec::new();
    for dir in dirs {
        if let Ok(entries) = std::fs::read_dir(dir) {
            for entry in entries.filter_map(|e| e.ok()) {
                let sub = entry.path();
                let Some(name) = sub.file_name().and_then(|n| n.to_str()) else {
                    continue;
                };
                if name.starts_with('.') || name == "patches" {
                    continue;
                }
                if !sub.join("manifest.json").is_file() {
                    continue;
                }
                let version = manifest_version(&sub).unwrap_or_default();
                out.push(InstalledPatch {
                    line: name.to_string(),
                    version,
                    dir: sub,
                    flat: true,
                });
            }
        }
        let patches_root = dir.join("patches");
        let Ok(lines) = std::fs::read_dir(&patches_root) else {
            continue;
        };
        for line_entry in lines.filter_map(|e| e.ok()) {
            let line_dir = line_entry.path();
            let Some(minor) = line_dir.file_name().and_then(|n| n.to_str()) else {
                continue;
            };
            if minor.starts_with('.') {
                continue;
            }
            let Ok(patches) = std::fs::read_dir(&line_dir) else {
                continue;
            };
            for patch_entry in patches.filter_map(|e| e.ok()) {
                let sub = patch_entry.path();
                let Some(version_name) = sub.file_name().and_then(|n| n.to_str()) else {
                    continue;
                };
                if version_name.starts_with('.') {
                    continue;
                }
                if !sub.join("manifest.json").is_file() {
                    continue;
                }
                let version = manifest_version(&sub).unwrap_or_else(|| version_name.to_string());
                out.push(InstalledPatch {
                    line: minor.to_string(),
                    version,
                    dir: sub,
                    flat: false,
                });
            }
        }
    }
    out.sort_by(|a, b| {
        minor_order(&a.line)
            .cmp(&minor_order(&b.line))
            .then_with(|| version_key(&a.version).cmp(&version_key(&b.version)))
    });
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The per-user cache is keyed by the crate's own ABI revision — read off
    /// [`crate::ABI_REVISION`], never typed — and the fetch test override does
    /// not move it. Suffix checks, so no environment is touched.
    #[test]
    fn the_per_user_cache_is_keyed_by_the_abi_revision() {
        let abi = format!("abi{}", crate::ABI_REVISION);
        let tail = |platform: &str| {
            Path::new("chtypes")
                .join("artifacts")
                .join(&abi)
                .join(platform)
        };
        let foreign = cache_dir_for("linux-amd64");
        assert!(
            foreign.ends_with(tail("linux-amd64")),
            "{}",
            foreign.display()
        );
        let host = default_registry_dir();
        assert!(
            host.ends_with(tail(host_platform().as_str())),
            "{}",
            host.display()
        );
        #[cfg(feature = "fetch")]
        {
            let previous =
                crate::fetch::__set_fetch_abi_revision_for_tests(Some(crate::ABI_REVISION + 1));
            let moved = default_registry_dir();
            crate::fetch::__set_fetch_abi_revision_for_tests(previous);
            assert_eq!(moved, host, "the fetch override moved the registry");
        }
    }

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

    /// A registry directory holding one line whose "library" is plain text:
    /// enough for the loader to reach `dlopen`, never enough to survive it.
    /// The verdict every verification test below reads is WHICH error came
    /// back — a checksum error means the hash decided first, a load error
    /// means it passed and the open went on.
    fn fake_artifact(tag: &str, sha256: Option<&str>) -> PathBuf {
        let body = b"not a shared library";
        let dir = std::env::temp_dir().join(format!(
            "chtypes-rs-verify-{}-{tag}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let sub = dir.join("25.8");
        std::fs::create_dir_all(&sub).unwrap();
        std::fs::write(sub.join("libchtypes.so"), body).unwrap();
        let hash = match sha256 {
            Some(s) => format!(r#","library_sha256":"{s}""#),
            None => String::new(),
        };
        std::fs::write(
            sub.join("manifest.json"),
            format!(
                r#"{{"library":"libchtypes.so","library_bytes":{}{hash},
                    "clickhouse_version":"25.8.1.1","clickhouse_minor":"25.8"}}"#,
                body.len()
            ),
        )
        .unwrap();
        dir
    }

    fn open_verified(dir: &Path) -> Error {
        let reg = Registry::from_search_path_with(RegistryOptions {
            dir: Some(dir.to_path_buf()),
            verify_checksums: true,
            ..RegistryOptions::default()
        })
        .expect("the directory holds a manifest, so construction must succeed");
        reg.for_version("25.8")
            .expect_err("a text file cannot dlopen; the open must fail")
    }

    #[test]
    fn verify_checksums_refuses_bytes_the_manifest_does_not_claim() {
        let dir = fake_artifact("mismatch", Some(&"00".repeat(32)));
        let err = open_verified(&dir);
        assert!(
            matches!(err, Error::ChecksumMismatch { .. }),
            "want the checksum refusal BEFORE dlopen, got {err:?}"
        );

        // The same directory with the option off: the open reaches dlopen,
        // which is what proves the OPTION — not merely the broken file —
        // produced the verdict above.
        let reg = Registry::from_search_path_with(RegistryOptions {
            dir: Some(dir.clone()),
            ..RegistryOptions::default()
        })
        .expect("the directory holds a manifest, so construction must succeed");
        let err = reg.for_version("25.8").unwrap_err();
        assert!(
            !matches!(err, Error::ChecksumMismatch { .. }),
            "without verify_checksums the hash must not be consulted; got {err:?}"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    /// Issue #82's parity claim, made explicit: the manifest's `library_bytes`
    /// size check is UNCONDITIONAL, unlike the hash the test above exercises.
    /// `fake_artifact` always writes `library_bytes` matching the real body, so
    /// this hand-writes a manifest with a WRONG size instead, and asserts the
    /// load is refused with no `verify_checksums` anywhere in sight — this
    /// crate has made this check unconditionally since before #50's lazy
    /// loading moved it from construction to first use, and this pins the
    /// mismatch case specifically (the existing
    /// `a_missing_library_file_is_library_read_at_the_size_check` only proves
    /// the check runs at all, via an absent file).
    #[test]
    fn library_bytes_mismatch_refuses_without_verify_checksums() {
        let body = b"not a shared library";
        let dir = std::env::temp_dir().join(format!(
            "chtypes-rs-bytes-unconditional-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let sub = dir.join("25.8");
        std::fs::create_dir_all(&sub).unwrap();
        std::fs::write(sub.join("libchtypes.so"), body).unwrap();
        // Wrong size, and deliberately NO library_sha256: if the load reached
        // the hash check at all, this manifest could not satisfy it either,
        // so refusing the size check name specifically proves it ran FIRST,
        // unconditionally.
        std::fs::write(
            sub.join("manifest.json"),
            format!(
                r#"{{"library":"libchtypes.so","library_bytes":{},
                    "clickhouse_version":"25.8.1.1","clickhouse_minor":"25.8"}}"#,
                body.len() + 1
            ),
        )
        .unwrap();

        let reg = Registry::from_search_path_with(RegistryOptions {
            dir: Some(dir.clone()),
            ..RegistryOptions::default()
        })
        .expect("the directory holds a manifest, so construction must succeed");
        let err = reg
            .for_version("25.8")
            .expect_err("a size mismatch must refuse even with verify_checksums off");
        match &err {
            Error::CorruptArtifact {
                expected, actual, ..
            } => {
                assert_eq!(*expected, body.len() as u64 + 1);
                assert_eq!(*actual, body.len() as u64);
            }
            other => panic!("want Error::CorruptArtifact naming both sizes, got {other:?}"),
        }
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn verify_checksums_accepts_matching_bytes_and_loads_on() {
        // sha256 of "not a shared library", the body fake_artifact writes.
        let dir = fake_artifact(
            "match",
            Some(&crate::digest::sha256_hex(b"not a shared library")),
        );
        let err = open_verified(&dir);
        assert!(
            matches!(err, Error::Load { .. } | Error::NotAnArtifact { .. }),
            "matching bytes must pass verification and fail at dlopen, got {err:?}"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn verify_checksums_refuses_a_manifest_with_no_hash() {
        // Verification asked for and not possible is not verification.
        let dir = fake_artifact("nohash", None);
        let err = open_verified(&dir);
        let text = err.to_string();
        assert!(
            matches!(err, Error::Registry { .. }) && text.contains("library_sha256"),
            "want a refusal naming the unverifiable artifact, got {err:?}"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn an_empty_registry_is_an_error_not_an_empty_result() {
        let dir = std::env::temp_dir().join(format!("chtypes-rs-empty-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let err = Registry::new(&dir).unwrap_err();
        assert!(matches!(err, Error::EmptyRegistry { .. }), "got {err:?}");
        std::fs::remove_dir_all(&dir).ok();
    }

    /// A manifest naming a library file that is never written: the size check
    /// and the verification hash both read that FILE, and a registry-directory
    /// error (`Error::Registry`) would name the wrong path entirely.
    fn missing_library_artifact(tag: &str, library_bytes: u64, sha256: Option<&str>) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "chtypes-rs-missing-lib-{}-{tag}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let sub = dir.join("25.8");
        std::fs::create_dir_all(&sub).unwrap();
        let hash = match sha256 {
            Some(s) => format!(r#","library_sha256":"{s}""#),
            None => String::new(),
        };
        std::fs::write(
            sub.join("manifest.json"),
            format!(
                r#"{{"library":"libchtypes.so","library_bytes":{library_bytes}{hash},
                    "clickhouse_version":"25.8.1.1","clickhouse_minor":"25.8"}}"#
            ),
        )
        .unwrap();
        dir
    }

    #[test]
    fn a_missing_library_file_is_library_read_at_the_size_check() {
        // library_bytes > 0 always runs the size check (RegistryOptions::verify_checksums
        // docs), before dlopen and before verification — the library file does
        // not exist on disk at all here, so `std::fs::metadata` fails first.
        let dir = missing_library_artifact("size", 232226512, None);
        let reg = Registry::from_search_path_with(RegistryOptions {
            dir: Some(dir.clone()),
            ..RegistryOptions::default()
        })
        .expect("the directory holds a manifest, so construction must succeed");
        let err = reg
            .for_version("25.8")
            .expect_err("the manifested library file does not exist");
        match &err {
            Error::LibraryRead { path, .. } => {
                assert_eq!(*path, dir.join("25.8").join("libchtypes.so"));
            }
            other => panic!("want Error::LibraryRead naming the missing file, got {other:?}"),
        }
        assert!(
            err.to_string().starts_with("chtypes: cannot read library "),
            "got {err}"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn a_missing_library_file_is_library_read_at_the_verification_hash() {
        // library_bytes: 0 skips the size check; verify_checksums: true reaches
        // the sha256 read instead, and the library file still does not exist.
        let dir = missing_library_artifact("hash", 0, Some(&"00".repeat(32)));
        let err = open_verified(&dir);
        match &err {
            Error::LibraryRead { path, .. } => {
                assert_eq!(*path, dir.join("25.8").join("libchtypes.so"));
            }
            other => panic!("want Error::LibraryRead naming the missing file, got {other:?}"),
        }
        assert!(
            err.to_string().starts_with("chtypes: cannot read library "),
            "got {err}"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    // `Registry::open` — `new`/`with_timezone`/`from_env*` route through it now,
    // so the tests above passing unchanged (they all go through
    // `from_search_path_with` or `new`) are the regression proof for those.
    // These cover only what `open` adds.

    /// The shape every search-path-only-field refusal must have: `Error::Registry`
    /// with an `InvalidInput` source naming `field` AND
    /// `Registry::from_search_path_with`. `InvalidInput` (rather than, say,
    /// `NotFound`) is itself the proof the refusal ran BEFORE `read_dir` — the
    /// directory passed by every caller below does not exist.
    fn assert_refused_field(err: &Error, field: &str) {
        match err {
            Error::Registry { source, .. } => {
                assert_eq!(
                    source.kind(),
                    std::io::ErrorKind::InvalidInput,
                    "a refusal for {field} must not touch disk (this dir does not exist; \
                     NotFound here would mean Registry::open tried to scan it instead of \
                     refusing first): got {source:?}"
                );
                let text = source.to_string();
                assert!(
                    text.contains(field),
                    "the refusal must name {field}, got {text:?}"
                );
                assert!(
                    text.contains("Registry::from_search_path_with"),
                    "the refusal must name the constructor that honors {field}, got {text:?}"
                );
            }
            other => panic!("want Error::Registry naming {field}, got {other:?}"),
        }
    }

    #[test]
    fn open_refuses_a_non_default_dir_before_touching_disk() {
        let missing =
            std::env::temp_dir().join(format!("chtypes-rs-open-refuse-dir-{}", std::process::id()));
        let err = Registry::open(
            &missing,
            RegistryOptions {
                dir: Some(PathBuf::from("/somewhere/else")),
                ..RegistryOptions::default()
            },
        )
        .expect_err("RegistryOptions.dir is not honored by Registry::open");
        assert_refused_field(&err, "RegistryOptions.dir");
    }

    #[test]
    fn open_refuses_a_non_default_autofetch_before_touching_disk() {
        let missing = std::env::temp_dir().join(format!(
            "chtypes-rs-open-refuse-autofetch-{}",
            std::process::id()
        ));
        let err = Registry::open(
            &missing,
            RegistryOptions {
                autofetch: Some(true),
                ..RegistryOptions::default()
            },
        )
        .expect_err("RegistryOptions.autofetch is not honored by Registry::open");
        assert_refused_field(&err, "RegistryOptions.autofetch");
    }

    #[cfg(feature = "fetch")]
    #[test]
    fn open_refuses_a_non_default_fetch_before_touching_disk() {
        let missing = std::env::temp_dir().join(format!(
            "chtypes-rs-open-refuse-fetch-{}",
            std::process::id()
        ));
        let err = Registry::open(
            &missing,
            RegistryOptions {
                fetch: crate::fetch::EnsureOptions {
                    force: true,
                    ..crate::fetch::EnsureOptions::default()
                },
                ..RegistryOptions::default()
            },
        )
        .expect_err("RegistryOptions.fetch is not honored by Registry::open");
        assert_refused_field(&err, "RegistryOptions.fetch");
    }

    #[test]
    fn open_with_verify_checksums_refuses_a_mismatch_at_construction() {
        let dir = fake_artifact("open-mismatch", Some(&"00".repeat(32)));
        let err = Registry::open(
            &dir,
            RegistryOptions {
                verify_checksums: true,
                preload: vec!["25.8".into()],
                ..RegistryOptions::default()
            },
        )
        .expect_err("a text file's hash cannot match the manifest's fabricated one");
        assert!(
            matches!(err, Error::ChecksumMismatch { .. }),
            "want the checksum refusal AT CONSTRUCTION, which is where a preloaded line opens, got {err:?}"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn open_with_verify_checksums_refuses_a_manifest_with_no_hash() {
        let dir = fake_artifact("open-nohash", None);
        let err = Registry::open(
            &dir,
            RegistryOptions {
                verify_checksums: true,
                preload: vec!["25.8".into()],
                ..RegistryOptions::default()
            },
        )
        .expect_err("verification asked for and not possible is not verification");
        let text = err.to_string();
        assert!(
            matches!(err, Error::Registry { .. }) && text.contains("library_sha256"),
            "want a refusal naming the unverifiable artifact, got {err:?}"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn open_with_verify_checksums_accepts_matching_bytes_and_loads_on() {
        // sha256 of "not a shared library", the body fake_artifact writes —
        // mirrors verify_checksums_accepts_matching_bytes_and_loads_on's
        // assertion for the search-path registry: matching bytes must clear
        // verification and fail only at dlopen.
        let dir = fake_artifact(
            "open-match",
            Some(&crate::digest::sha256_hex(b"not a shared library")),
        );
        let err = Registry::open(
            &dir,
            RegistryOptions {
                verify_checksums: true,
                preload: vec!["25.8".into()],
                ..RegistryOptions::default()
            },
        )
        .expect_err("a text file cannot dlopen; the open must fail");
        assert!(
            matches!(err, Error::Load { .. } | Error::NotAnArtifact { .. }),
            "matching bytes must pass verification and fail at dlopen, got {err:?}"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    // -------------------------------------------------------- #284: exact-patch resolution
    //
    // The resolution SEAM (P11 of the common #284 brief): `newest_patch_in`,
    // `locate_in`, `locate_patch_in`, `patch_matches` and `is_patch_spelling`
    // are pure filesystem/string functions that decide WHICH directory a
    // request resolves to without ever `dlopen`ing anything, so they are
    // tested directly here — no loadable artifact needed. End-to-end
    // `Registry::resolve`/`for_version` behavior against a REAL patch is
    // covered by the fixture-backed `two_patches_*` suite in
    // `tests/fetch.rs`, which this crate's fake text "libraries" cannot
    // stand in for past the point `dlopen` is reached.

    /// A fake, non-dlopenable artifact at an arbitrary path — flat or
    /// nested — mirroring [`fake_artifact`] but letting the caller choose
    /// exactly where it lands.
    fn fake_patch_at(dir: &Path, minor: &str, version: &str) -> PathBuf {
        let body = format!("not a shared library ({version})");
        std::fs::create_dir_all(dir).unwrap();
        std::fs::write(dir.join("libchtypes.so"), body.as_bytes()).unwrap();
        std::fs::write(
            dir.join("manifest.json"),
            format!(
                r#"{{"library":"libchtypes.so","library_bytes":{},"clickhouse_version":"{version}","clickhouse_minor":"{minor}"}}"#,
                body.len()
            ),
        )
        .unwrap();
        dir.to_path_buf()
    }

    fn scratch_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "chtypes-rs-284-{}-{tag}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        std::fs::remove_dir_all(&dir).ok();
        dir
    }

    #[test]
    fn r1_patch_vs_line_spelling() {
        for line in ["25.8", "25.10", " 25.8 ", "25.8.28"] {
            assert!(!is_patch_spelling(line), "{line:?} must be a LINE");
        }
        for patch in [
            "25.8.28.1",
            "25.8.28.1-lts",
            " 25.8.28.1-lts ",
            "26.9.3.38-stable",
        ] {
            assert!(is_patch_spelling(patch), "{patch:?} must be a PATCH");
        }
    }

    #[test]
    fn decision_7_patch_matching() {
        // A channeled request matches only that channel.
        assert!(patch_matches("25.8.28.1-lts", "25.8.28.1-lts"));
        assert!(!patch_matches("25.8.28.1-lts", "25.8.28.1-stable"));
        // A bare request matches that patch on ANY channel.
        assert!(patch_matches("25.8.28.1", "25.8.28.1-lts"));
        assert!(patch_matches("25.8.28.1", "25.8.28.1-stable"));
        // A different patch never matches, channel or not.
        assert!(!patch_matches("25.8.28.1", "25.8.33.5-lts"));
        assert!(!patch_matches("25.8.28.1-lts", "25.8.33.5-lts"));
    }

    #[test]
    fn version_key_orders_numerically_and_ignores_the_channel() {
        assert!(version_key("25.10.7.6-stable") > version_key("25.8.33.5-lts"));
        assert!(version_key("25.8.33.5-lts") > version_key("25.8.28.1-lts"));
        assert_eq!(
            version_key("25.8.28.1-lts"),
            version_key("25.8.28.1-stable")
        );
    }

    /// R3: the newest patch of a line in ONE directory, counting both the
    /// flat slot and its `patches/<minor>/` children; a nested install wins
    /// a tie at the same version.
    #[test]
    fn newest_patch_in_two_levels_and_ties() {
        let root = scratch_dir("newest");
        fake_patch_at(&root.join("25.8"), "25.8", "25.8.28.1-lts");
        fake_patch_at(
            &root.join("patches").join("25.8").join("25.8.33.5-lts"),
            "25.8",
            "25.8.33.5-lts",
        );
        assert_eq!(
            newest_patch_in(&root, "25.8"),
            Some(root.join("patches").join("25.8").join("25.8.33.5-lts")),
            "the nested, NEWER patch must win"
        );
        std::fs::remove_dir_all(&root).ok();

        // A tie at the same version: nested beats flat.
        let root = scratch_dir("tie");
        fake_patch_at(&root.join("25.8"), "25.8", "25.8.28.1-lts");
        fake_patch_at(
            &root.join("patches").join("25.8").join("25.8.28.1-lts"),
            "25.8",
            "25.8.28.1-lts",
        );
        assert_eq!(
            newest_patch_in(&root, "25.8"),
            Some(root.join("patches").join("25.8").join("25.8.28.1-lts")),
            "on a tie, the NESTED install must win"
        );
        std::fs::remove_dir_all(&root).ok();

        // Nothing at all.
        let root = scratch_dir("none");
        std::fs::create_dir_all(&root).unwrap();
        assert_eq!(newest_patch_in(&root, "25.8"), None);
        std::fs::remove_dir_all(&root).ok();
    }

    /// [`locate_in`] is [`newest_patch_in`] across a search path, first
    /// directory that holds ANY patch of the line wins — even when a LATER
    /// directory holds a newer one.
    #[test]
    fn locate_in_takes_the_first_directory_that_holds_the_line() {
        let a = scratch_dir("locate-a");
        let b = scratch_dir("locate-b");
        fake_patch_at(&a.join("25.8"), "25.8", "25.8.28.1-lts");
        fake_patch_at(&b.join("25.8"), "25.8", "25.8.33.5-lts");
        let dirs = vec![a.clone(), b.clone()];
        assert_eq!(locate_in(&dirs, "25.8"), Some(a.join("25.8")));
        // 26.7 exists only in `b`.
        fake_patch_at(&b.join("26.7"), "26.7", "26.7.3.19-stable");
        assert_eq!(locate_in(&dirs, "26.7"), Some(b.join("26.7")));
        std::fs::remove_dir_all(&a).ok();
        std::fs::remove_dir_all(&b).ok();
    }

    /// R4 step 2: [`locate_patch_in`] scans the WHOLE search path, not just
    /// the first directory — the more specific request wins over the
    /// first-directory-only rule a line uses.
    #[test]
    fn locate_patch_in_scans_the_whole_search_path() {
        let a = scratch_dir("patch-a");
        let b = scratch_dir("patch-b");
        fake_patch_at(&a.join("25.8"), "25.8", "25.8.28.1-lts");
        fake_patch_at(
            &b.join("patches").join("25.8").join("25.8.33.5-lts"),
            "25.8",
            "25.8.33.5-lts",
        );
        let dirs = vec![a.clone(), b.clone()];
        // `a` has ONLY 28.1, so a request for 33.5 must reach into `b`,
        // never falling back to what `a` happens to hold.
        assert_eq!(
            locate_patch_in(&dirs, "25.8.33.5-lts"),
            Some(b.join("patches").join("25.8").join("25.8.33.5-lts"))
        );
        // Decision 7: a bare spelling matches the channeled install.
        assert_eq!(locate_patch_in(&dirs, "25.8.28.1"), Some(a.join("25.8")));
        // Nothing matches a patch that is not there.
        assert_eq!(locate_patch_in(&dirs, "25.8.30.2-lts"), None);
        std::fs::remove_dir_all(&a).ok();
        std::fs::remove_dir_all(&b).ok();
    }

    /// §4's two-level scan requirement: a line present ONLY under
    /// `patches/…`, with no flat slot at all, still counts as installed —
    /// for both [`installed_lines`] and [`installed_patches`], and (P19) a
    /// registry over such a directory is not "empty" at construction.
    #[test]
    fn nested_only_lines_count_as_installed() {
        let root = scratch_dir("nested-only");
        fake_patch_at(
            &root.join("patches").join("25.8").join("25.8.33.5-lts"),
            "25.8",
            "25.8.33.5-lts",
        );
        let dirs = vec![root.clone()];
        let lines = installed_lines(&dirs);
        assert_eq!(lines.len(), 1, "{lines:?}");
        assert_eq!(lines[0].0, "25.8");
        assert_eq!(
            lines[0].1,
            root.join("patches").join("25.8").join("25.8.33.5-lts")
        );

        let patches = installed_patches(&dirs);
        assert_eq!(patches.len(), 1, "{patches:?}");
        assert!(!patches[0].flat);
        assert_eq!(patches[0].version, "25.8.33.5-lts");

        // P19: construction must not see this as an empty registry.
        let reg = Registry::from_search_path_with(RegistryOptions {
            dir: Some(root.clone()),
            ..RegistryOptions::default()
        })
        .expect("a nested-only install is not an empty registry");
        assert!(reg.versions().contains(&"25.8".to_string()));
        std::fs::remove_dir_all(&root).ok();
    }

    /// [`installed_patches`] lists EVERY installed patch, flat and nested
    /// alike — the per-patch listing that supersedes [`installed_lines`] as
    /// `fetch.list-installed`.
    #[test]
    fn installed_patches_lists_flat_and_nested() {
        let root = scratch_dir("installed-patches");
        fake_patch_at(&root.join("25.8"), "25.8", "25.8.33.5-lts");
        fake_patch_at(
            &root.join("patches").join("25.8").join("25.8.28.1-lts"),
            "25.8",
            "25.8.28.1-lts",
        );
        let dirs = vec![root.clone()];
        let mut patches = installed_patches(&dirs);
        patches.sort_by(|a, b| a.version.cmp(&b.version));
        assert_eq!(patches.len(), 2, "{patches:?}");
        assert_eq!(patches[0].version, "25.8.28.1-lts");
        assert!(!patches[0].flat);
        assert_eq!(patches[1].version, "25.8.33.5-lts");
        assert!(patches[1].flat);
        std::fs::remove_dir_all(&root).ok();
    }

    /// R-c's fallback cache: a value just written is readable back
    /// immediately (the mechanism itself); the 60s expiry boundary is not
    /// exercised here (a real-time sleep would make this suite slow for a
    /// window nothing else in it depends on) but is covered by
    /// [`fallback_within_line`]'s use of [`FALLBACK_RECHECK`] directly.
    #[test]
    fn fallback_cache_round_trips() {
        let key = format!("test-only-284-{:?}", std::thread::current().id());
        let dir = scratch_dir("fallback-cache");
        std::fs::create_dir_all(&dir).unwrap();
        assert!(fallback_cache_get(&key).is_none());
        fallback_cache_put(&key, "25.8.28.1-lts", &dir);
        let got = fallback_cache_get(&key).expect("just written");
        assert_eq!(got.version, "25.8.28.1-lts");
        assert_eq!(got.dir, dir);
        std::fs::remove_dir_all(&dir).ok();
    }

    /// R4 step 4 + §3: a patch request with nothing exact installed falls
    /// back to the newest patch of its line and warns once for the
    /// (requested, actual) pair — recorded even though the fake fixture then
    /// fails to `dlopen` (the warning is emitted from the manifest read,
    /// before the load is attempted, precisely so a caller sees it even
    /// when the load then fails).
    #[test]
    fn patch_fallback_warns_once_per_pair() {
        let root = scratch_dir("fallback-warn");
        fake_patch_at(&root.join("25.8"), "25.8", "25.8.28.1-lts");
        let reg = Registry::from_search_path_with(RegistryOptions {
            dir: Some(root.clone()),
            ..RegistryOptions::default()
        })
        .unwrap();

        let requested = format!("25.8.30.{}-lts", std::process::id() % 9973);
        let pair = (requested.clone(), "25.8.28.1-lts".to_string());
        {
            warned_pairs().lock().unwrap().remove(&pair);
        }
        let _ = reg.resolve(&requested); // fails to dlopen; the warning must still fire
        assert!(
            warned_pairs().lock().unwrap().contains(&pair),
            "the fallback must be recorded even though the load then failed"
        );
        let _ = reg.resolve(&requested);
        assert_eq!(
            warned_pairs()
                .lock()
                .unwrap()
                .iter()
                .filter(|p| **p == pair)
                .count(),
            1,
            "one entry, not duplicated, after a second resolve of the same pair"
        );
        std::fs::remove_dir_all(&root).ok();
    }

    // NOTE: `Loaded::pin_line`'s "never moves once set" property (R3/R8)
    // needs a SECOND real, distinct, `dlopen`-able artifact of the same line
    // to observe past construction — this crate's fake fixture text never
    // loads, so `resolve_line` never actually reaches the successful-load
    // point that would set a pin. That makes it untestable at the unit level
    // here regardless of fixture availability; it is exercised end-to-end
    // only where two real artifacts of one line exist (the produced-artifact
    // CI job, per the design's P18/`CHTYPES_TWO_PATCHES`), which this
    // repository's own suite cannot provide. Left as a known gap rather than
    // a test that would just restate `HashMap::Entry::or_insert_with`'s
    // documented behavior.
}
