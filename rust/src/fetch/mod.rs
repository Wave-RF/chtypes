//! Fetching, verifying and installing artifacts — `docs/guides/fetch.md`, the
//! contract every SDK implements identically. Behind the `fetch` feature
//! (on by default).
//!
//! [`ensure`] is the function; the `chtypes` binary (`cargo install chtypes`
//! → `chtypes fetch 25.8`) is the command over it. Nothing here is a verdict
//! but the chain (§3): the signature over `SHA256SUMS`, the index agreeing
//! with it, the tarball hashed before it is unpacked, the installed library
//! re-hashed where it landed. No exit code, no `Content-Length`, no "download
//! finished" ever is.

mod install;
mod lock;
mod release;
mod source;
mod trust;

use std::path::{Path, PathBuf};

pub use lock::{DEFAULT_LOCK_FILE, LOCK_SCHEMA, LockEntry, LockFile, lock_key};
pub use release::IndexRow;
pub use source::{ARTIFACTS_URL_ENV, DEFAULT_ARTIFACTS_URL, DEFAULT_TAG, DOWNLOAD_TOKEN_ENV};
pub use trust::{
    ALLOW_UNSIGNED_ENV, RELEASE_KEY_ID, RELEASE_PUBLIC_KEY, RELEASE_PUBLIC_KEY_HEX,
    TRUSTED_KEYS_ENV, TrustPolicy, key_id, parse_key_hex, parse_signature_file, sha256_file,
    sha256_hex, verify_signature,
};

use crate::legacy::error::{Error, FETCH_COMMAND, Result};
pub(crate) use crate::legacy::registry::minor_of;
use crate::legacy::registry::{
    Manifest, host_platform, install_dir_for, locate_in, locate_patch_in, search_path_for,
};
use release::{Release, Request};
use source::Source;

/// Where an install lands (docs/guides/fetch.md, THE LAYOUT RULE): the flat
/// `<minor>/` slot a LINE request loads, or a specific patch's own
/// `patches/<minor>/<version>/` directory. Determined once, from the kind of
/// request that selected the row — never from whether the version happens to
/// be the line's newest.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum InstallSlot {
    /// `<dest>/<minor>/` — what a LINE request loads.
    Flat,
    /// `<dest>/patches/<minor>/<clickhouse_version>/` — any other exact
    /// patch.
    Patch,
}

thread_local! {
    /// The test-only override of the ABI revision fetch selects rows at, for
    /// the calling thread; see [`__set_fetch_abi_revision_for_tests`].
    static ABI_REVISION_OVERRIDE: std::cell::Cell<Option<i32>> = const { std::cell::Cell::new(None) };
}

/// For this crate's tests only; not a supported API. An artifact at another
/// revision still refuses to load.
///
/// The environment variable the test-only override is also read from, for a
/// process the test suite spawns (the `chtypes` binary, a re-executed test) —
/// an internal, undocumented test hook exactly like
/// `CHTYPES_FETCH_TEST_RETRY_DELAY_MS`, never part of the public contract
/// (`docs/guides/fetch.md` documents no such variable).
const ABI_REVISION_TEST_ENV: &str = "CHTYPES_FETCH_TEST_ABI_REVISION";

/// For this crate's tests only; not a supported API. An artifact at another
/// revision still refuses to load.
///
/// Hidden from the docs, exempt from any stability promise, and meant for this
/// crate's own fetch-fixture suite and nothing else.
///
/// Sets, for the calling thread, the ABI revision fetch selects release rows
/// at in place of [`crate::legacy::error::ABI_REVISION`] (`docs/guides/fetch.md` §2), and
/// returns the previous override. The fixture suite sets it to the revision
/// its fixture release carries — derived from that release's own
/// `index.json` — so a crate whose own revision has moved ahead of the
/// fixtures still exercises the whole chain. It changes WHICH rows are
/// eligible and nothing else: the default registry directory stays
/// `abi<ABI_REVISION>/`, and the loader still refuses an artifact of another
/// revision. `pub` only because an integration test is a separate crate that
/// cannot reach a `pub(crate)` item.
#[doc(hidden)]
pub fn __set_fetch_abi_revision_for_tests(revision: Option<i32>) -> Option<i32> {
    ABI_REVISION_OVERRIDE.with(|cell| cell.replace(revision))
}

/// Not part of the public API — hidden from the docs, exempt from any
/// stability promise — and here only because the `chtypes` binary is a
/// separate crate: the revision fetch selects at, and `list`'s one line naming
/// the platform's rows at another revision (`None` when none), which `list`
/// does not show (`docs/guides/fetch.md` §6).
#[doc(hidden)]
pub fn __list_revision(rows: &[IndexRow], platform: &str) -> (i32, Option<String>) {
    let revision = fetch_abi_revision();
    (revision, release::not_shown(rows, platform, revision))
}

/// The one ABI revision whose rows fetch may install: this crate's own,
/// unless the test-only override above names another.
pub(crate) fn fetch_abi_revision() -> i32 {
    if let Some(revision) = ABI_REVISION_OVERRIDE.with(std::cell::Cell::get) {
        return revision;
    }
    // CHTYPES_FETCH_TEST_ABI_REVISION: for this crate's tests only; not a
    // supported API. An artifact at another revision still refuses to load.
    std::env::var(ABI_REVISION_TEST_ENV)
        .ok()
        .and_then(|raw| raw.trim().parse().ok())
        .unwrap_or(crate::legacy::error::ABI_REVISION)
}

