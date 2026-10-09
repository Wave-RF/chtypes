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

It also resolves one tag the v1 contract never asks for: a version request
fetches `<tag>--fp-<its own fingerprint>` first, the newest dev build of the ABI
this binding speaks, and falls back to `<tag>` only when no base has that alias
(docs/guides/fetch-v1.md §3, "The dev channel's alias step"), so a dev build of a
newer fingerprint never strands this one. Its cache lookups (`resolve_installed`,
and `ensure`'s offline answer and its monotonic rule) see only the records whose
signed `abi_fingerprint` is its own: a build of another fingerprint in a shared
cache is never returned and never kept over this one's own, and it is never
removed or rewritten either. It is automatic, with no override; the trust checks
are unchanged, and a listing never shows an alias. The fingerprint
is the generated `CHS_ABI_FINGERPRINT` of the ABI layer (`chtypes._abi2._decls`),
never a hand-written copy.

The v1 contract itself stays in this package, unchanged, because the fetch-v1
conformance cases (tests/fixtures/fetch-v1) are its specification and the dev
channel shares every other rule with it. Only a test run reaches it:
`use_fetch_v1_for_tests` and `allow_overrides_for_tests` raise anywhere but
under pytest (the counterpart of Go's `testing.Testing()`), so neither is an
override a user can reach.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Final, Protocol

from chtypes._abi2._decls import CHS_ABI_FINGERPRINT
from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._dsse import TrustedKey
from chtypes._ocifetch._errors import ArtifactUnpublishedError

__all__ = [
    "ALIAS_SEPARATOR",
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
    "ahead_of_registry",
    "alias_tag",
    "allow_overrides_for_tests",
    "channel_name",
    "enforce",
    "ignored_settings_warned",
    "pinning_requested",
    "trusted_keys",
    "use_own_fingerprint_for_tests",
    "visible",
    "use_dev_channel_for_tests",
    "use_fetch_v1_for_tests",
    "use_prod_v2_for_tests",
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
# The environment twin of the offline option (public issue #528): CHTYPES_OFFLINE=1
# reads the cache only and makes no request. It names no source, so rule r6 holds.
# Not a generated constant: the generated set is the v1 fetch contract's.
ENV_OFFLINE_NAME: Final = "CHTYPES_OFFLINE"
DEV_CACHE_DIR: Final = "v2-dev"
# The verified.json schema the dev channel writes, and the only one it reads (rule r5).
DEV_RECORD_SCHEMA: Final = 2
# The abi a dev predicate must carry.
DEV_ABI_GENERATION: Final = 2

# Joins a tag and a fingerprint into the dev channel's alias tag:
# <tag>--fp-<64 lowercase hex> (docs/guides/fetch-v1.md §3).
ALIAS_SEPARATOR: Final = "--fp-"

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
    # The fingerprint, 64 lowercase hex, this contract's SDK speaks: a version
    # request resolves its alias tag before the tag itself, and the cache
    # lookups see only records signed with it. "" does neither (the v1 contract
    # and every production channel).
    own_fingerprint: str = ""


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
    own_fingerprint=CHS_ABI_FINGERPRINT.removeprefix("sha256:"),
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

# The production generation-2 channel a 2.0.0 SDK speaks after the lock: the v1
# contract with v2 values, from the generated constants (docs/guides/fetch-v1.md,
# "Generation 2 after the lock"). It trusts the release key, honors every
# override and pin, and has no alias step. NOT the default: only
# use_prod_v2_for_tests selects it, until the lock change makes it active()'s
# fallback.
PROD_V2_CHANNEL: Final = Channel(
    name=C.PROD_V2_NAME,
    abi=C.PROD_V2_ABI_GENERATION,
    record_schema=C.PROD_V2_RECORD_SCHEMA,
    root_leaf=C.PROD_V2_CACHE_DIR,
    subroot=C.PROD_V2_CACHE_DIR,
    system_dirs=C.PROD_V2_SYSTEM_DIRS,
    bases=C.PROD_V2_BASES,
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


def use_prod_v2_for_tests() -> Callable[[], None]:
    """Make this TEST run speak the production generation-2 channel
    (`PROD_V2_CHANNEL`): the v1 contract with abi 2, schema-2 records and the
    `v2` subroot. Returns the restore function; raises outside a test run."""
    _test_only("use_prod_v2_for_tests")
    return _use(PROD_V2_CHANNEL)


def allow_overrides_for_tests() -> Callable[[], None]:
    """Make this TEST run's dev channel honor the base, trust and unsigned
    overrides, so a test can reach a fixture registry signed with the test key;
    everything else stays the dev channel's (abi 2, schema-2 records, the v2-dev
    cache, no pinning). Returns the restore function; raises outside a test run."""
    _test_only("allow_overrides_for_tests")
    return _use(replace(DEV_CHANNEL, overridable=True))


_FINGERPRINT_HEX = re.compile(r"[0-9a-f]{64}")


def use_own_fingerprint_for_tests(fingerprint: str) -> Callable[[], None]:
    """Make this TEST run's active contract speak `fingerprint` (64 lowercase
    hex) as the dev channel speaks its own: a version request resolves that
    fingerprint's alias tag first, and the cache lookups see only records
    signed with it. The fetch-v1 conformance cases that carry
    `request.own_fingerprint` run under it, against fixtures that name a
    fixture fingerprint. Everything else stays the active contract's. Returns
    the restore function; raises outside a test run or on a fingerprint that is
    not 64 lowercase hex."""
    _test_only("use_own_fingerprint_for_tests")
    if not _FINGERPRINT_HEX.fullmatch(fingerprint):
        raise ValueError(f"use_own_fingerprint_for_tests: {fingerprint!r} is not 64 lowercase hex")
    return _use(replace(active(), own_fingerprint=fingerprint))


def alias_tag(tag: str, channel: Channel | None = None) -> str | None:
    """The alias a version request for `tag` resolves first under a contract
    with an own fingerprint, or None when it resolves `tag` alone: under the
    v1 contract and every production channel, and for a request that is not a
    version spelling (an arbitrary tag has no alias)."""
    channel = channel or active()
    if not channel.own_fingerprint or not re.fullmatch(C.SPELLING_REGEX, tag):
        return None
    return f"{tag}{ALIAS_SEPARATOR}{channel.own_fingerprint}"


def visible(predicate: object, channel: Channel | None = None) -> bool:
    """Whether a cache record (or a pre-seeded entry) whose SIGNED predicate is
    `predicate` may answer a request under the active contract: under one with
    an own fingerprint (the dev channel), only when the predicate's
    `abi_fingerprint` is that fingerprint; under every other, always. A record
    it cannot see is never returned and never kept by the monotonic rule, and
    nothing removes or rewrites it: another SDK of another fingerprint owns it."""
    channel = channel or active()
    if not channel.own_fingerprint:
        return True
    fingerprint = predicate.get("abi_fingerprint") if isinstance(predicate, dict) else None
    return fingerprint == f"sha256:{channel.own_fingerprint}"


def ahead_of_registry(
    alias_absent: bool,
    predicate: object,
    channel: Channel | None = None,
    *,
    cached_own_build: Callable[[], str | None] | None = None,
) -> None:
    """The dev channel's answer for an SDK whose fingerprint no published build
    carries yet (docs/guides/fetch-v1.md §3; public issue #578): when every base
    answered its own alias 404 (`alias_absent`) and the build the tag names is
    signed for another fingerprint (`predicate`, the SIGNED predicate, the field
    `visible` keys on), raise `ArtifactUnpublishedError`, the code a request no
    build answers gets, naming both fingerprints. Nothing is installed and an
    install of that build is never reported, because the lookup this SDK opens
    through would refuse it. Returns in every other case: the alias answered,
    the tag's build is this SDK's own, or the contract has no own fingerprint.

    The expired alias with a usable cache (public issue #581): `cached_own_build`
    looks up, through the filter open uses, the cache's verified record of this
    SDK's OWN fingerprint for the request, and returns its build id ("" for a
    record that names none) or None for no record. When there is one the
    message grows by `_cached_own_suffix`; the code does not change."""
    channel = channel or active()
    if not alias_absent or visible(predicate, channel):
        return
    message = _ahead_message(channel.own_fingerprint, predicate)
    if cached_own_build is not None:
        build = cached_own_build()
        if build is not None:
            message += _cached_own_suffix(build)
    raise ArtifactUnpublishedError(f"chtypes: {message}")


def _cached_own_suffix(build: str) -> str:
    """The clause appended when the cache holds an own-fingerprint build: "; a
    cached build for this fingerprint (build <id>) is still usable by open;
    upgrade the SDK to get newer builds", the parenthesis dropped when the
    record names no build."""
    suffix = "; a cached build for this fingerprint"
    if build:
        suffix += f" (build {build})"
    return suffix + " is still usable by open; upgrade the SDK to get newer builds"


def _ahead_message(own: str, predicate: object) -> str:
    """`ahead_of_registry`'s message: "no published build for this SDK's
    fingerprint <own>; newest published on this channel is <fp> (build <id>)",
    each fingerprint 64 lowercase hex without its `sha256:` prefix, the
    parenthesis dropped when the predicate names no build, and "unnamed" for a
    predicate that names no fingerprint."""
    fields = predicate if isinstance(predicate, dict) else {}
    theirs = fields.get("abi_fingerprint")
    theirs = theirs.removeprefix("sha256:") if isinstance(theirs, str) else ""
    message = (
        f"no published build for this SDK's fingerprint {own}; "
        f"newest published on this channel is {theirs or 'unnamed'}"
    )
    build = fields.get("build")
    if isinstance(build, str) and build:
        message += f" (build {build})"
    return message


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
