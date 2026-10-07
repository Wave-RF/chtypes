"""The ABI v2 dev channel (`chtypes._ocifetch._channel`; spec/abi-v2/docs.md, rules
r5 and r6), tested as a user's process speaks it: each test switches to it with
`use_dev_channel_for_tests` for its own duration, undoing this directory's v1
seam (conftest.py). Nothing here reaches the network: every refusal is proven to
come before the first request, and every cache read is offline.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

import chtypes._ocifetch._http as http_module
from chtypes._ocifetch import _channel
from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._ensure import (
    Options,
    Request,
    _check_predicate_matches_request,
    _record_to_resolved,
    ensure,
    fetch_signed,
    list_installed,
    resolve_installed,
    verify_installed,
)
from chtypes._ocifetch._errors import ArtifactCorruptError, ArtifactMissingError
from chtypes._ocifetch._layout import VerifiedRecord, resolve_cache_root, search_roots

REPO = Path(__file__).resolve().parents[3]
V1_CACHE = REPO / "tests" / "fixtures" / "fetch-v1" / "layouts" / "cache-record-canonical"
OVERRIDE_ENV = (C.ENV_BASES_NAME, C.ENV_TRUSTED_KEYS_NAME, C.ENV_ALLOW_UNSIGNED_NAME)


@pytest.fixture(autouse=True)
def dev_channel(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    restore = _channel.use_dev_channel_for_tests()
    for name in (
        *OVERRIDE_ENV,
        C.ENV_CACHE_NAME,
        C.ENV_CACHE_STRICT_NAME,
        _channel.ENV_OFFLINE_NAME,
    ):
        monkeypatch.delenv(name, raising=False)
    try:
        yield
    finally:
        restore()


@pytest.fixture
def fresh_warnings() -> Iterator[None]:
    """Forget which ignored settings were already named, so a test sees exactly its own."""
    old = _channel._forget_warnings_for_tests()
    try:
        yield
    finally:
        _channel._restore_warnings_for_tests(old)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every request the fetch layer would make, recorded and refused."""
    requests: list[str] = []

    def refuse(*args, **kwargs):
        requests.append(str(args[0]) if args else repr(kwargs))
        raise AssertionError(f"a request was made: {args!r}")

    monkeypatch.setattr(http_module, "_single_request", refuse)
    monkeypatch.setattr(http_module, "_fetch_file", refuse)
    return requests


def test_the_dev_channel_pins_the_staging_base_and_key_alone() -> None:
    """r6: the base, the key and its id are the ones the rule pins, and the
    release key is not in the dev trust list."""
    assert _channel.DEV_CHANNEL_BASE == "https://registry-staging.wavehouse.dev/chtypes/v2-dev"
    key = bytes.fromhex(_channel.DEV_KEY_HEX)
    assert _channel.DEV_KEY_HEX == (
        "5cd30c53c65a1ebc2d85836a41deb06661bb0ae7b658adb9eb116ec2db8e9b1c"
    )
    # The id by the constants' own algorithm (sha256-first16hex), not copied.
    assert C.KEYID_ALGORITHM == "sha256-first16hex"
    assert hashlib.sha256(key).hexdigest()[:16] == _channel.DEV_KEY_ID == "824345f9bcf8e5bf"
    options = Options()
    assert options.resolved_bases() == (_channel.DEV_CHANNEL_BASE,)
    trust = options.resolved_trusted_keys()
    assert [(k.keyid, k.public_key) for k in trust] == [(_channel.DEV_KEY_ID, key)]
    release = {k["ed25519_hex"] for k in C.RELEASE_KEYS}
    assert not release & {k.public_key.hex() for k in trust}, "the release key is trusted"
    assert _channel.channel_name() == "v2-dev" and _channel.abi() == 2


def test_the_dev_channel_ignores_every_override_naming_each_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fresh_warnings
) -> None:
    """r6: no override. Every base, trust and unsigned setting, from the
    environment and from the options, is ignored, and each is named exactly once
    per process, however many calls see it."""
    test_key = C.TEST_KEYS[0]["ed25519_hex"]
    monkeypatch.setenv(C.ENV_BASES_NAME, "http://127.0.0.1:9/chtypes/v1")
    monkeypatch.setenv(C.ENV_TRUSTED_KEYS_NAME, str(test_key))
    monkeypatch.setenv(C.ENV_ALLOW_UNSIGNED_NAME, "1")
    from chtypes._ocifetch._dsse import fixture_trusted_keys

    options = Options(
        bases=("http://127.0.0.1:9/x",),
        trusted_keys=fixture_trusted_keys(),
        allow_unsigned=True,
        system_dirs=(),
    )
    for _ in range(3):
        _channel.enforce(options)
        assert options.resolved_bases() == (_channel.DEV_CHANNEL_BASE,)
        assert [k.keyid for k in options.resolved_trusted_keys()] == [_channel.DEV_KEY_ID]
        assert options.resolved_allow_unsigned() is False
        list_installed(options)  # a seam call names nothing twice
    want = sorted(
        [*OVERRIDE_ENV, "the allow_unsigned option", "the bases option", "the trusted_keys option"]
    )
    assert _channel.ignored_settings_warned() == want
    err = capsys.readouterr().err
    for setting in want:
        assert err.count(f"WARNING: {setting} is set and IGNORED") == 1, err
    assert _channel.DEV_CHANNEL_BASE in err and _channel.DEV_KEY_ID in err