/// How [`ensure`] fetches. `Default` is what `chtypes fetch <line>` does with
/// no flags: this host's platform, the artifacts host's rolling release, the
/// environment's trust policy, no lock, quiet.
///
/// `PartialEq` is derived so the v0 `Registry::open` can
/// tell whether the v0 `RegistryOptions::fetch` was
/// left at its default — every field here is a `bool`, a `String`/`PathBuf`,
/// or an `Option`/`Vec` of one, so the comparison is structural and exact,
/// never a `Debug`-string comparison standing in for one.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct EnsureOptions {
    /// An explicit registry directory (`--dest`; `docs/guides/fetch.md` §1 item 1).
    /// Searched first and written to. `None` = `$CHTYPES_REGISTRY`, else the
    /// per-user cache.
    pub dest: Option<PathBuf>,
    /// `<os>-<arch>` to fetch for (`--platform`). `None` = this host. A
    /// foreign platform's artifacts install into that platform's own cache
    /// (they are for a container, not this process).
    pub platform: Option<String>,
    /// `--url`: any base — `https://…`, `file:///…`, or a plain directory.
    /// `None` = `$CHTYPES_ARTIFACTS_URL` (default the artifacts host) under
    /// `tag`.
    pub url: Option<String>,
    /// `--tag`: a release tag on the artifacts host (`v1.2.0`); `None` = the
    /// rolling `artifacts`. Exclusive with `url`.
    pub tag: Option<String>,
    /// `--lock <file>`: record what was installed (§5); with `frozen`, the
    /// file to enforce. `None` with `frozen` = `chtypes.lock` in the working
    /// directory.
    pub lock: Option<PathBuf>,
    /// `--frozen`: refuse anything the lock does not pin
    /// ([`Error::ArtifactPinned`]).
    pub frozen: bool,
    /// `--force`: re-download even when installed and verified.
    pub force: bool,
    /// `--offline`: never touch the network. An installed-and-verified line
    /// still succeeds; anything that needs the source is
    /// [`Error::SourceUnreachable`] before a connection is attempted.
    pub offline: bool,
    /// Trusted keys as raw hex, replacing the embedded release key. `None` =
    /// `$CHTYPES_TRUSTED_KEYS`, else the embedded key (§4).
    pub trusted_keys: Option<Vec<String>>,
    /// Skip the signature step, loudly. `None` = `$CHTYPES_ALLOW_UNSIGNED=1`.
    pub allow_unsigned: Option<bool>,
    /// Print progress on stderr (the binary does; the library default is
    /// quiet). The unsigned warning prints regardless.
    pub progress: bool,
}

/// What [`ensure`] did.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    /// The line was already installed and its library hashed as it should;
    /// nothing was downloaded.
    AlreadyInstalled,
    /// Fetched and installed into an empty slot.
    Installed,
    /// Fetched and installed over a previous (stale or corrupt) install.
    Replaced,
}

/// An installed, verified line: what [`ensure`] returns.
#[derive(Debug, Clone)]
pub struct Installed {
    /// `<registry>/<minor>` — the directory a registry loads.
    pub dir: PathBuf,
    /// The minor line, `25.8`.
    pub line: String,
    /// The exact ClickHouse release, from the manifest.
    pub version: String,
    /// The shared library's path.
    pub library: PathBuf,
    /// Its sha256, hashed in place.
    pub library_sha256: String,
    /// `<os>-<arch>`.
    pub platform: String,
    /// What was done.
    pub action: Action,
    /// The release asset `(file, sha256)` this install corresponds to, when
    /// the release was consulted — what a lock file records.
    pub asset: Option<(String, String)>,
}

/// The result of re-hashing one installed line (`chtypes verify`).
#[derive(Debug)]
pub struct Verification {
    /// `<registry>/<minor>`.
    pub dir: PathBuf,
    /// The minor line (the directory name).
    pub line: String,
    /// `manifest.clickhouse_version`.
    pub version: String,
    /// The library the manifest names.
    pub library: PathBuf,
    /// `manifest.library_sha256`.
    pub expected: String,
    /// `Ok(sha256)` when the file hashes as the manifest says; otherwise the
    /// [`Error::ArtifactCorrupt`] (or the I/O failure as [`Error::Fetch`]).
    pub result: Result<String>,
}

/// What a release offers (`chtypes list`).
#[derive(Debug)]
pub struct ReleaseInfo {
    /// The source that was read.
    pub origin: String,
    /// The id of the key that signed `SHA256SUMS`; `None` when the signature
    /// step was skipped under `CHTYPES_ALLOW_UNSIGNED=1`.
    pub signed_by: Option<String>,
    /// The artifacts' license, as the listing names it.
    pub license: String,
    /// Every published artifact, as listed.
    pub artifacts: Vec<IndexRow>,
}

