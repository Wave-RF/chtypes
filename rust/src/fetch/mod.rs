//! Fetching, verifying and installing artifacts — `docs/fetch.md`, the
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

pub use lock::{DEFAULT_LOCK_FILE, LockEntry, LockFile, lock_key};
pub use release::IndexRow;
pub use source::{ARTIFACTS_URL_ENV, DEFAULT_ARTIFACTS_URL, DEFAULT_TAG, DOWNLOAD_TOKEN_ENV};
pub use trust::{
    ALLOW_UNSIGNED_ENV, RELEASE_KEY_ID, RELEASE_PUBLIC_KEY, RELEASE_PUBLIC_KEY_HEX,
    TRUSTED_KEYS_ENV, TrustPolicy, key_id, parse_key_hex, parse_signature_file, sha256_file,
    sha256_hex, verify_signature,
};

use crate::error::{Error, Result};
pub(crate) use crate::library::minor_of;
use crate::registry::{Manifest, host_platform, install_dir_for, locate_in, search_path_for};
use release::{Release, Request};
use source::Source;

/// How [`ensure`] fetches. `Default` is what `chtypes fetch <line>` does with
/// no flags: this host's platform, the artifacts host's rolling release, the
/// environment's trust policy, no lock, quiet.
#[derive(Debug, Clone, Default)]
pub struct EnsureOptions {
    /// An explicit registry directory (`--dest`; `docs/fetch.md` §1 item 1).
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
    /// The artifacts' licence, as the listing names it.
    pub license: String,
    /// Every published artifact, as listed.
    pub artifacts: Vec<IndexRow>,
}

