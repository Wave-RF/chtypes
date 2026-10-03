//! The v0 artifact directory helpers (`manifest.json`, the search path, the
//! installed-patch scan) that the v0 `fetch` module and the `chtypes` binary
//! still use. Quarantined here, with `legacy::error`, when the v0 `Registry` and
//! `Library` were deleted; the switch lane removes this module together with
//! `fetch/` and the binary.

//! The artifact registry: a directory of one subdirectory per ClickHouse minor
//! line, each holding a `manifest.json` and the shared library it names —
//! plus, since #284, every OTHER installed patch of that line, in a
//! `patches/<minor>/<clickhouse_version>/` sibling (docs/guides/fetch.md, THE
//! LAYOUT RULE). The flat `<minor>/` slot is always what a LINE request
//! loads; `patches/` holds everything else. Both are read; only the flat slot
//! is written by a released (pre-#284) SDK sharing the same cache.

use std::path::{Path, PathBuf};

use serde::Deserialize;

/// The minor line of a reported version: the first two dot-separated components.
pub(crate) fn minor_of(version: &str) -> String {
    let mut it = version.splitn(3, '.');
    match (it.next(), it.next()) {
        (Some(a), Some(b)) => format!("{a}.{b}"),
        _ => version.to_string(),
    }
}

/// `(major, minor)` for ordering minor lines numerically.
pub(crate) fn minor_order(minor: &str) -> (u64, u64) {
    let mut it = minor.split('.');
    let major = it.next().and_then(|s| s.parse().ok()).unwrap_or(0);
    let line = it.next().and_then(|s| s.parse().ok()).unwrap_or(0);
    (major, line)
}

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
/// [`crate::fetch::ensure`] first (`docs/guides/fetch.md` §6). Off by default, because a
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
/// crate's [`crate::legacy::error::ABI_REVISION`] (`docs/guides/fetch.md` §1 item 3). Where fetch
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
        .join(format!("abi{}", crate::legacy::error::ABI_REVISION))
        .join(platform)
}

/// The per-user artifact cache for this host —
/// `${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/abi<R>/<os>-<arch>`, R this
/// crate's [`crate::legacy::error::ABI_REVISION`] and `<arch>` spelled the artifact way
/// (`amd64`, `arm64`). Where `scripts/fetch.sh` and [`crate::fetch::ensure`] install,
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