/// Make ClickHouse `line` (`25.8`, or an exact patch `25.8.28.1-lts`)
/// installed and verified for this host, fetching it through the
/// `docs/guides/fetch.md` §3 chain when it is not.
///
/// Idempotent: a line already installed whose library hashes what the signed
/// release lists is [`Action::AlreadyInstalled`] and nothing is downloaded —
/// the release's three small files are read, the tarball is not. Only
/// [`EnsureOptions::offline`] reads no source: then an installed line that
/// hashes what its own manifest says is the answer, and nothing else is
/// (`docs/guides/fetch.md`, Decisions). "Installed" means anywhere on the §1 search
/// path when no [`EnsureOptions::dest`] is given (what a registry would
/// find), and in `dest` itself when one is (the caller named where the line
/// must be). Otherwise the asset for the line is downloaded, hashed, unpacked
/// into a temporary sibling, renamed into `<dest>/<minor>/` and re-hashed in
/// place. With a lock file ([`EnsureOptions::lock`]) the pin is recorded or,
/// under `frozen`, enforced.
///
/// The directory it returns is what the v0 `Registry::new` loads.
///
/// # Errors
///
/// Each carries its shared code ([`Error::artifact_code`]):
///
/// * [`Error::ArtifactUntrusted`] — `SHA256SUMS` unsigned or mis-signed.
/// * [`Error::ArtifactCorrupt`] — any hash mismatch in the chain.
/// * [`Error::ArtifactPinned`] — `frozen` and the release offers something
///   else.
/// * [`Error::ArtifactUnpublished`] — nothing for the line/platform.
/// * [`Error::SourceUnreachable`] — offline or the source failed.
/// * [`Error::Fetch`] — an unusable option, listing or install directory.
pub fn ensure(line: &str, opts: &EnsureOptions) -> Result<Installed> {
    let request = Request::parse(line)?;
    let platform = platform_of(opts)?;
    let dest = install_dir_for(&platform, opts.dest.as_deref());
    let search = installed_search(opts, &platform, &dest);
    // THE LAYOUT RULE: the patch a LINE request selects installs flat; any
    // OTHER exact patch installs under patches/. Decided by the KIND of
    // request, never by whether the version happens to be the line's newest.
    let slot = if request.exact.is_some() {
        InstallSlot::Patch
    } else {
        InstallSlot::Flat
    };

    // `--offline` is the no-source path (§6): an installed patch whose
    // library hashes what its own manifest says is the answer, and nothing
    // else is. Otherwise the signed release is read first — SHA256SUMS, its
    // signature, index.json; never the tarball — and "installed" means
    // hashing what that listing says (§3), the same in all four SDKs
    // (docs/guides/fetch.md, Decisions). A LINE request is located with the
    // two-level, first-directory-wins scan (R3); an exact patch is looked
    // for anywhere on the whole search path (R4 step 2).
    let found = located_installed(&search, &request);
    if opts.offline && !opts.force {
        if let Some(inst) = &found {
            if request.accepts(&inst.manifest.clickhouse_version) {
                let sha = inst.verify()?;
                say(
                    opts,
                    &format!(
                        "already installed and verified: {}",
                        inst.library().display()
                    ),
                );
                return Ok(inst.installed(&platform, sha, None));
            }
        }
    }

    check_lock_revision_early(opts, &platform, &request)?;

    let source = Source::resolve(opts.url.as_deref(), opts.tag.as_deref(), opts.offline)?;
    let policy = policy_of(opts)?;
    let release = Release::load(&source, &policy, opts.progress)?;

    let mut lock = lock_of(opts)?;
    let (row, key) = if opts.frozen {
        let (row, key) = frozen_row(
            lock.as_ref().expect("frozen always has a lock"),
            &release,
            &platform,
            &request,
        )?;
        (row, key)
    } else {
        let row = release.select(&request, &platform)?;
        (row, lock_key(&platform, &row.clickhouse_version))
    };
    say(
        opts,
        &format!(
            "ClickHouse {} -> patch {} (line {}), {platform}, from {}",
            request.spelling, row.clickhouse_version, row.clickhouse_minor, release.origin
        ),
    );

    // Installed and hashing what the release says: nothing to download. A
    // LINE request whose selected patch is already installed, but only under
    // patches/ (an earlier exact-patch fetch put it there), is PROMOTED into
    // the flat slot — a local move, no network — so the flat slot keeps
    // holding the line's newest, as the layout rule requires.
    if !opts.force {
        if let Some(inst) = &found {
            if inst.manifest.clickhouse_version == row.clickhouse_version
                && inst.manifest.library == row.library
            {
                if let Ok(sha) = inst.verify() {
                    if sha == row.library_sha256 {
                        let final_dir =
                            seat_existing(&dest, slot, &row.clickhouse_minor, &inst.dir)?;
                        let final_inst: InstalledDir = if final_dir == inst.dir {
                            inst.clone()
                        } else {
                            InstalledDir::read(&final_dir)?
                        };
                        say(
                            opts,
                            &format!(
                                "already installed and verified: {}",
                                final_inst.library().display()
                            ),
                        );
                        let installed = final_inst.installed(
                            &platform,
                            sha,
                            Some((row.file.clone(), row.sha256.clone())),
                        );
                        record(lock.as_mut(), opts, &key, row)?;
                        return Ok(installed);
                    }
                }
            }
        }
    }

    let installed =
        install::fetch_and_install(&source, row, &dest, &platform, slot, opts.progress)?;
    record(lock.as_mut(), opts, &key, row)?;
    install_goldens(&source, &release, &policy, &dest, opts.progress);
    Ok(installed)
}

