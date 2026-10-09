//! The fetch contract this build speaks. A 2.0.0-dev SDK speaks the ABI v2 dev
//! channel (spec/abi-v2/docs.md, rules r5 and r6), which is the v1 fetch
//! contract (docs/guides/fetch-v1.md) narrowed in five ways:
//!
//! * it fetches ONLY from [`DEV_CHANNEL_BASE`] and trusts ONLY the staging key
//!   ([`DEV_KEY_HEX`], id [`DEV_KEY_ID`]); the release key is not in its trust
//!   list;
//! * it has no override: `CHTYPES_ARTIFACTS_URL`, `CHTYPES_TRUSTED_KEYS` and
//!   `CHTYPES_ALLOW_UNSIGNED`, and the options that set a base, a trust list or
//!   an unsigned fetch, are ignored, each with one loud warning per process;
//! * it refuses `--lock`, `--frozen` and `--update`, and their options, before
//!   any network call: a dev build is replaceable and a superseded one expires,
//!   so nothing may pin one;
//! * its cache is one no released 1.x reader ever reads: `verified.json`
//!   records are schema 2, the default root is
//!   `${XDG_CACHE_HOME:-~/.cache}/chtypes/v2-dev`, and an explicit cache
//!   (`CHTYPES_CACHE`, `--cache`, the `cache_dir` option) is used through its
//!   subroot `<cache>/v2-dev`, never as a whole layout;
//! * a signed predicate must say abi 2.
//!
//! It also resolves one tag the v1 contract never asks for: a version request
//! fetches `<tag>--fp-<its own fingerprint>` first, the newest dev build of the
//! ABI this crate speaks, and falls back to `<tag>` only when no base has that
//! alias (docs/guides/fetch-v1.md §3, "The dev channel's alias step"), so a dev
//! build of a newer fingerprint never strands this one. Its cache lookups
//! (`resolve_installed`, and `ensure`'s offline answer and its monotonic rule)
//! see only the records whose signed `abi_fingerprint` is its own: a build of
//! another fingerprint in a shared cache is never returned and never kept over
//! this one's own, and it is never removed or rewritten either. It is
//! automatic, with no override; the trust checks are unchanged, and a listing
//! never shows an alias.
//! The fingerprint is the generated [`DEV_ABI_FINGERPRINT`], never a
//! hand-written copy.
//!
//! The v1 contract itself stays in this module tree, unchanged, because the
//! fetch-v1 conformance cases (tests/fixtures/fetch-v1) are its specification
//! and the dev channel shares every other rule with it. Only a test build
//! reaches it: [`use_fetch_v1_for_tests`] and its siblings exist only under
//! `cfg(test)` (a unit test, or a `rust/tests/` suite that recompiles this
//! module with `#[path]`), so neither is an override a user can reach. The
//! seam is per THREAD: libtest runs each test on its own thread, so one test's
//! contract never leaks into another's, and every call a test makes on its
//! own thread sees it.

use std::collections::BTreeSet;
use std::sync::Mutex;

use super::abi_fingerprint::DEV_ABI_FINGERPRINT;
use super::constants;
use super::ensure::Options;
use super::error::Error;
use super::oci::VersionRequest;

/// The only base a 2.0.0-dev SDK fetches from (rule r6).
pub const DEV_CHANNEL_BASE: &str = "https://registry-staging.wavehouse.dev/chtypes/v2-dev";
/// The staging key's id (`constants::KEYID_ALGORITHM` over [`DEV_KEY_HEX`]).
pub const DEV_KEY_ID: &str = "824345f9bcf8e5bf";
/// The staging key, the only key a 2.0.0-dev SDK trusts: an ed25519 public
/// key, raw 32 bytes as lowercase hex.
pub const DEV_KEY_HEX: &str = "5cd30c53c65a1ebc2d85836a41deb06661bb0ae7b658adb9eb116ec2db8e9b1c";
/// The dev channel's cache directory name: the default root's last element
/// and an explicit cache's subroot (rule r5).
pub const DEV_CACHE_DIR: &str = "v2-dev";
/// The `verified.json` schema the dev channel writes, and the only one it
/// reads (rule r5).
pub const DEV_RECORD_SCHEMA: u64 = 2;
/// The `abi` a dev predicate must carry.
pub const DEV_ABI_GENERATION: u32 = 2;

/// Joins a tag and a fingerprint into the dev channel's alias tag:
/// `<tag>--fp-<64 lowercase hex>` (docs/guides/fetch-v1.md §3).
pub const ALIAS_SEPARATOR: &str = "--fp-";