@pytest.mark.parametrize(
    "pinning",
    [
        {"frozen": True},
        {"lock_write": True, "lock_path": "chtypes.lock"},
        {"update": True, "lock_path": "chtypes.lock"},
        {"lock_path": "chtypes.lock"},
        {"frozen": True, "offline": True},
    ],
    ids=["frozen", "lock-write", "update", "a-lock-path", "frozen-offline"],
)
def test_the_dev_channel_refuses_pinning_before_any_request(
    pinning: dict, no_network: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """r6: a lock, frozen and update, as options, are refused before any network
    call, with the dev channel's reason, by every seam call; no lock is written."""
    monkeypatch.chdir(tmp_path)
    options = Options(platform="linux-amd64", cache_dir=tmp_path / "cache", **pinning)
    calls = (
        lambda: ensure(Request("26.8"), options),
        lambda: resolve_installed(Request("26.8"), "linux-amd64", options),
        lambda: list_installed(options),
        lambda: verify_installed(options),
        lambda: fetch_signed("", "sha256:" + "0" * 64, C.PREDICATE_TYPE_GOLDENS, options),
    )
    for call in calls:
        with pytest.raises(_channel.PinningRefusedError) as info:
            call()
        assert isinstance(info.value, ValueError)  # the caller's misuse, never an artifact error
        assert _channel.PINNING_REFUSED in str(info.value) and "14 days" in str(info.value)
    assert no_network == []
    assert not (tmp_path / "chtypes.lock").exists()
    assert not (tmp_path / "cache").exists()


def test_the_dev_channel_cache_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """r5: the default root, and an explicit cache from the environment or the
    option, each used through the v2-dev subroot, never as a whole layout; the
    system dirs are v2-dev dirs, never a 1.x one."""
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg))
    assert resolve_cache_root() == xdg / "chtypes" / "v2-dev"
    env = tmp_path / "env"
    monkeypatch.setenv(C.ENV_CACHE_NAME, str(env))
    assert resolve_cache_root() == env / "v2-dev"
    opt = tmp_path / "opt"
    assert resolve_cache_root(opt) == opt / "v2-dev"
    roots = search_roots(opt)
    assert roots[0] == opt / "v2-dev"
    assert roots[1:] and all(str(d).endswith("/v2-dev") for d in roots[1:])
    assert not set(map(str, roots[1:])) & set(C.SYSTEM_CACHE_DIRS)


def _record() -> VerifiedRecord:
    return VerifiedRecord(
        manifest="sha256:" + "b" * 64,
        layer="sha256:" + "c" * 64,
        platform="linux-amd64",
        version="26.8.15.10",
        build="20261001.000000",
        channel=None,
        predicate={"abi": 2},
        signed_by=_channel.DEV_KEY_ID,
        library="libchtypes.so",
        library_sha256="a" * 64,
        library_bytes=1,
    )


def test_the_dev_channel_writes_and_reads_only_schema_2_records() -> None:
    """r5: the dev channel writes schema-2 records and reads only those; a
    schema-1 record (every released 1.x writer's) is absent to it."""
    doc = _record().to_json()
    assert doc["schema"] == 2
    assert VerifiedRecord.from_json(json.loads(json.dumps(doc))).version == "26.8.15.10"
    with pytest.raises(ValueError, match="schema is not 2"):
        VerifiedRecord.from_json(dict(doc, schema=1))
    resolved = _record_to_resolved(
        Path("/d"), _record(), request_spelling="26.8", already_installed=True, source="cache"
    )
    assert resolved.abi_generation == 2