/// `ensure`'s pre-check: a LINE request is located with [`locate_in`]'s
/// two-level, first-directory-wins scan (R3); an exact patch is looked for
/// anywhere on the whole search path with [`locate_patch_in`] (R4 step 2).
fn located_installed(search: &[PathBuf], request: &Request) -> Option<InstalledDir> {
    let dir = match &request.exact {
        Some(exact) => locate_patch_in(search, exact),
        None => locate_in(search, &request.minor),
    }?;
    InstalledDir::read(&dir).ok()
}

/// Promote an already-verified install into the flat slot when a LINE
/// request (`slot == Flat`) selected it and it currently sits only under
/// `<dest>/patches/…` — a local rename, demoting whatever is in the flat
/// slot first (THE LAYOUT RULE), never a re-download. An install found
/// elsewhere on the search path (a system location, another `--dest`) is
/// left exactly where it is: only a directory already inside THIS `dest`'s
/// own `patches/` is ours to move.
fn seat_existing(
    dest: &Path,
    slot: InstallSlot,
    minor: &str,
    existing_dir: &Path,
) -> Result<PathBuf> {
    if slot == InstallSlot::Patch {
        return Ok(existing_dir.to_path_buf());
    }
    let flat = dest.join(minor);
    if existing_dir == flat {
        return Ok(flat);
    }
    if !existing_dir.starts_with(dest.join("patches")) {
        return Ok(existing_dir.to_path_buf());
    }
    let (target, _action) = install::seat_flat(dest, minor, existing_dir)?;
    Ok(target)
}

/// F1–F5: resolve the ONE row `--frozen` installs for `request`, from the
/// lock's own candidates alone — never the release's unpinned rows.
///
/// # Errors
///
/// [`Error::ArtifactPinned`] — the lock pins nothing for `request` on
/// `platform`, or the row the candidate names carries another ABI revision,
/// or the candidate's sha256 disagrees with what the release lists for that
/// file. [`Error::ArtifactUnpublished`] — the release does not list the
/// pinned file at all. [`Error::ArtifactCorrupt`] — the release disagrees
/// with its own signed sums about that file (the same check every install
/// makes).
fn frozen_row<'a>(
    lock: &LockFile,
    release: &'a Release,
    platform: &str,
    request: &Request,
) -> Result<(&'a IndexRow, String)> {
    let (key, entry) =
        lock.best_candidate(platform, request)
            .ok_or_else(|| Error::ArtifactPinned {
                key: format!("{platform}/{}", request.spelling),
                message: format!(
                    "{} pins nothing for {platform}/{}",
                    lock.path().display(),
                    request.spelling
                ),
            })?;
    let row = release
        .rows()
        .iter()
        .find(|r| r.platform() == platform && r.file == entry.file)
        .ok_or_else(|| Error::ArtifactUnpublished {
            requested: request.spelling.clone(),
            platform: platform.to_string(),
            origin: release.origin.clone(),
            offered: format!(
                "the release at {} does not list {}, which {} pins for {key}.{}",
                release.origin,
                entry.file,
                lock.path().display(),
                no_revision_note(lock, entry, &request.spelling)
            ),
        })?;
    if let Some(row_rev) = row.abi_revision {
        let rev = fetch_abi_revision();
        if row_rev != rev {
            return Err(Error::ArtifactPinned {
                key: key.to_string(),
                message: format!(
                    "{} pins {key} -> {}, but the release's row for it is ABI revision \
                     {row_rev}, not this SDK's {rev} — re-lock with: {FETCH_COMMAND} {} --lock {}",
                    lock.path().display(),
                    entry.file,
                    request.spelling,
                    lock.path().display()
                ),
            });
        }
    }
    release.cross_check(row)?;
    if row.sha256 != entry.sha256 {
        return Err(Error::ArtifactPinned {
            key: key.to_string(),
            message: format!(
                "{} pins {key} -> {} {}, but the release lists {} as {}.{}",
                lock.path().display(),
                entry.file,
                entry.sha256,
                row.file,
                row.sha256,
                no_revision_note(lock, entry, &request.spelling)
            ),
        });
    }
    Ok((row, key.to_string()))
}

/// The one sentence appended to a PINNED or UNPUBLISHED message when, under
/// `--frozen`, the candidate entry exists but names no ABI revision at all —
/// written by an SDK before this field existed (#282's remedy). `""` when
/// the entry does record one.
fn no_revision_note(lock: &LockFile, entry: &LockEntry, spelling: &str) -> String {
    if entry.abi_revision.is_some() {
        return String::new();
    }
    format!(
        " {} records no ABI revision (written by an older SDK); this SDK speaks ABI revision {} \
         — re-lock with: {FETCH_COMMAND} {spelling} --lock {}",
        lock.path().display(),
        fetch_abi_revision(),
        lock.path().display()
    )
}

/// The served golden set: a release-level file like `index.json`, and a row in
/// the signed `SHA256SUMS` like a tarball, so it verifies through the same chain
/// and installs beside the artifacts as `<registry>/sdk-goldens.json` — where
/// every binding's golden test reads it offline.
const GOLDENS_ASSET: &str = "sdk-goldens.json";

