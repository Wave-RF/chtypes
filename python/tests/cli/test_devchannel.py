"""The command line under the ABI v2 dev channel (spec/abi-v2/docs.md, rules r5 and
r6). The first tests run the in-process CLI switched to the dev channel; the last
runs the real `chtypes` command in a child process, which no test seam can reach
(each one refuses outside a test run), and proves the same answers from it.
Nothing here reaches the network.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from chtypes import __main__ as cli
from chtypes._ocifetch import _channel
from chtypes._ocifetch import _constants as C

PINNING = (
    ("fetch", "26.8", "--lock", "@LOCK@"),
    ("fetch", "26.8", "--frozen"),
    ("fetch", "26.8", "--frozen", "--lock", "@LOCK@"),
    ("fetch", "--update", "--lock", "@LOCK@"),
    ("fetch", "--all", "--frozen"),
)


@pytest.fixture
def dev_channel(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    restore = _channel.use_dev_channel_for_tests()
    for name in (C.ENV_BASES_NAME, C.ENV_TRUSTED_KEYS_NAME, C.ENV_ALLOW_UNSIGNED_NAME):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(C.ENV_CACHE_NAME, raising=False)
    try:
        yield
    finally:
        restore()


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_where_is_the_v2_dev_subroot(dev_channel, tmp_path, monkeypatch, capsys) -> None:
    """r5: an explicit cache is used through <cache>/v2-dev; the default root is
    ${XDG_CACHE_HOME}/chtypes/v2-dev."""
    monkeypatch.setenv(C.ENV_CACHE_NAME, str(tmp_path / "env"))
    assert run(capsys, "where")[:2] == (0, f"{tmp_path / 'env' / 'v2-dev'}\n")
    monkeypatch.delenv(C.ENV_CACHE_NAME)
    assert run(capsys, "where", "--cache", str(tmp_path / "c"))[:2] == (
        0,
        f"{tmp_path / 'c' / 'v2-dev'}\n",
    )
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert run(capsys, "where")[:2] == (0, f"{tmp_path / 'xdg' / 'chtypes' / 'v2-dev'}\n")


def test_lock_frozen_and_update_are_refused_before_anything(
    dev_channel, tmp_path, monkeypatch, capsys
) -> None:
    """r6: exit 2, nothing on stdout, the dev channel's reason, no lock written."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(C.ENV_CACHE_NAME, str(tmp_path / "cache"))
    lock = tmp_path / "chtypes.lock"
    for argv in PINNING:
        args = [str(lock) if a == "@LOCK@" else a for a in argv]
        code, out, err = run(capsys, *args)
        assert (code, out) == (2, ""), (args, err)
        assert _channel.PINNING_REFUSED in err, (args, err)
    assert not lock.exists() and not (tmp_path / C.LOCK_DEFAULT_FILE).exists()
    assert not (tmp_path / "cache").exists()


def _real_cli() -> list[str]:
    """The `chtypes` console script this environment installed, else the same
    entry point through `python -m chtypes`."""
    script = shutil.which("chtypes", path=str(Path(sys.executable).parent))
    return [script] if script else [sys.executable, "-m", "chtypes"]


def test_the_real_cli_speaks_the_dev_channel(tmp_path: Path) -> None:
    """A child process, which no seam reaches: the subroot, the default root, the
    pinning refusals, and one warning for each ignored override all come from
    the command a user runs."""
    cmd = _real_cli()
    xdg = tmp_path / "xdg"

    def real(env: dict[str, str], *args: str) -> tuple[int, str, str]:
        full = {
            "HOME": str(tmp_path / "home"),
            "XDG_CACHE_HOME": str(xdg),
            "PATH": os.environ.get("PATH", ""),
            **env,
        }
        done = subprocess.run(
            [*cmd, *args], capture_output=True, text=True, env=full, timeout=120, cwd=tmp_path
        )
        return done.returncode, done.stdout, done.stderr

    code, out, err = real({}, "where")
    assert (code, out.strip()) == (0, str(xdg / "chtypes" / "v2-dev")), err
    cache = tmp_path / "cache"
    code, out, err = real({C.ENV_CACHE_NAME: str(cache)}, "where")
    assert (code, out.strip()) == (0, str(cache / "v2-dev")), err
    for argv in PINNING:
        args = [str(tmp_path / "chtypes.lock") if a == "@LOCK@" else a for a in argv]
        code, out, err = real({C.ENV_CACHE_NAME: str(cache)}, *args)
        assert (code, out) == (2, "") and _channel.PINNING_REFUSED in err, (args, err)
    overrides = {
        C.ENV_CACHE_NAME: str(cache),
        C.ENV_BASES_NAME: "http://127.0.0.1:9/chtypes/v1",
        C.ENV_TRUSTED_KEYS_NAME: str(C.TEST_KEYS[0]["ed25519_hex"]),
        C.ENV_ALLOW_UNSIGNED_NAME: "1",
    }
    code, out, err = real(overrides, "fetch", "26.8", "--offline", "--platform", "linux-amd64")
    assert (code, out) == (C.ERROR_EXIT_CODES["CHTYPES_ARTIFACT_MISSING"], ""), err
    assert "CHTYPES_ARTIFACT_MISSING" in err
    for name in (C.ENV_BASES_NAME, C.ENV_TRUSTED_KEYS_NAME, C.ENV_ALLOW_UNSIGNED_NAME):
        assert err.count(f"WARNING: {name} is set and IGNORED") == 1, err
    assert not (tmp_path / "chtypes.lock").exists()
