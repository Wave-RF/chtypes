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

use super::constants;
use super::ensure::Options;

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
