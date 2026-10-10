"""The public API under the ABI v2 dev channel (spec/abi-v2/docs.md, rules r5 and
r6), as a user's process speaks it (no seam is active outside the directories
that install one): `Registry` refuses a lock, frozen or update request as the
caller's misuse before any request, and `FetchOptions` ignores every base, trust
and unsigned override, naming each once. Needs no library and no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import chtypes._ocifetch._http as http_module
from chtypes import FetchOptions, Registry, UsageError
from chtypes._ocifetch import _channel
from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._dsse import fixture_trusted_keys


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (C.ENV_BASES_NAME, C.ENV_TRUSTED_KEYS_NAME, C.ENV_ALLOW_UNSIGNED_NAME):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(C.ENV_AUTOFETCH_NAME, raising=False)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    seen: list[object] = []

    def refuse(*args, **kwargs):
        seen.append(args)
        raise AssertionError(f"a request was made: {args!r}")

    monkeypatch.setattr(http_module, "_single_request", refuse)
    monkeypatch.setattr(http_module, "_fetch_file", refuse)
    return seen


def test_this_process_speaks_the_dev_channel() -> None:
    assert _channel.channel_name() == "v2-dev" and _channel.abi() == 2


@pytest.mark.parametrize(
    "pinning",
    [
        {"frozen": True},
        {"lock_path": "chtypes.lock"},
        {"lock_path": "chtypes.lock", "lock_write": True},
        {"lock_path": "chtypes.lock", "update": True},
    ],
    ids=["frozen", "lock-path", "lock-write", "update"],
)
def test_registry_refuses_pinning_as_misuse_before_any_request(
    pinning: dict, tmp_path: Path, no_network: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fetch = FetchOptions(cache_dir=tmp_path / "cache", **pinning)
    registry = Registry(fetch=fetch, autofetch=True)
    for call in (lambda: registry.for_version("26.8"), registry.installed):
        with pytest.raises(UsageError) as info:
            call()
        assert _channel.PINNING_REFUSED in str(info.value)
    assert no_network == [] and registry.libraries() == ()
    assert not (tmp_path / "cache").exists() and not (tmp_path / "chtypes.lock").exists()


def test_fetch_options_overrides_are_ignored_and_named_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    old = _channel._forget_warnings_for_tests()
    try:
        monkeypatch.setenv(C.ENV_ALLOW_UNSIGNED_NAME, "1")
        fetch = FetchOptions(
            bases=("http://127.0.0.1:9/x",),
            trusted_keys=tuple(k.public_key.hex() for k in fixture_trusted_keys()),
            allow_unsigned=True,
            cache_dir=tmp_path,
        )
        options = fetch._to_options()
        assert options.resolved_bases() == (_channel.DEV_CHANNEL_BASE,)
        assert [k.keyid for k in options.resolved_trusted_keys()] == [_channel.DEV_KEY_ID]
        assert options.resolved_allow_unsigned() is False
        # The variable alone does not become an unsigned option on the dev channel.
        assert FetchOptions()._to_options().allow_unsigned is False
        for _ in range(2):
            assert Registry(fetch=fetch).installed() == ()
        err = capsys.readouterr().err
        for name in (
            "the bases option",
            "the trusted_keys option",
            "the allow_unsigned option",
            C.ENV_ALLOW_UNSIGNED_NAME,
        ):
            assert err.count(f"WARNING: {name} is set and IGNORED") == 1, err
    finally:
        _channel._restore_warnings_for_tests(old)