/// What every lock, frozen or update request gets from a 2.0.0-dev SDK,
/// before any network call (rule r6).
pub const PINNING_REFUSED: &str = "--lock, --frozen and --update are refused by a 2.0.0-dev SDK: a dev build is replaceable, \
and a superseded one expires after 14 days, so nothing may pin one (spec/abi-v2/docs.md, rule r6)";

/// One fetch contract.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Channel {
    /// `v2-dev`, or `v1` (test builds only).
    pub name: &'static str,
    /// A predicate's `abi`, a lock's `abi`, and `Resolved::abi_generation`.
    pub abi: u32,
    /// The `verified.json` schema written, and the only one read.
    pub record_schema: u64,
    /// The default root is `${XDG_CACHE_HOME:-~/.cache}/chtypes/<root_leaf>`.
    pub root_leaf: &'static str,
    /// An explicit cache is used as `<cache>/<subroot>`; `None` uses it whole.
    pub subroot: Option<&'static str>,
    /// The read-only system cache directories searched after the cache.
    pub system_dirs: &'static [&'static str],
    /// The default bases.
    pub bases: &'static [&'static str],
    /// The default trust list: (key id, raw ed25519 key as hex).
    pub keys: &'static [(&'static str, &'static str)],
    /// Whether the base, trust and unsigned overrides are honored.
    pub overridable: bool,
    /// Whether lock, frozen and update are honored.
    pub pinnable: bool,
    /// The fingerprint this contract's SDK speaks (64 lowercase hex, a
    /// `sha256:` prefix ignored): a version request resolves its alias tag
    /// before the tag itself, and the cache lookups see only records signed
    /// with it. `None` does neither (the v1 contract and every production
    /// channel).
    pub own_fingerprint: Option<&'static str>,
}

/// What a 2.0.0-dev SDK speaks, and what every non-test build of this crate
/// speaks.
pub const DEV_CHANNEL: Channel = Channel {
    name: "v2-dev",
    abi: DEV_ABI_GENERATION,
    record_schema: DEV_RECORD_SCHEMA,
    root_leaf: DEV_CACHE_DIR,
    subroot: Some(DEV_CACHE_DIR),
    system_dirs: &["/usr/local/share/chtypes/v2-dev", "/opt/chtypes/v2-dev"],
    bases: &[DEV_CHANNEL_BASE],
    keys: &[(DEV_KEY_ID, DEV_KEY_HEX)],
    overridable: false,
    pinnable: false,
    // The generated constant (abi_fingerprint.rs), never a hand-written one.
    own_fingerprint: Some(DEV_ABI_FINGERPRINT),
};

/// The v1 contract the fetch-v1 conformance cases specify, from the generated
/// constants. Only [`use_fetch_v1_for_tests`] selects it.
#[cfg(test)]
const FETCH_V1_CHANNEL: Channel = Channel {
    name: "v1",
    abi: constants::ABI_GENERATION,
    record_schema: constants::SCHEMA_VERSION as u64,
    root_leaf: "v1",
    subroot: None,
    system_dirs: constants::SYSTEM_CACHE_DIRS,
    bases: constants::DEFAULT_BASES,
    keys: &[(
        constants::RELEASE_KEYS[0].keyid,
        constants::RELEASE_KEYS[0].ed25519_hex,
    )],
    overridable: true,
    pinnable: true,
    own_fingerprint: None,
};

#[cfg(test)]
thread_local! {
    static TEST_CHANNEL: std::cell::Cell<Option<Channel>> = const { std::cell::Cell::new(None) };
    static TEST_WARNINGS: std::cell::RefCell<Option<(BTreeSet<String>, String)>> =
        const { std::cell::RefCell::new(None) };
}

/// The contract this thread fetches under: the dev channel, unless a test
/// selected another.
pub fn active() -> Channel {
    test_channel().unwrap_or(DEV_CHANNEL)
}

#[cfg(test)]
fn test_channel() -> Option<Channel> {
    TEST_CHANNEL.with(std::cell::Cell::get)
}

#[cfg(not(test))]
fn test_channel() -> Option<Channel> {
    None
}

/// Restores the contract a test seam replaced, when dropped.
#[cfg(test)]
#[must_use = "the contract is restored when this guard drops"]
pub struct ChannelGuard(Option<Channel>);

#[cfg(test)]
impl Drop for ChannelGuard {
    fn drop(&mut self) {
        TEST_CHANNEL.with(|c| c.set(self.0));
    }
}