/// Install the served golden set, if this release publishes one, and never
/// fail the fetch that called it: a release-level file here is best-effort.
///
/// The first look reuses `release`'s already-verified sums ([`Release::sum_for`]
/// via [`Release::read_goldens`]) — cheap, and right on the overwhelmingly
/// common case that nothing is mid-publish. Only a disagreement is retried
/// (the same publish-window symptom as the signature and index.json:
/// `docs/guides/fetch.md` §3a), and a retry re-reads the WHOLE consistent set
/// fresh through [`Release::load_once`] — `SHA256SUMS`, its signature,
/// `index.json` AND the golden set together — never the golden set alone
/// checked against this call's by-then possibly-stale `release`.
///
/// A release with no such row simply predates the served set, and a mismatch
/// that never heals — or a set that cannot be written — leaves the golden
/// tests skipping loudly, which is their job when there is nothing
/// trustworthy to read.
fn install_goldens(
    source: &Source,
    release: &Release,
    policy: &TrustPolicy,
    dest: &std::path::Path,
    progress: bool,
) {
    let note = |msg: String| {
        if progress {
            eprintln!("chtypes: {msg}");
        }
    };
    let attempts = if matches!(source, Source::Http { .. }) {
        release::RELEASE_LOAD_ATTEMPTS
    } else {
        1
    };
    let mut delay = release::release_retry_delay();
    let mut elapsed = std::time::Duration::ZERO;
    let mut attempt = 1;
    let blob = loop {
        let result = if attempt == 1 {
            release.read_goldens(source, GOLDENS_ASSET)
        } else {
            // A stale first look: re-verify everything from scratch, not just
            // the golden set against sums that may themselves have moved on.
            Release::load_once(source, policy, progress)
                .and_then(|fresh| fresh.read_goldens(source, GOLDENS_ASSET))
        };
        match result {
            Ok(blob) => break blob,
            Err(err) => {
                let (retryable, retry_after) = release::retry_info(&err);
                if attempt >= attempts || !retryable {
                    note(format!("{err} — the golden tests will skip"));
                    return;
                }
                let wait = retry_after.unwrap_or(delay);
                if elapsed + wait > release::retry_budget() {
                    note(format!(
                        "{err} — the source asked to wait {wait:?} before retrying, which would \
                         exceed the {:?} retry budget; the golden tests will skip",
                        release::retry_budget()
                    ));
                    return;
                }
                note(format!(
                    "{err} (attempt {attempt}/{attempts}) — this is what a release being \
                     published (or briefly unreachable) looks like from outside; retrying in \
                     {wait:?}"
                ));
                std::thread::sleep(wait);
                elapsed += wait;
                delay *= 2;
                attempt += 1;
            }
        }
    };
    let Some(blob) = blob else {
        note(format!(
            "this release does not publish {GOLDENS_ASSET} (the SDKs' golden tests will skip \
             until it does)"
        ));
        return;
    };
    let out = dest.join(GOLDENS_ASSET);
    if let Err(e) = std::fs::create_dir_all(dest).and_then(|()| std::fs::write(&out, &blob)) {
        note(format!(
            "could not write {GOLDENS_ASSET}: {e} — the golden tests will skip"
        ));
        return;
    }
    note(format!(
        "golden set verified and installed: {}",
        out.display()
    ));
}

/// [`ensure`] for every line the release publishes for the platform
/// (`chtypes fetch --all`). Always consults the release; each line is then
/// installed or confirmed exactly as [`ensure`] does it. Stops at the first
/// failure.
pub fn ensure_all(opts: &EnsureOptions) -> Result<Vec<Installed>> {
    let platform = platform_of(opts)?;
    let dest = install_dir_for(&platform, opts.dest.as_deref());
    let search = installed_search(opts, &platform, &dest);
    check_lock_revision_early_all(opts, &platform)?;
    let source = Source::resolve(opts.url.as_deref(), opts.tag.as_deref(), opts.offline)?;
    let policy = policy_of(opts)?;
    let release = Release::load(&source, &policy, opts.progress)?;

    if opts.frozen {
        return ensure_all_frozen(opts, &platform, &dest, &source, &policy, &release);
    }

    let rows = release.all(&platform);
    if rows.is_empty() {
        let on_platform: Vec<&IndexRow> = release
            .rows()
            .iter()
            .filter(|r| r.platform() == platform)
            .collect();
        let mut platforms: Vec<String> = release.rows().iter().map(|r| r.platform()).collect();
        platforms.sort();
        platforms.dedup();
        return Err(Error::ArtifactUnpublished {
            requested: "every line".into(),
            platform: platform.clone(),
            origin: release.origin.clone(),
            offered: if !on_platform.is_empty() {
                // The platform has rows, just none at this crate's revision.
                release::unpublished_at_revision(
                    &[],
                    &on_platform,
                    fetch_abi_revision(),
                    &format!("rows for {platform}"),
                )
            } else if platforms.is_empty() {
                "it has nothing.".into()
            } else {
                format!("it has platforms {}.", platforms.join(", "))
            },
        });
    }
    // A line the release has only at another ABI revision is not installed —
    // and never silently: one loud line per such line, then the rest go on.
    for message in release::skipped_lines(release.rows(), &platform, fetch_abi_revision()) {
        eprintln!("chtypes: WARNING: {message}");
    }
    let mut lock = lock_of(opts)?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        row_complete(row)?;
        release.cross_check(row)?;
        // --all always picks the newest patch of each line (`release.all`):
        // a LINE-level selection, so every install here is flat.
        let key = lock_key(&platform, &row.clickhouse_version);
        let found = if opts.force {
            None
        } else {
            locate_in(&search, &row.clickhouse_minor).and_then(|dir| InstalledDir::read(&dir).ok())
        };
        let confirmed = found.and_then(|inst| {
            (inst.manifest.clickhouse_version == row.clickhouse_version
                && inst.manifest.library == row.library)
                .then(|| {
                    inst.verify()
                        .ok()
                        .filter(|sha| *sha == row.library_sha256)
                        .map(|sha| (inst, sha))
                })
                .flatten()
        });
        let installed = match confirmed {
            Some((inst, sha)) => {
                let final_dir =
                    seat_existing(&dest, InstallSlot::Flat, &row.clickhouse_minor, &inst.dir)?;
                let final_inst: InstalledDir = if final_dir == inst.dir {
                    inst
                } else {
                    InstalledDir::read(&final_dir)?
                };
                say(
                    opts,
                    &format!(
                        "already installed and verified: {}",
                        final_inst.library().display()
                    ),
                );
                final_inst.installed(&platform, sha, Some((row.file.clone(), row.sha256.clone())))
            }
            None => install::fetch_and_install(
                &source,
                row,
                &dest,
                &platform,
                InstallSlot::Flat,
                opts.progress,
            )?,
        };
        record(lock.as_mut(), opts, &key, row)?;
        out.push(installed);
    }
    install_goldens(&source, &release, &policy, &dest, opts.progress);
    Ok(out)
}