def test_the_dev_channel_predicate_must_say_abi_2() -> None:
    """r6: a signed predicate must say abi 2; an abi-1 one (a 1.x build) is
    refused even when every other field matches."""
    predicate = {
        "abi": 1,
        "os": "linux",
        "arch": "arm64",
        "clickhouse_version": "26.8.15.10",
        "build": "20261001.000000",
        "library": "libchtypes.so",
        "library_sha256": "a" * 64,
        "library_bytes": 1,
    }
    with pytest.raises(ArtifactCorruptError, match="want 2"):
        _check_predicate_matches_request(predicate, "linux-arm64", "26.8")
    _check_predicate_matches_request(dict(predicate, abi=2), "linux-arm64", "26.8")


def test_the_dev_channel_never_reads_a_1x_cache(tmp_path: Path) -> None:
    """r5, end to end: a cache a 1.x binding wrote is never what the dev channel
    reads, whether the cache names it (the dev channel reads its subroot) or the
    1.x layout sits in the subroot itself (its schema-1 records are absent). The
    control is the same cache read under the v1 contract."""
    if not V1_CACHE.is_dir():
        pytest.skip(f"SKIPPED: the fetch-v1 fixtures are not beside this checkout ({V1_CACHE})")
    cache = tmp_path / "cache"
    shutil.copytree(V1_CACHE, cache)
    shutil.copytree(V1_CACHE, cache / "v2-dev")
    options = Options(cache_dir=cache, system_dirs=())

    restore = _channel.use_fetch_v1_for_tests()
    try:
        v1 = list_installed(options)
    finally:
        restore()
    assert v1, "control: the v1 contract lists nothing; the fixture is not a 1.x cache"

    assert list_installed(options) == []
    for r in v1:
        assert resolve_installed(Request(r.version), r.platform, options) is None
    assert verify_installed(options) == []


def test_the_seams_are_test_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The v1 contract and the overrides are reachable only from a test run:
    outside one, each seam refuses (the counterpart of Go's testing.Testing())."""
    import sys

    monkeypatch.delitem(sys.modules, "_pytest")
    for seam in (
        _channel.use_fetch_v1_for_tests,
        _channel.allow_overrides_for_tests,
        _channel.use_dev_channel_for_tests,
    ):
        with pytest.raises(RuntimeError, match="test-only"):
            seam()
    assert _channel.channel_name() == "v2-dev"


def test_offline_env_alone_is_artifact_missing_with_zero_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_network: list[str]
) -> None:
    """#528: CHTYPES_OFFLINE=1 is the environment twin of the offline option. With
    nothing installed it is CHTYPES_ARTIFACT_MISSING, and not one request is made
    (the counter is `no_network`, as for the refusals above)."""
    monkeypatch.setenv(_channel.ENV_OFFLINE_NAME, "1")
    options = Options(cache_dir=tmp_path / "cache", system_dirs=(), platform="linux-arm64")
    assert options.offline is False and options.resolved_offline() is True
    with pytest.raises(ArtifactMissingError):
        ensure(Request("26.8"), options)
    assert no_network == []
    # The option explicitly false does not turn the variable off.
    off = Options(
        cache_dir=tmp_path / "cache", system_dirs=(), platform="linux-arm64", offline=False
    )
    with pytest.raises(ArtifactMissingError):
        ensure(Request("26.8"), off)
    assert no_network == []
    # The option alone, with the variable unset, is offline too.
    monkeypatch.delenv(_channel.ENV_OFFLINE_NAME)
    on = Options(cache_dir=tmp_path / "cache", system_dirs=(), platform="linux-arm64", offline=True)
    with pytest.raises(ArtifactMissingError):
        ensure(Request("26.8"), on)
    assert no_network == []


def test_offline_env_with_an_installed_build_loads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_network: list[str]
) -> None:
    from .test_cache_roots import _write_record

    monkeypatch.setenv(_channel.ENV_OFFLINE_NAME, "1")
    cache = tmp_path / "cache"
    options = Options(cache_dir=cache, system_dirs=(), platform="linux-arm64")
    entry = _write_record(resolve_cache_root(cache), "26.8.1.1", "20260801.000001")
    resolved = ensure(Request("26.8"), options)
    assert resolved.dir == entry
    assert no_network == []


def test_offline_env_only_one_is_on_and_the_two_are_ored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for value, want in (("1", True), ("0", False), ("", False), ("true", False)):
        monkeypatch.setenv(_channel.ENV_OFFLINE_NAME, value)
        assert Options().resolved_offline() is want, value
    monkeypatch.setenv(_channel.ENV_OFFLINE_NAME, "1")
    assert Options(offline=False).resolved_offline() is True
    monkeypatch.delenv(_channel.ENV_OFFLINE_NAME)
    assert Options(offline=True).resolved_offline() is True
