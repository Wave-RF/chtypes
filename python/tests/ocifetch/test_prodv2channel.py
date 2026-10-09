"""The production generation-2 channel (`chtypes._ocifetch._channel.PROD_V2_CHANNEL`;
docs/guides/fetch-v1.md, "Generation 2 after the lock"), built and tested but not
the default. The fetch conformance cases run under it in `test_conformance.py`;
these are the pins those cases do not reach. Nothing here reaches the network.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from chtypes._ocifetch import _channel
from chtypes._ocifetch import _constants as C
from chtypes._ocifetch._ensure import Options, search_roots
from chtypes._ocifetch._layout import resolve_cache_root


@pytest.fixture(autouse=True)
def prod_v2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    restore = _channel.use_prod_v2_for_tests()
    for name in (
        C.ENV_BASES_NAME,
        C.ENV_TRUSTED_KEYS_NAME,
        C.ENV_ALLOW_UNSIGNED_NAME,
        C.ENV_CACHE_NAME,
    ):
        monkeypatch.delenv(name, raising=False)
    try:
        yield
    finally:
        restore()


def test_the_production_v2_channel_carries_the_v2_values() -> None:
    c = _channel.active()
    assert (c.name, c.abi, c.record_schema, c.root_leaf, c.subroot) == ("v2", 2, 2, "v2", "v2")
    assert c.overridable and c.pinnable and c.own_fingerprint == ""
    assert c.system_dirs == ("/usr/local/share/chtypes/v2", "/opt/chtypes/v2")
    options = Options()
    assert options.resolved_bases() == ("https://registry.wavehouse.dev/chtypes/v2",)
    trust = options.resolved_trusted_keys()
    assert [k.keyid for k in trust] == ["deb275922dbff76e"]
    assert not {k.public_key.hex() for k in trust} & {_channel.DEV_KEY_HEX}


def test_the_production_v2_channel_honors_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(C.ENV_BASES_NAME, "https://mirror.example/chtypes/v2")
    assert Options().resolved_bases() == ("https://mirror.example/chtypes/v2",)


def test_the_production_v2_channel_cache_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert resolve_cache_root() == tmp_path / "xdg" / "chtypes" / "v2"
    assert resolve_cache_root(tmp_path / "opt") == tmp_path / "opt" / "v2"
    roots = search_roots(tmp_path / "opt")
    assert roots[1:] == (Path("/usr/local/share/chtypes/v2"), Path("/opt/chtypes/v2"))


def test_the_production_v2_channel_is_not_the_default() -> None:
    # What an ordinary process speaks, with no seam selected.
    restore = _channel.use_dev_channel_for_tests()
    try:
        assert _channel.channel_name() == "v2-dev"
    finally:
        restore()
    assert _channel.channel_name() == "v2"