/// Make ClickHouse `line` (`25.8`, or an exact patch `25.8.28.1-lts`)
/// installed and verified for this host, fetching it through the
/// `docs/fetch.md` §3 chain when it is not.
///
/// Idempotent: a line already installed whose library hashes what the signed
/// release lists is [`Action::AlreadyInstalled`] and nothing is downloaded —
/// the release's three small files are read, the tarball is not. Only
/// [`EnsureOptions::offline`] reads no source: then an installed line that
/// hashes what its own manifest says is the answer, and nothing else is
/// (`docs/fetch.md`, Decisions). "Installed" means anywhere on the §1 search
/// path when no [`EnsureOptions::dest`] is given (what a registry would
/// find), and in `dest` itself when one is (the caller named where the line
/// must be). Otherwise the asset for the line is downloaded, hashed, unpacked
/// into a temporary sibling, renamed into `<dest>/<minor>/` and re-hashed in
/// place. With a lock file ([`EnsureOptions::lock`]) the pin is recorded or,
/// under `frozen`, enforced.
///
/// The directory it returns is what [`crate::Registry::new`] loads.
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

    // `--offline` is the no-source path (§6): an installed line whose library
    // hashes what its own manifest says is the answer, and nothing else is.
    // Otherwise the signed release is read first — SHA256SUMS, its signature,
    // index.json; never the tarball — and "installed" means hashing what that
    // listing says (§3), the same in all four SDKs (docs/fetch.md, Decisions).
    let found = locate_in(&search, &request.minor).and_then(|dir| InstalledDir::read(&dir).ok());
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

    let source = Source::resolve(opts.url.as_deref(), opts.tag.as_deref(), opts.offline)?;
    let policy = policy_of(opts)?;
    let release = Release::load(&source, &policy, opts.progress)?;
    let row = release.select(&request, &platform)?;
    let key = lock_key(&platform, &row.clickhouse_minor);
    let mut lock = lock_of(opts)?;
    if opts.frozen {
        lock.as_ref()
            .expect("frozen always has a lock")
            .enforce(&key, row)?;
    }
    say(
        opts,
        &format!(
            "ClickHouse {} -> line {} ({}), {platform}, from {}",
            request.spelling, row.clickhouse_minor, row.clickhouse_version, release.origin
        ),
    );

    // Installed and hashing what the release says: nothing to download.
    if !opts.force {
        if let Some(inst) = &found {
            if inst.manifest.clickhouse_version == row.clickhouse_version
                && inst.manifest.library == row.library
            {
                if let Ok(sha) = inst.verify() {
                    if sha == row.library_sha256 {
                        say(
                            opts,
                            &format!(
                                "already installed and verified: {}",
                                inst.library().display()
                            ),
                        );
                        let installed = inst.installed(
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

    let installed = install::fetch_and_install(&source, row, &dest, &platform, opts.progress)?;
    record(lock.as_mut(), opts, &key, row)?;
    install_goldens(&source, &release, &dest, opts.progress);
    Ok(installed)
}

/// The served golden set: a release-level file like `index.json`, and a row in
/// the signed `SHA256SUMS` like a tarball, so it verifies through the same chain
/// and installs beside the artifacts as `<registry>/sdk-goldens.json` — where
/// every binding's golden test reads it offline.
const GOLDENS_ASSET: &str = "sdk-goldens.json";

/// Install the served golden set, if this release publishes one.
///
/// Never fails a fetch. A release with no such row simply predates the served
/// set, and a set that cannot be written leaves the golden tests skipping
/// loudly, which is their job when there is nothing to read. What it will not do
/// is install bytes the signed `SHA256SUMS` does not describe.
fn install_goldens(source: &Source, release: &Release, dest: &std::path::Path, progress: bool) {
    let note = |msg: String| {
        if progress {
            eprintln!("chtypes: {msg}");
        }
    };
    let Some(want) = release.sum_for(GOLDENS_ASSET) else {
        note(format!(
            "this release does not publish {GOLDENS_ASSET} (the SDKs' golden tests will skip \
             until it does)"
        ));
        return;
    };
    let blob = match source.read(GOLDENS_ASSET) {
        Ok(Some(b)) => b,
        Ok(None) => {
            note(format!(
                "SHA256SUMS lists {GOLDENS_ASSET} but {} does not serve it — NOT installing it",
                source.describe()
            ));
            return;
        }
        Err(e) => {
            note(format!(
                "could not read {GOLDENS_ASSET}: {e} — the golden tests will skip"
            ));
            return;
        }
    };
    let got = super::fetch::trust::sha256_hex(&blob);
    if got != want {
        note(format!(
            "NOT installing {GOLDENS_ASSET}: it hashes to {got} but the signed SHA256SUMS says \
             {want}"
        ));
        return;
    }
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
    let source = Source::resolve(opts.url.as_deref(), opts.tag.as_deref(), opts.offline)?;
    let policy = policy_of(opts)?;
    let release = Release::load(&source, &policy, opts.progress)?;
    let rows = release.all(&platform);
    if rows.is_empty() {
        let mut platforms: Vec<String> = release.rows().iter().map(|r| r.platform()).collect();
        platforms.sort();
        platforms.dedup();
        return Err(Error::ArtifactUnpublished {
            requested: "every line".into(),
            platform: platform.clone(),
            origin: release.origin.clone(),
            offered: if platforms.is_empty() {
                "nothing".into()
            } else {
                format!("platforms {}", platforms.join(", "))
            },
        });
    }
    let mut lock = lock_of(opts)?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        row_complete(row)?;
        release.cross_check(row)?;
        let key = lock_key(&platform, &row.clickhouse_minor);
        if opts.frozen {
            lock.as_ref()
                .expect("frozen always has a lock")
                .enforce(&key, row)?;
        }
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
                say(
                    opts,
                    &format!(
                        "already installed and verified: {}",
                        inst.library().display()
                    ),
                );
                inst.installed(&platform, sha, Some((row.file.clone(), row.sha256.clone())))
            }
            None => install::fetch_and_install(&source, row, &dest, &platform, opts.progress)?,
        };
        record(lock.as_mut(), opts, &key, row)?;
        out.push(installed);
    }
    install_goldens(&source, &release, &dest, opts.progress);
    Ok(out)
}

/// Re-hash every installed line under `dir` against its own manifest
/// (`chtypes verify`). A directory holding no line answers an empty list —
/// the caller says so; an empty answer must never read as "all verified".
pub fn verify_installed(dir: &Path) -> Vec<Verification> {
    let mut out = Vec::new();
    for (line, sub) in crate::registry::installed_lines(std::slice::from_ref(&dir.to_path_buf())) {
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
            say(opts, &format!("pinned {key} in {}", lock.path().display()));
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

/// An installed `<registry>/<minor>` and its manifest.
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