#[cfg(test)]
fn use_channel(c: Channel) -> ChannelGuard {
    ChannelGuard(TEST_CHANNEL.with(|cell| cell.replace(Some(c))))
}

/// Make this TEST thread speak the v1 fetch contract (docs/guides/fetch-v1.md):
/// overrides, locks, schema-1 records, abi-1 predicates and the v1 cache root.
/// The fetch-v1 conformance cases and the v1-protocol unit tests run under it;
/// every case names its own fixture registry and the test key.
#[cfg(test)]
pub fn use_fetch_v1_for_tests() -> ChannelGuard {
    use_channel(FETCH_V1_CHANNEL)
}

/// Make this TEST thread's dev channel honor the base, trust and unsigned
/// overrides, so a test can reach a fixture registry signed with the test key;
/// everything else stays the dev channel's (abi 2, schema-2 records, the v2-dev
/// cache, no pinning).
#[cfg(test)]
pub fn allow_overrides_for_tests() -> ChannelGuard {
    use_channel(Channel {
        overridable: true,
        ..DEV_CHANNEL
    })
}

/// Make this TEST thread's active contract speak `fingerprint` (64 lowercase
/// hex) as the dev channel speaks its own: a version request resolves that
/// fingerprint's alias tag first, and the cache lookups see only records
/// signed with it. The fetch-v1 conformance cases that carry
/// `request.own_fingerprint` run under it, against fixtures that name a
/// fixture fingerprint. Everything else stays the active contract's. Panics on
/// a fingerprint that is not 64 lowercase hex.
#[cfg(test)]
pub fn use_own_fingerprint_for_tests(fingerprint: &str) -> ChannelGuard {
    assert!(
        fingerprint.len() == 64
            && fingerprint
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        "use_own_fingerprint_for_tests: {fingerprint:?} is not 64 lowercase hex"
    );
    // A test-only seam: the leaked copy lives as long as the process, as the
    // `&'static str` every other channel field is.
    let fingerprint: &'static str = Box::leak(fingerprint.to_string().into_boxed_str());
    use_channel(Channel {
        own_fingerprint: Some(fingerprint),
        ..active()
    })
}

/// The alias a version request resolves first under the active contract, or
/// `None` when it resolves its tag alone: under the v1 contract and every
/// production channel, and for a request that is not a version spelling (an
/// arbitrary tag has no alias).
pub fn alias_tag(request: &VersionRequest) -> Option<String> {
    let fingerprint = active().own_fingerprint?;
    if request.is_literal() {
        return None;
    }
    let hex = fingerprint.strip_prefix("sha256:").unwrap_or(fingerprint);
    Some(format!("{}{ALIAS_SEPARATOR}{hex}", request.tag()))
}

/// Whether a cache record (or a pre-seeded entry) whose SIGNED predicate is
/// `predicate` may answer a request under the active contract: under one with
/// an own fingerprint (the dev channel), only when the predicate's
/// `abi_fingerprint` is that fingerprint; under every other, always. A record
/// it cannot see is never returned and never kept by the monotonic rule, and
/// nothing removes or rewrites it: another SDK of another fingerprint owns it.
pub fn visible(predicate: &serde_json::Value) -> bool {
    let Some(fingerprint) = active().own_fingerprint else {
        return true;
    };
    let hex = fingerprint.strip_prefix("sha256:").unwrap_or(fingerprint);
    let own = format!("sha256:{hex}");
    predicate["abi_fingerprint"].as_str() == Some(own.as_str())
}

/// The dev channel's answer for an SDK whose fingerprint no published build
/// carries yet (docs/guides/fetch-v1.md §3; public issue #578): when every base
/// answered its own alias 404 (`alias_absent`) and the build the tag names is
/// signed for another fingerprint (`predicate`, the SIGNED predicate, the field
/// [`visible`] keys on), it is `Error::ArtifactUnpublished`, the code a request
/// no build answers gets, naming both fingerprints. Nothing is installed and an
/// install of that build is never reported, because the lookup this SDK opens
/// through would refuse it. `Ok` in every other case: the alias answered, the
/// tag's build is this SDK's own, or the contract has no own fingerprint.
pub fn ahead_of_registry(alias_absent: bool, predicate: &serde_json::Value) -> Result<(), Error> {
    if !alias_absent || visible(predicate) {
        return Ok(());
    }
    let Some(fingerprint) = active().own_fingerprint else {
        return Ok(());
    };
    let own = fingerprint.strip_prefix("sha256:").unwrap_or(fingerprint);
    Err(Error::ArtifactUnpublished(ahead_message(own, predicate)))
}

