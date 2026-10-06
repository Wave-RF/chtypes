"""The fetch contract this build speaks.

A 2.0.0-dev SDK speaks the ABI v2 dev channel (spec/abi-v2/docs.md, rules r5
and r6), which is the v1 fetch contract (docs/guides/fetch-v1.md) narrowed in
five ways:

- it fetches ONLY from `DEV_CHANNEL_BASE` and trusts ONLY the staging key
  (`DEV_KEY_HEX`, id `DEV_KEY_ID`); the release key is not in its trust list;
- it has no override: `CHTYPES_ARTIFACTS_URL`, `CHTYPES_TRUSTED_KEYS` and
  `CHTYPES_ALLOW_UNSIGNED`, and the options that set a base, a trust list or an
  unsigned fetch, are ignored, each with one loud warning per process;
- it refuses a lock, frozen and update, and their options, before any network
  call: a dev build is replaceable and a superseded one expires, so nothing may
  pin one;
- its cache is one no released 1.x reader ever reads: `verified.json` records
  are schema 2, the default root is `${XDG_CACHE_HOME:-~/.cache}/chtypes/v2-dev`,
  and an explicit cache (`CHTYPES_CACHE`, `--cache`, the `cache_dir` option) is
  used through its subroot `<cache>/v2-dev`, never as a whole layout;
- a signed predicate must say abi 2.

The v1 contract itself stays in this package, unchanged, because the fetch-v1
conformance cases (tests/fixtures/fetch-v1) are its specification and the dev
channel shares every other rule with it. Only a test run reaches it:
`use_fetch_v1_for_tests` and `allow_overrides_for_tests` raise anywhere but
under pytest (the counterpart of Go's `testing.Testing()`), so neither is an
override a user can reach.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Final, Protocol

from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._dsse import TrustedKey

__all__ = [
    "DEV_ABI_GENERATION",
    "DEV_CACHE_DIR",
    "DEV_CHANNEL_BASE",
    "DEV_KEY_HEX",
    "DEV_KEY_ID",
    "DEV_RECORD_SCHEMA",
    "PINNING_REFUSED",
    "Channel",
    "PinningRefusedError",
    "abi",
    "active",
    "allow_overrides_for_tests",
    "channel_name",
    "enforce",
    "ignored_settings_warned",
    "pinning_requested",
    "trusted_keys",
    "use_dev_channel_for_tests",
    "use_fetch_v1_for_tests",
]

# The dev channel's registry and key (spec/abi-v2/docs.md, rule r6).
DEV_CHANNEL_BASE: Final = "https://registry-staging.wavehouse.dev/chtypes/v2-dev"
# The staging key's id: sha256-first16hex (constants: KEYID_ALGORITHM) over DEV_KEY_HEX.
DEV_KEY_ID: Final = "824345f9bcf8e5bf"
# The staging key, the only key a 2.0.0-dev SDK trusts: an ed25519 public key,
# raw 32 bytes as lowercase hex.
DEV_KEY_HEX: Final = "5cd30c53c65a1ebc2d85836a41deb06661bb0ae7b658adb9eb116ec2db8e9b1c"
# The dev channel's cache directory name: the default root's last element and
# an explicit cache's subroot (rule r5).
DEV_CACHE_DIR: Final = "v2-dev"
# The verified.json schema the dev channel writes, and the only one it reads (rule r5).
DEV_RECORD_SCHEMA: Final = 2
# The abi a dev predicate must carry.
DEV_ABI_GENERATION: Final = 2

# What every lock, frozen or update request gets from a 2.0.0-dev SDK, before
# any network call (rule r6).
PINNING_REFUSED: Final = (
    "--lock, --frozen and --update are refused by a 2.0.0-dev SDK: a dev build is replaceable, "
    "and a superseded one expires after 14 days, so nothing may pin one "
    "(spec/abi-v2/docs.md, rule r6)"
)


@dataclass(frozen=True)
class Channel:
    """One fetch contract."""

    name: str
    abi: int  # a predicate's abi; a lock's abi; Resolved.abi_generation
    record_schema: int  # the verified.json schema written, and the only one read
    root_leaf: str  # the default root is ${XDG_CACHE_HOME:-~/.cache}/chtypes/<root_leaf>
    subroot: str  # an explicit cache is used as <cache>/<subroot>; "" uses it whole
    system_dirs: tuple[str, ...]  # the read-only system cache dirs searched after the cache
    bases: tuple[str, ...]  # the default bases
    keys: tuple[dict[str, str], ...]  # {"keyid", "ed25519_hex"}: the default trust
    overridable: bool  # the base, trust and unsigned overrides are honored
    pinnable: bool  # lock, frozen and update are honored


# What a 2.0.0-dev SDK speaks, and what every process speaks unless a test
# selected another contract.
DEV_CHANNEL: Final = Channel(
    name="v2-dev",
    abi=DEV_ABI_GENERATION,
    record_schema=DEV_RECORD_SCHEMA,
    root_leaf=DEV_CACHE_DIR,
    subroot=DEV_CACHE_DIR,
    system_dirs=(f"/usr/local/share/chtypes/{DEV_CACHE_DIR}", f"/opt/chtypes/{DEV_CACHE_DIR}"),
    bases=(DEV_CHANNEL_BASE,),
    keys=({"keyid": DEV_KEY_ID, "ed25519_hex": DEV_KEY_HEX},),
    overridable=False,
    pinnable=False,
)

# The v1 contract the fetch-v1 conformance cases specify, from the generated
# constants. Only use_fetch_v1_for_tests selects it.
FETCH_V1_CHANNEL: Final = Channel(
    name="v1",
    abi=C.ABI_GENERATION,
    record_schema=C.SCHEMA_VERSION,
    root_leaf="v1",
    subroot="",
    system_dirs=C.SYSTEM_CACHE_DIRS,
    bases=C.DEFAULT_BASES,
    keys=C.RELEASE_KEYS,
    overridable=True,
    pinnable=True,
)

_lock = threading.Lock()
_current: Channel | None = None


def active() -> Channel:
    """The contract this process fetches under: the dev channel, unless a test
    selected another."""
    current = _current
    return DEV_CHANNEL if current is None else current


def channel_name() -> str:
    """The fetch contract this process speaks: "v2-dev" outside a test run."""
    return active().name


def abi() -> int:
    """The abi a predicate must carry under the active contract, and the
    generation a `Resolved` names."""
    return active().abi


def _test_only(what: str) -> None:
    # A seam opens only while pytest is RUNNING a test: pytest imported AND
    # PYTEST_CURRENT_TEST set (pytest sets it for each test's setup, call and
    # teardown, and unsets it between tests). pytest merely being importable,
    # or imported by a production process, opens nothing; neither does the
    # variable alone (anyone can export it). Go's equivalent is testing.Testing().
    if "_pytest" not in sys.modules or not os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError(
            f"chtypes._ocifetch: {what} is test-only; a 2.0.0-dev SDK has no override "
            "(spec/abi-v2/docs.md, rule r6)"
        )


def _use(channel: Channel) -> Callable[[], None]:
    global _current
    with _lock:
        previous = _current
        _current = channel

    def restore() -> None:
        global _current
        with _lock:
            _current = previous

    return restore


def use_fetch_v1_for_tests() -> Callable[[], None]:
    """Make this TEST run speak the v1 fetch contract (docs/guides/fetch-v1.md):
    overrides, locks, schema-1 records and abi-1 predicates. The fetch-v1
    conformance cases and the v1-protocol tests run under it; every case names
    its own fixture registry and the test key. Returns the function that
    restores the previous contract; raises outside a test run."""
    _test_only("use_fetch_v1_for_tests")
    return _use(FETCH_V1_CHANNEL)


def allow_overrides_for_tests() -> Callable[[], None]:
    """Make this TEST run's dev channel honor the base, trust and unsigned
    overrides, so a test can reach a fixture registry signed with the test key;
    everything else stays the dev channel's (abi 2, schema-2 records, the v2-dev
    cache, no pinning). Returns the restore function; raises outside a test run."""
    _test_only("allow_overrides_for_tests")
    return _use(replace(DEV_CHANNEL, overridable=True))


def use_dev_channel_for_tests() -> Callable[[], None]:
    """Make this TEST run speak the dev channel exactly as a user's process does
    (undoing either function above, for a test of the dev channel itself).
    Returns the restore function."""
    _test_only("use_dev_channel_for_tests")
    return _use(DEV_CHANNEL)


def trusted_keys(channel: Channel | None = None) -> tuple[TrustedKey, ...]:
    """The contract's default trust list: the staging key alone on the dev
    channel, the release key(s) under the v1 contract."""
    channel = channel or active()
    return tuple(
        TrustedKey(keyid=k["keyid"], public_key=bytes.fromhex(k["ed25519_hex"]))
        for k in channel.keys
    )


# ----------------------------------------------------------- ignored settings

_warn_lock = threading.Lock()
_warned: set[str] = set()


def _warn_ignored(setting: str) -> None:
    """Say, once per process per setting, that a dev SDK ignores it (rule r6:
    "a dev SDK says so, once and loudly")."""
    with _warn_lock:
        if setting in _warned:
            return
        _warned.add(setting)
    sys.stderr.write(
        f"chtypes: WARNING: {setting} is set and IGNORED: this is a 2.0.0-dev SDK, which fetches "
        f"only from {DEV_CHANNEL_BASE} and trusts only the staging key {DEV_KEY_ID} "
        "(spec/abi-v2/docs.md, rule r6)\n"
    )
    sys.stderr.flush()


def ignored_settings_warned() -> list[str]:
    """The settings warned about so far, sorted; for tests."""
    with _warn_lock:
        return sorted(_warned)


def _forget_warnings_for_tests() -> set[str]:
    """Forget which settings were warned about, returning the old set; for tests."""
    global _warned
    _test_only("_forget_warnings_for_tests")
    with _warn_lock:
        old, _warned = _warned, set()
    return old


def _restore_warnings_for_tests(old: set[str]) -> None:
    global _warned
    with _warn_lock:
        _warned = old


# ---------------------------------------------------------------- the checks


class _PinningOptions(Protocol):
    frozen: bool
    lock_write: bool
    update: bool
    lock_path: str | os.PathLike[str] | None


class _OverrideOptions(_PinningOptions, Protocol):
    bases: Sequence[str]
    trusted_keys: Sequence[TrustedKey] | None
    allow_unsigned: bool


def pinning_requested(options: _PinningOptions) -> list[str]:
    """The pinning options a call set, if any."""
    requested = []
    if options.frozen:
        requested.append("frozen")
    if options.lock_write:
        requested.append("lock")
    if options.update:
        requested.append("update")
    if options.lock_path is not None:
        requested.append("a lock path")
    return requested


class PinningRefusedError(ValueError):
    """A 2.0.0-dev SDK's refusal of a lock, frozen or update request (rule r6),
    raised before any network call. The caller's misuse, never an artifact
    failure: the command line's usage exit status, the public API's
    `UsageError`."""

    def __init__(self, requested: Sequence[str]) -> None:
        self.requested = tuple(requested)
        super().__init__(f"chtypes: {PINNING_REFUSED} (requested: {', '.join(self.requested)})")


def enforce(options: _OverrideOptions) -> None:
    """What the active contract does with `options` before anything else, the
    network above all: the dev channel refuses every pinning request, and names
    each override it ignores, once, loudly (rule r6). The v1 contract does
    neither."""
    channel = active()
    if not channel.pinnable:
        requested = pinning_requested(options)
        if requested:
            raise PinningRefusedError(requested)
    if channel.overridable:
        return
    if options.bases:
        _warn_ignored("the bases option")
    if os.environ.get(C.ENV_BASES_NAME, "").strip():
        _warn_ignored(C.ENV_BASES_NAME)
    if options.trusted_keys is not None:
        _warn_ignored("the trusted_keys option")
    if os.environ.get(C.ENV_TRUSTED_KEYS_NAME, "").strip():
        _warn_ignored(C.ENV_TRUSTED_KEYS_NAME)
    if options.allow_unsigned:
        _warn_ignored("the allow_unsigned option")
    if os.environ.get(C.ENV_ALLOW_UNSIGNED_NAME, ""):
        _warn_ignored(C.ENV_ALLOW_UNSIGNED_NAME)