/// `--all --frozen` (F6): installs the newest PINNED patch of each line the
/// lock pins for `platform` — never a line the release has that the lock
/// does not pin, which gets one progress note instead of an error. Always a
/// flat (LINE-level) install.
fn ensure_all_frozen(
    opts: &EnsureOptions,
    platform: &str,
    dest: &Path,
    source: &Source,
    policy: &TrustPolicy,
    release: &Release,
) -> Result<Vec<Installed>> {
    let lock = lock_of(opts)?.expect("frozen always has a lock");
    let by_line = lock.lines_for(platform);
    if by_line.is_empty() {
        return Err(Error::ArtifactPinned {
            key: format!("{platform}/*"),
            message: format!(
                "{} pins nothing for {platform}; --all --frozen has nothing to install",
                lock.path().display()
            ),
        });
    }
    let pinned_lines: std::collections::BTreeSet<&str> =
        by_line.keys().map(String::as_str).collect();
    for row in release.all(platform) {
        if !pinned_lines.contains(row.clickhouse_minor.as_str()) {
            say(
                opts,
                &format!(
                    "{} is served but not pinned in {}; --all --frozen does not install it",
                    row.clickhouse_minor,
                    lock.path().display()
                ),
            );
        }
    }
    let search = installed_search(opts, platform, dest);
    let mut out = Vec::with_capacity(by_line.len());
    for (minor, (key, _entry)) in &by_line {
        // Delegate to `frozen_row` for the actual F1–F5 checks, rather than
        // re-deriving them: one code path for the revision check, the
        // release lookup, cross_check and the sha comparison, `lines_for`'s
        // grouping having already picked the right candidate.
        let request = Request {
            spelling: minor.clone(),
            minor: minor.clone(),
            exact: None,
        };
        let (row, got_key) = frozen_row(&lock, release, platform, &request)?;
        debug_assert_eq!(&got_key, key);
        row_complete(row)?;
        let found = if opts.force {
            None
        } else {
            locate_in(&search, minor).and_then(|dir| InstalledDir::read(&dir).ok())
        };
        let confirmed = found.and_then(|inst| {
            (inst.manifest.clickhouse_version == row.clickhouse_version
                && inst.manifest.library == row.library)
                .then(|| {
                    inst.verify()
                        .ok()
                        .filter(|sha| *sha == row.library_sha256)
                        .map(|sha| (inst, sha))
                })
                .flatten()
        });
        let installed = match confirmed {
            Some((inst, sha)) => {
                let final_dir = seat_existing(dest, InstallSlot::Flat, minor, &inst.dir)?;
                let final_inst: InstalledDir = if final_dir == inst.dir {
                    inst
                } else {
                    InstalledDir::read(&final_dir)?
                };
                say(
                    opts,
                    &format!(
                        "already installed and verified: {}",
                        final_inst.library().display()
                    ),
                );
                final_inst.installed(platform, sha, Some((row.file.clone(), row.sha256.clone())))
            }
            None => install::fetch_and_install(
                source,
                row,
                dest,
                platform,
                InstallSlot::Flat,
                opts.progress,
            )?,
        };
        out.push(installed);
    }
    // --frozen never writes the lock (F7): `lock` was read only to pick
    // candidates, above.
    install_goldens(source, release, policy, dest, opts.progress);
    Ok(out)
}

