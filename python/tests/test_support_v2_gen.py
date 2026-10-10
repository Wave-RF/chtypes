"""The v2 support-matrix generator (scripts/support-v2/gen.py) takes its registry,
key and generation from the Python binding's ACTIVE generation-2 channel, so the
lock change's one-line `active()` switch moves the page to production with no
change to the generator (public issue #438). These are the pins on that
derivation; the generator's own `--selftest` covers verification and omission.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from chtypes._ocifetch import _channel
from chtypes._ocifetch import _constants as C

GEN = Path(__file__).resolve().parents[2] / "scripts" / "support-v2" / "gen.py"


def _load():
    spec = importlib.util.spec_from_file_location("support_v2_gen", GEN)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gen = _load()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (C.ENV_BASES_NAME, C.ENV_TRUSTED_KEYS_NAME, C.ENV_ALLOW_UNSIGNED_NAME):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def prod_v2() -> Iterator[None]:
    restore = _channel.use_prod_v2_for_tests()
    try:
        yield
    finally:
        restore()


def test_support_v2_generator_derives_its_channel_from_active_dev(
    clean_env: None, tmp_path: Path
) -> None:
    ctx = gen.channel_context()
    assert (ctx.name, ctx.abi, ctx.production) == ("v2-dev", 2, False)
    assert ctx.base == _channel.DEV_CHANNEL_BASE
    assert ctx.key_ids == (_channel.DEV_KEY_ID,)
    options = gen.channel_options(str(tmp_path))
    assert options.resolved_bases() == (_channel.DEV_CHANNEL_BASE,)
    assert [k.keyid for k in options.resolved_trusted_keys()] == [_channel.DEV_KEY_ID]
    page = gen.render(ctx, gen.Collected())
    assert "development channel" in page and "chtypes/v2-dev" in page
    assert "generated from the production registry" not in page


def test_support_v2_generator_derives_its_channel_from_active_production(
    clean_env: None, prod_v2: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An environment that tries to redirect the base and the trust changes nothing.
    monkeypatch.setenv(C.ENV_BASES_NAME, "https://mirror.example/chtypes/v2")
    monkeypatch.setenv(C.ENV_TRUSTED_KEYS_NAME, _channel.DEV_KEY_HEX)
    ctx = gen.channel_context()
    release = tuple(k["keyid"] for k in C.RELEASE_KEYS)
    assert (ctx.name, ctx.abi, ctx.production) == ("v2", 2, True)
    assert ctx.base == C.PROD_V2_BASES[0]
    assert ctx.key_ids == release
    options = gen.channel_options(str(tmp_path))
    assert options.resolved_bases() == C.PROD_V2_BASES
    assert tuple(k.keyid for k in options.resolved_trusted_keys()) == release
    assert _channel.DEV_KEY_ID not in release
    page = gen.render(ctx, gen.Collected())
    assert "generated from the production registry" in page
    assert "generated from the development channel" not in page