/// [`ahead_of_registry`]'s message: "no published build for this SDK's
/// fingerprint <own>; newest published on this channel is <fp> (build <id>)",
/// each fingerprint 64 lowercase hex without its `sha256:` prefix, the
/// parenthesis dropped when the predicate names no build, and "unnamed" for a
/// predicate that names no fingerprint.
fn ahead_message(own: &str, predicate: &serde_json::Value) -> String {
    let named = predicate["abi_fingerprint"].as_str().unwrap_or("");
    let theirs = named.strip_prefix("sha256:").unwrap_or(named);
    let theirs = if theirs.is_empty() { "unnamed" } else { theirs };
    let message = format!(
        "no published build for this SDK's fingerprint {own}; newest published on this channel is {theirs}"
    );
    match predicate["build"].as_str().filter(|b| !b.is_empty()) {
        Some(build) => format!("{message} (build {build})"),
        None => message,
    }
}

/// Make this TEST thread speak the dev channel exactly as a non-test build does
/// (undoing either seam above, for a test of the dev channel itself).
#[cfg(test)]
pub fn use_dev_channel_for_tests() -> ChannelGuard {
    use_channel(DEV_CHANNEL)
}

/// Collect this TEST thread's ignored-setting warnings instead of printing
/// them, starting from none warned: [`warned_for_tests`] and
/// [`warning_text_for_tests`] read them back.
#[cfg(test)]
pub fn capture_warnings_for_tests() {
    TEST_WARNINGS.with(|w| *w.borrow_mut() = Some((BTreeSet::new(), String::new())));
}

/// The settings warned about on this TEST thread since
/// [`capture_warnings_for_tests`], sorted.
#[cfg(test)]
pub fn warned_for_tests() -> Vec<String> {
    TEST_WARNINGS.with(|w| {
        w.borrow()
            .as_ref()
            .map(|(set, _)| set.iter().cloned().collect())
            .unwrap_or_default()
    })
}

/// Every warning text printed on this TEST thread since
/// [`capture_warnings_for_tests`].
#[cfg(test)]
pub fn warning_text_for_tests() -> String {
    TEST_WARNINGS.with(|w| {
        w.borrow()
            .as_ref()
            .map(|(_, text)| text.clone())
            .unwrap_or_default()
    })
}

/// The settings already warned about in this process.
static WARNED: Mutex<BTreeSet<String>> = Mutex::new(BTreeSet::new());

/// The one warning for an ignored setting (rule r6: "a dev SDK says so, once
/// and loudly").
pub fn warning_text(setting: &str) -> String {
    format!(
        "chtypes: WARNING: {setting} is set and IGNORED: this is a 2.0.0-dev SDK, which fetches only from \
         {DEV_CHANNEL_BASE} and trusts only the staging key {DEV_KEY_ID} (spec/abi-v2/docs.md, rule r6)"
    )
}

/// Warn, once per process per setting, that a dev SDK ignores `setting`.
pub fn warn_ignored(setting: &str) {
    if captured(setting) {
        return;
    }
    let mut warned = WARNED.lock().unwrap_or_else(|e| e.into_inner());
    if warned.insert(setting.to_string()) {
        eprintln!("{}", warning_text(setting));
    }
}

/// Under [`capture_warnings_for_tests`], record the warning on this TEST
/// thread instead of printing it.
#[cfg(test)]
fn captured(setting: &str) -> bool {
    TEST_WARNINGS.with(|w| match w.borrow_mut().as_mut() {
        Some((set, text)) => {
            if set.insert(setting.to_string()) {
                text.push_str(&warning_text(setting));
                text.push('\n');
            }
            true
        }
        None => false,
    })
}

#[cfg(not(test))]
fn captured(_setting: &str) -> bool {
    false
}

/// Under a contract with no override, name once each override `options` or the
/// environment sets; it is then ignored by [`bases`], [`trusted_key_hexes`] and
/// [`allow_unsigned`]. Nothing to do under a contract that honors them.
pub fn warn_overrides(options: &Options) {
    if active().overridable {
        return;
    }
    let env_set = |name: &str| std::env::var(name).is_ok_and(|v| !v.trim().is_empty());
    if options.bases.as_ref().is_some_and(|b| !b.is_empty()) {
        warn_ignored("the bases option");
    }
    if env_set(constants::ENV_BASES_NAME) {
        warn_ignored(constants::ENV_BASES_NAME);
    }
    if options.trusted_keys.as_ref().is_some_and(|k| !k.is_empty()) {
        warn_ignored("the trusted_keys option");
    }
    if env_set(constants::ENV_TRUSTED_KEYS_NAME) {
        warn_ignored(constants::ENV_TRUSTED_KEYS_NAME);
    }
    if options.allow_unsigned {
        warn_ignored("the allow_unsigned option");
    }
    if env_set(constants::ENV_ALLOW_UNSIGNED_NAME) {
        warn_ignored(constants::ENV_ALLOW_UNSIGNED_NAME);
    }
}