/// Re-hash every installed line under `dir` against its own manifest
/// (`chtypes verify`). A directory holding no line answers an empty list —
/// the caller says so; an empty answer must never read as "all verified".
pub fn verify_installed(dir: &Path) -> Vec<Verification> {
    let mut out = Vec::new();
    for patch in
        crate::legacy::registry::installed_patches(std::slice::from_ref(&dir.to_path_buf()))
    {
        let sub = patch.dir;
        let line = patch.line;
        let inst = match InstalledDir::read(&sub) {
            Ok(inst) => inst,
            Err(e) => {
                out.push(Verification {
                    dir: sub.clone(),
                    line,
                    version: String::new(),
                    library: sub.join("manifest.json"),
                    expected: String::new(),
                    result: Err(e),
                });
                continue;
            }
        };
        out.push(Verification {
            dir: sub.clone(),
            line,
            version: inst.manifest.clickhouse_version.clone(),
            library: inst.library(),
            expected: inst.manifest.library_sha256.clone(),
            result: inst.verify(),
        });
    }
    out
}

/// What the release offers (`chtypes list`): read through §3 steps 0–1, so
/// an untrusted listing is refused exactly as a fetch would refuse it.
pub fn release_info(opts: &EnsureOptions) -> Result<ReleaseInfo> {
    let source = Source::resolve(opts.url.as_deref(), opts.tag.as_deref(), opts.offline)?;
    let policy = policy_of(opts)?;
    let release = Release::load(&source, &policy, opts.progress)?;
    Ok(ReleaseInfo {
        origin: release.origin.clone(),
        signed_by: release.signed_by.clone(),
        license: release.license().0.to_string(),
        artifacts: release.rows().to_vec(),
    })
}

/// Check a line spelling the way [`ensure`] will read it — a minor line
/// (`25.8`) or an exact patch (`v25.8.28.1-lts`) — without fetching anything.
/// Returns the minor line.
///
/// # Errors
///
/// [`Error::Fetch`] when the spelling is not a ClickHouse version.
pub fn parse_line(line: &str) -> Result<String> {
    Request::parse(line).map(|r| r.minor)
}

/// The registry directory a fetch with these options writes to
/// (`chtypes where`).
pub fn install_dir(opts: &EnsureOptions) -> Result<PathBuf> {
    let platform = platform_of(opts)?;
    Ok(install_dir_for(&platform, opts.dest.as_deref()))
}

/// The §1 search path a fetch with these options looks in.
pub fn search_path(opts: &EnsureOptions) -> Result<Vec<PathBuf>> {
    let platform = platform_of(opts)?;
    Ok(search_path_for(&platform, opts.dest.as_deref()))
}

/// Where "already installed" is looked for. Without an explicit `dest`, the
/// whole §1 search path: a line a registry would find — in the per-user
/// cache, or pre-seeded in a system location — is installed, and fetching a
/// second copy into the cache would only spend 250 MB. With an explicit
/// `dest` the caller named where the line must be, so only `dest` counts: a
/// container build's `--dest /opt/chtypes/artifacts` must not be satisfied
/// by the builder's own cache.
fn installed_search(opts: &EnsureOptions, platform: &str, dest: &Path) -> Vec<PathBuf> {
    match opts.dest {
        Some(_) => vec![dest.to_path_buf()],
        None => search_path_for(platform, None),
    }
}

fn platform_of(opts: &EnsureOptions) -> Result<String> {
    let platform = opts.platform.clone().unwrap_or_else(host_platform);
    match platform.as_str() {
        "linux-arm64" | "linux-amd64" | "darwin-arm64" | "darwin-amd64" => Ok(platform),
        other => Err(Error::Fetch {
            message: format!("not a known platform key: {other} ((linux|darwin)-(arm64|amd64))"),
        }),
    }
}

fn policy_of(opts: &EnsureOptions) -> Result<TrustPolicy> {
    match (&opts.trusted_keys, opts.allow_unsigned) {
        (None, None) => TrustPolicy::from_env(),
        (keys, allow) => {
            let env = TrustPolicy::from_env()?;
            let keys_hex: Option<Vec<String>> = match keys {
                Some(k) => Some(k.clone()),
                None => std::env::var(TRUSTED_KEYS_ENV).ok().map(|list| {
                    list.split(',')
                        .map(str::trim)
                        .filter(|s| !s.is_empty())
                        .map(String::from)
                        .collect()
                }),
            };
            TrustPolicy::new(keys_hex.as_deref(), allow.unwrap_or(env.allow_unsigned()))
        }
    }
}

fn lock_of(opts: &EnsureOptions) -> Result<Option<LockFile>> {
    match (&opts.lock, opts.frozen) {
        (None, false) => Ok(None),
        (Some(path), frozen) => LockFile::load(path, frozen).map(Some),
        (None, true) => LockFile::load(Path::new(DEFAULT_LOCK_FILE), true).map(Some),
    }
}

fn record(
    lock: Option<&mut LockFile>,
    opts: &EnsureOptions,
    key: &str,
    row: &IndexRow,
) -> Result<()> {
    if let Some(lock) = lock {
        if !opts.frozen && lock.record(key, row) {
            lock.save()?;
            say(
                opts,
                &format!(
                    "pinned {key} -> {} (ABI revision {}) in {}",
                    row.file,
                    row.abi_revision.unwrap_or_else(fetch_abi_revision),
                    lock.path().display()
                ),
            );
        }
    }
    Ok(())
}

