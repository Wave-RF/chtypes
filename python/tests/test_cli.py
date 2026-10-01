"""`python -m chtypes` and the `chtypes` console script — docs/guides/fetch.md §6.

The surface, the exit codes, stdout carrying the installed directory alone,
progress on stderr. Most runs go through `main(argv)` in-process; the
entry points themselves are exercised in subprocesses.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import chtypes
from chtypes.__main__ import main

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "fetch"
EXPECTED_FILE = FIXTURES / "expected.json"
PLATFORM = "linux-arm64"

needs_fixtures = pytest.mark.skipif(
    not EXPECTED_FILE.is_file(),
    reason=(
        f"no fetch fixtures at {FIXTURES} "
        "(generated in the core repository, docs/guides/fetch.md §9)"
    ),
)


def _keys() -> str:
    return ",".join(json.loads(EXPECTED_FILE.read_text())["trusted_keys"])


@pytest.fixture
def env(
    isolated_search_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Path:
    if EXPECTED_FILE.is_file():
        monkeypatch.setenv("CHTYPES_TRUSTED_KEYS", _keys())
        # Fetch at the ABI revision the fixtures carry, read off their own rows.
        request.getfixturevalue("at_fixture_revision")
    return isolated_search_path


# `python -m chtypes`, exactly — runpy runs chtypes/__main__.py as __main__,
# which is all `-m` does — with fetch's test-only revision override set first
# (the child cannot inherit the parent's monkeypatch). argv[1] is the revision.
_MODULE_AT_REVISION = (
    "import runpy, sys; import chtypes.fetch as f; "
    "f._ABI_REVISION_OVERRIDE = int(sys.argv.pop(1)); "
    "runpy.run_module('chtypes', run_name='__main__', alter_sys=True)"
)


def test_where_prints_the_write_directory(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["where"]) == 0
    out = capsys.readouterr().out
    # The directory first, on its own line, so `cd "$(chtypes where | head -1)"`
    # keeps working; then the served golden set, which is the other half of
    # "what is in my registry" and the reason golden tests skip when absent.
    first, second = out.splitlines()
    assert first == str(env)
    assert second.startswith(str(env / "sdk-goldens.json"))
    assert "not fetched" in second  # an isolated search path has none


def test_usage_errors_exit_2(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (
        [],
        ["fetch"],
        ["fetch", "25.8", "--all"],
        ["fetch", "25.8", "--url", "x", "--tag", "y"],
        ["fetch", "25.8", "--platform", "windows-x64"],
        ["fetch", "not-a-version", "--url", str(FIXTURES / "signed")],
        ["bogus"],
    ):
        try:
            code = main(argv)
        except SystemExit as exc:  # argparse's own refusals
            code = exc.code
        assert code == 2, argv
        assert capsys.readouterr().out == ""


@needs_fixtures
@pytest.mark.parametrize(
    "verdict",
    json.loads(EXPECTED_FILE.read_text())["verdicts"] if EXPECTED_FILE.is_file() else [],
    ids=lambda v: (
        f"{v['fixture']}-{v['trusted_keys']}-{'unsigned-ok' if v['allow_unsigned'] else 'strict'}"
    ),
)
def test_fetch_exit_codes_and_streams(
    verdict: dict,
    env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    at_fixture_revision: int,
) -> None:
    """Every fixture verdict through the real entry point, in a subprocess:
    the spec's exit code, the code on stderr, and stdout empty or exactly
    the installed directory."""
    child_env = {
        k: v
        for k, v in __import__("os").environ.items()
        if k not in ("CHTYPES_TRUSTED_KEYS", "CHTYPES_ALLOW_UNSIGNED", "CHTYPES_REGISTRY")
    }
    child_env["XDG_CACHE_HOME"] = str(tmp_path / "xdg")
    if verdict["trusted_keys"] == "test":
        child_env["CHTYPES_TRUSTED_KEYS"] = _keys()
    if verdict["allow_unsigned"]:
        child_env["CHTYPES_ALLOW_UNSIGNED"] = "1"
    dest = tmp_path / "reg"
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _MODULE_AT_REVISION,
            str(at_fixture_revision),
            "fetch",
            "25.8",
            "--platform",
            PLATFORM,
            "--url",
            f"file://{FIXTURES / verdict['fixture']}",
            "--dest",
            str(dest),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=child_env,
        check=False,
    )
    assert proc.returncode == verdict["exit"], (verdict["why"], proc.stderr)
    if verdict["code"] is None:
        assert proc.stdout == f"{dest / '25.8'}\n"
        assert "installed and verified" in proc.stderr
        if verdict["allow_unsigned"]:
            assert proc.stderr.count("WARNING") == 1  # one loud warning, not two
            assert str(FIXTURES / verdict["fixture"]) in proc.stderr
    else:
        assert proc.stdout == ""
        assert f"chtypes: {verdict['code']}" in proc.stderr


@needs_fixtures
def test_unpublished_exits_4_and_unreachable_exits_3(
    env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    signed = str(FIXTURES / "signed")
    assert (
        main(["fetch", "24.8", "--platform", PLATFORM, "--url", signed, "--dest", str(tmp_path)])
        == 4
    )
    assert "CHTYPES_ARTIFACT_UNPUBLISHED" in capsys.readouterr().err
    monkeypatch.setattr("chtypes.fetch._HTTP_ATTEMPTS", 1)
    monkeypatch.setattr("chtypes.fetch.time.sleep", lambda s: None)
    assert main(["fetch", "25.8", "--url", "http://127.0.0.1:1/x", "--dest", str(tmp_path)]) == 3
    assert "CHTYPES_SOURCE_UNREACHABLE" in capsys.readouterr().err
    assert (
        main(["fetch", "25.8", "--offline", "--platform", PLATFORM, "--dest", str(tmp_path)]) == 3
    )
    assert "CHTYPES_SOURCE_UNREACHABLE" in capsys.readouterr().err


@needs_fixtures
def test_verify_and_list(env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    signed = str(FIXTURES / "signed")
    dest = tmp_path / "reg"
    assert main(["verify", "--dest", str(dest)]) == 0
    assert "nothing installed" in capsys.readouterr().err

    assert (
        main(["fetch", "--all", "--platform", PLATFORM, "--url", signed, "--dest", str(dest)]) == 0
    )
    out = capsys.readouterr().out
    assert out == f"{dest / '25.8'}\n{dest / '26.7'}\n"

    assert main(["verify", "--dest", str(dest)]) == 0
    out, err = capsys.readouterr()
    # The per-line rows, then the served golden set's own line — this fixture
    # release publishes none, so it reports absent rather than being silent.
    rows, goldens = out.splitlines()[:-1], out.splitlines()[-1]
    assert [ln.split()[:2] for ln in rows] == [["25.8", "ok"], ["26.7", "ok"]]
    assert goldens.startswith(str(dest / "sdk-goldens.json")) and "not fetched" in goldens
    assert "2 installed line(s) verified" in err

    assert main(["list", "--platform", PLATFORM, "--url", signed, "--dest", str(dest)]) == 0
    out = capsys.readouterr().out
    assert "installed (" in out and "[installed]" in out and "signed by key" in out
    assert "[not installed]" not in out
    assert "not shown" not in out  # every row is at the revision fetch selects at

    # One revision past the fixtures' own, list shows none of their rows and
    # says so in one line naming what it hid (docs/guides/fetch.md §6).
    from chtypes import fetch as fetch_module

    rev = fetch_module._fetch_abi_revision()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fetch_module, "_ABI_REVISION_OVERRIDE", rev + 1)
        assert main(["list", "--platform", PLATFORM, "--url", signed, "--dest", str(dest)]) == 0
    out = capsys.readouterr().out
    assert f"  2 row(s) at ABI revision(s) {rev} not shown; this SDK speaks {rev + 1}\n" in out
    assert "[installed]" not in out

    # Corrupt one library: verify says so, exits 1, and names the line.
    (dest / "26.7" / "libchtypes.so").write_bytes(b"corrupt")
    assert main(["verify", "--dest", str(dest)]) == 1
    out, err = capsys.readouterr()
    assert [ln.split()[:2] for ln in out.splitlines()[:-1]] == [["25.8", "ok"], ["26.7", "FAILED"]]
    assert ("bytes" in err or "sha256" in err) and "1 of 2" in err


@needs_fixtures
def test_list_and_verify_report_the_patches_tree_too(
    env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """SDK#284: `list` and `verify` both report every installed PATCH, not
    only the flat line slot — using `two-patches/`, which serves two patches
    of one line side by side."""
    doc = json.loads(EXPECTED_FILE.read_text())
    if "patches" not in doc:
        raise AssertionError(
            f"{EXPECTED_FILE} carries no 'patches' block — tests/fixtures/fetch/two-patches/ "
            f"is required for SDK#284 (docs/guides/fetch.md §9); this is a hard failure"
        )
    block = doc["patches"]
    line, older, newer = block["line"], block["served"][0], block["served"][1]
    two_patches = str(FIXTURES / "two-patches")
    dest = tmp_path / "reg"
    base = ["fetch", "--platform", PLATFORM, "--url", two_patches, "--dest", str(dest)]
    assert main([*base, older]) == 0  # -> patches/25.8/<older>/
    assert main([*base, line]) == 0  # -> 25.8/ (flat), holds `newer`
    capsys.readouterr()

    assert main(["list", "--platform", PLATFORM, "--url", two_patches, "--dest", str(dest)]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert any(ln.startswith(f"  {line:<8} {newer:<18} {dest / line}") for ln in lines)
    assert any(
        ln.startswith(f"  {line:<8} {older:<18} {dest / 'patches' / line / older}")
        and "(patches/)" in ln
        for ln in lines
    )
    # "release offers" is a per-LINE view (the newest patch of each line), so
    # only `newer` appears there, and it is installed.
    assert out.count("[installed]") == 1 and "[not installed]" not in out

    assert main(["verify", "--dest", str(dest)]) == 0
    out, err = capsys.readouterr()
    rows = out.splitlines()[:-1]  # last line is the golden-set line
    labels = [ln.split()[0] for ln in rows]
    assert line in labels  # the flat slot, still bare-labeled for compatibility
    assert f"{line}/{older}" in labels  # the patches/ install, labeled minor/version
    assert "2 installed line(s) verified" in err


@needs_fixtures
def test_lock_and_frozen_through_the_cli(
    env: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    signed = str(FIXTURES / "signed")
    lock = tmp_path / "chtypes.lock"
    dest = tmp_path / "reg"
    base = ["fetch", "--platform", PLATFORM, "--url", signed, "--dest", str(dest)]
    assert main([*base, "25.8", "--lock", str(lock)]) == 0
    # Schema 2 (SDK#284): keyed by the exact patch, not the line.
    patch_258 = json.loads(EXPECTED_FILE.read_text())["lines"]["25.8"]
    assert json.loads(lock.read_text())["artifacts"].keys() == {f"{PLATFORM}/{patch_258}"}
    assert json.loads(lock.read_text())["schema"] == 2
    assert main([*base, "26.7", "--lock", str(lock), "--frozen"]) == 1
    assert "CHTYPES_ARTIFACT_PINNED" in capsys.readouterr().err
    # --frozen alone reads ./chtypes.lock: none here is PINNED (exit 1), not usage.
    nolock = tmp_path / "nolock"
    nolock.mkdir()
    monkeypatch.chdir(nolock)
    assert main([*base, "25.8", "--frozen"]) == 1
    err = capsys.readouterr().err
    assert "CHTYPES_ARTIFACT_PINNED" in err and "no lock file at chtypes.lock" in err
    monkeypatch.chdir(tmp_path)  # ./chtypes.lock is the one written above
    assert main([*base, "25.8", "--frozen"]) == 0
    assert (
        main([*base, "25.8", "--lock", str(FIXTURES / "chtypes.lock"), "--frozen", "--force"]) == 0
    )


def test_the_entry_points_exist(env: Path) -> None:
    module = subprocess.run(
        [sys.executable, "-m", "chtypes", "where"], capture_output=True, text=True, check=False
    )
    assert module.returncode == 0 and module.stdout.splitlines()[0] == str(env)
    script = Path(sys.executable).with_name("chtypes")
    assert script.exists(), f"console script {script} missing: pyproject [project.scripts]"
    console = subprocess.run([str(script), "where"], capture_output=True, text=True, check=False)
    assert console.returncode == 0 and console.stdout.splitlines()[0] == str(env)
    assert chtypes.ensure.__doc__ and "idempotent" in chtypes.ensure.__doc__.lower()
