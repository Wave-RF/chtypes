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
    /// library is what is actually passed to `chs_init`.
    #[serde(default)]
    pub unsafe_families: String,
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
    /// without fetching. Resolution is [`Registry::for_version`]'s minus the
    /// fetch, and so is the failure.
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
            let minor = minor_of(version);
            match self.resolve_without_fetch(version, &minor)? {
                Some(_) => {}
                None => return Err(self.missing(&minor)),
            }
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

    /// Resolve a version to its library, **opening it if it is not open yet**.
    /// A minor line (`"25.8"`) or an exact patch (`"25.8.28.1-lts"`) both
    /// work, and a *drifted* patch resolves to its minor line — asking for
    /// `"25.8.30.16"` finds the loaded `25.8`.
    ///
    /// This is what opens an artifact; construction does not. The line is
    /// taken from the first directory on this registry's path that holds
    /// `<minor>/manifest.json` (`docs/guides/fetch.md` §1) and that one
    /// artifact is loaded, once, however many threads ask at the same time.
    ///
    /// Resolution never falls back to the nearest version: answering 26.7
    /// semantics from a 25.8 artifact would be a lie, and version behavior is
    /// not monotonic (25.10 rejects a mixed-type DEFAULT that 25.8 and 26.6
    /// both accept).
    ///
    /// # Errors
    ///
    /// * [`Error::ArtifactMissing`] — no directory holds the line; the message
    ///   names every directory looked in and the fetch command. With autofetch
    ///   on it is fetched first through [`crate::ensure`] (once per process
    ///   per line, under one process-wide lock, so concurrent opens fetch
    ///   once) and the fetch's own error surfaces when that fails.
    ///
    ///   A one-directory registry answers this too, where it used to answer
    ///   [`Error::NoSuchVersion`]: under lazy loading "what IS loaded" is
    ///   "nothing", so naming the directory, the platform and the fetch
    ///   command is the useful answer, and it is what the other three give.
    /// * [`Error::LibraryRead`], [`Error::CorruptArtifact`],
    ///   [`Error::ChecksumMismatch`], [`Error::VersionMismatch`],
    ///   [`Error::Registry`], and everything [`Library::load`] can return —
    ///   the directory holds the line and the artifact is bad. These moved
    ///   here from the constructor when loading did.
    pub fn for_version(&self, version: &str) -> Result<Arc<Library>> {
        if let Some(l) = self.loaded().get(version) {
            return Ok(l);
        }
        let minor = minor_of(version);
        let _opening = self.opening.lock().unwrap_or_else(|p| p.into_inner());
        // Another thread may have opened it while this one waited.
        if let Some(l) = self.loaded().get(version) {
            return Ok(l);
        }
        if let Some(l) = self.resolve_without_fetch(version, &minor)? {
            return Ok(l);
        }
        let dir = self.autofetch_or_missing(&minor)?;
        self.load_into(&dir)
    }

    /// Resolution minus the fetch: what is already open, then the first
    /// directory on this registry's path that holds the line. `Ok(None)` means
    /// no directory holds it — a fetch's cue on the [`Registry::for_version`]
    /// path and [`Error::ArtifactMissing`] on the preload path. Preload never
    /// fetches, and this is the one function that makes those two paths
    /// identical in everything else.
    fn resolve_without_fetch(&self, version: &str, minor: &str) -> Result<Option<Arc<Library>>> {
        if let Some(l) = self.loaded().get(version) {
            return Ok(Some(l));
        }
        match locate_in(&self.search, minor) {
            Some(dir) => self.load_into(&dir).map(Some),
            None => Ok(None),
        }
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
}