/// Fails fast, before any network access, when `frozen`'s lock already pins
/// a candidate for `request` at an ABI revision that is not this crate's own
/// (`docs/guides/fetch.md` §5, F2). A lock made for one ABI revision does not
/// get a second chance disguised as a drifted pin or an unpublished patch
/// once the crate moves to another: the fix is always the same re-lock, so
/// the message says that directly instead of waiting to see which of the
/// two symptoms [`frozen_row`] would have produced.
fn check_lock_revision_early(
    opts: &EnsureOptions,
    platform: &str,
    request: &Request,
) -> Result<()> {
    if !opts.frozen {
        return Ok(());
    }
    let path = opts
        .lock
        .clone()
        .unwrap_or_else(|| PathBuf::from(DEFAULT_LOCK_FILE));
    // A lock that cannot be read at all raises its own error later, from the
    // authoritative [`lock_of`]; here, any read failure reads as "nothing to
    // check early" and falls through to that later, better-placed error.
    let Ok(lock) = LockFile::load(&path, false) else {
        return Ok(());
    };
    let rev = fetch_abi_revision();
    for (key, entry) in lock.candidates(platform, request) {
        if let Some(pinned_rev) = entry.abi_revision {
            if pinned_rev != rev {
                return Err(Error::ArtifactPinned {
                    key: key.to_string(),
                    message: format!(
                        "{} pins {key} at ABI revision {pinned_rev}; this SDK speaks ABI \
                         revision {rev} — re-lock with: {FETCH_COMMAND} {} --lock {}",
                        path.display(),
                        request.spelling,
                        path.display()
                    ),
                });
            }
        }
    }
    Ok(())
}

/// [`check_lock_revision_early`] for `--all --frozen` (F2: "every candidate
/// is checked before anything is read") — one candidate per line, the newest
/// patch [`LockFile::lines_for`] would pin.
fn check_lock_revision_early_all(opts: &EnsureOptions, platform: &str) -> Result<()> {
    if !opts.frozen {
        return Ok(());
    }
    let path = opts
        .lock
        .clone()
        .unwrap_or_else(|| PathBuf::from(DEFAULT_LOCK_FILE));
    let Ok(lock) = LockFile::load(&path, false) else {
        return Ok(());
    };
    let rev = fetch_abi_revision();
    for (minor, (key, entry)) in lock.lines_for(platform) {
        if let Some(pinned_rev) = entry.abi_revision {
            if pinned_rev != rev {
                return Err(Error::ArtifactPinned {
                    key: key.clone(),
                    message: format!(
                        "{} pins {key} at ABI revision {pinned_rev}; this SDK speaks ABI \
                         revision {rev} — re-lock with: {FETCH_COMMAND} {minor} --lock {}",
                        path.display(),
                        path.display()
                    ),
                });
            }
        }
    }
    Ok(())
}

fn row_complete(row: &IndexRow) -> Result<()> {
    if row.file.is_empty()
        || row.sha256.is_empty()
        || row.library.is_empty()
        || row.library_sha256.is_empty()
    {
        return Err(Error::Fetch {
            message: format!("index.json entry for {:?} is incomplete", row.file),
        });
    }
    Ok(())
}

fn say(opts: &EnsureOptions, message: &str) {
    if opts.progress {
        eprintln!("chtypes: {message}");
    }
}

/// An installed patch directory — the flat `<registry>/<minor>` slot, or a
/// `patches/<minor>/<version>` sibling — and its manifest.
#[derive(Clone)]
struct InstalledDir {
    dir: PathBuf,
    manifest: Manifest,
}

impl InstalledDir {
    fn read(dir: &Path) -> Result<InstalledDir> {
        let path = dir.join("manifest.json");
        let text = std::fs::read_to_string(&path).map_err(|e| Error::Fetch {
            message: format!("{}: {e}", path.display()),
        })?;
        let manifest: Manifest = serde_json::from_str(&text).map_err(|e| Error::Fetch {
            message: format!("{}: {e}", path.display()),
        })?;
        if manifest.library.is_empty() {
            return Err(Error::Fetch {
                message: format!("{} names no library", path.display()),
            });
        }
        Ok(InstalledDir {
            dir: dir.to_path_buf(),
            manifest,
        })
    }

    fn library(&self) -> PathBuf {
        self.dir.join(&self.manifest.library)
    }

    /// Hash the library in place against the manifest's own claim.
    fn verify(&self) -> Result<String> {
        let library = self.library();
        if self.manifest.library_sha256.is_empty() {
            return Err(Error::Fetch {
                message: format!(
                    "{} records no library_sha256",
                    self.dir.join("manifest.json").display()
                ),
            });
        }
        let actual = sha256_file(&library).map_err(|e| Error::Fetch {
            message: format!("{}: {e}", library.display()),
        })?;
        if actual != self.manifest.library_sha256 {
            return Err(Error::ArtifactCorrupt {
                subject: library.display().to_string(),
                expected: self.manifest.library_sha256.clone(),
                actual,
            });
        }
        Ok(actual)
    }

    fn installed(&self, platform: &str, sha: String, asset: Option<(String, String)>) -> Installed {
        let line = if self.manifest.clickhouse_minor.is_empty() {
            minor_of(&self.manifest.clickhouse_version)
        } else {
            self.manifest.clickhouse_minor.clone()
        };
        Installed {
            dir: self.dir.clone(),
            line,
            version: self.manifest.clickhouse_version.clone(),
            library: self.library(),
            library_sha256: sha,
            platform: platform.to_string(),
            action: Action::AlreadyInstalled,
            asset,
        }
    }
}