/// The ordered base list one call uses: under a contract that honors the
/// override, the options' own, else `$CHTYPES_ARTIFACTS_URL`
/// (comma-separated), else the contract's default; under the dev channel, its
/// one base, always.
pub fn bases(explicit: Option<&[String]>) -> Vec<String> {
    let c = active();
    let default = || c.bases.iter().map(|s| s.to_string()).collect::<Vec<_>>();
    if !c.overridable {
        return default();
    }
    if let Some(list) = explicit {
        return list.to_vec();
    }
    std::env::var(constants::ENV_BASES_NAME)
        .ok()
        .filter(|s| !s.is_empty())
        .map(|s| {
            s.split(constants::BASE_SEPARATOR)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_else(default)
}

/// The trust list one call uses, as raw ed25519 keys in hex, with the key id
/// each default key carries (`None` for a caller's own key, whose id is
/// computed): under a contract that honors the override, the caller's
/// non-empty list, else `$CHTYPES_TRUSTED_KEYS` (comma-separated), else the
/// contract's default; under the dev channel, the staging key alone, always.
pub fn trusted_key_hexes(explicit: Option<&[String]>) -> Vec<(Option<&'static str>, String)> {
    let c = active();
    let default = || {
        c.keys
            .iter()
            .map(|(id, hex)| (Some(*id), hex.to_string()))
            .collect::<Vec<_>>()
    };
    if !c.overridable {
        return default();
    }
    if let Some(list) = explicit.filter(|l| !l.is_empty()) {
        return list.iter().map(|h| (None, h.clone())).collect();
    }
    let from_env: Vec<(Option<&'static str>, String)> =
        std::env::var(constants::ENV_TRUSTED_KEYS_NAME)
            .map(|v| {
                v.split(constants::BASE_SEPARATOR)
                    .map(|k| k.trim().to_string())
                    .filter(|k| !k.is_empty())
                    .map(|k| (None, k))
                    .collect()
            })
            .unwrap_or_default();
    if from_env.is_empty() {
        default()
    } else {
        from_env
    }
}

/// Whether one call proceeds unsigned: the option or `CHTYPES_ALLOW_UNSIGNED=1`
/// under a contract that honors the override; never under the dev channel.
pub fn allow_unsigned(option: bool) -> bool {
    active().overridable
        && (option || std::env::var(constants::ENV_ALLOW_UNSIGNED_NAME).is_ok_and(|v| v == "1"))
}

/// The environment twin of the `offline` option (public issue #528):
/// `CHTYPES_OFFLINE=1` reads the cache only and makes no request. It names no
/// source, so rule r6 holds. Not a generated constant: the generated set is the
/// v1 fetch contract's.
pub const ENV_OFFLINE_NAME: &str = "CHTYPES_OFFLINE";

/// Whether a call is offline: the option, or `CHTYPES_OFFLINE=1`. The option is a
/// plain `bool`, so it can only turn the mode on; the variable is read per call,
/// with the other environment variables.
pub fn offline_mode(option: bool) -> bool {
    option || std::env::var(ENV_OFFLINE_NAME).is_ok_and(|v| v == "1")
}

/// The pinning options `options` sets, if any, by name.
pub fn pinning_requested(options: &Options) -> Vec<&'static str> {
    let mut set = Vec::new();
    if options.frozen {
        set.push("frozen");
    }
    if options.lock_write {
        set.push("lock");
    }
    if options.update {
        set.push("update");
    }
    if options.lock_path.is_some() {
        set.push("a lock path");
    }
    set
}

/// The refusal of every pinning request `options` makes, under a contract that
/// refuses them (the dev channel: rule r6), before anything else is done.
pub fn refuse_pinning(options: &Options) -> Option<String> {
    if active().pinnable {
        return None;
    }
    let set = pinning_requested(options);
    if set.is_empty() {
        return None;
    }
    Some(format!("{PINNING_REFUSED} (requested: {})", set.join(", ")))
}
