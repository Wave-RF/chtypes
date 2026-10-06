"""The CLI over the v1 fetch layer: `python -m chtypes` / the `chtypes` script.

Driven in-process through `main(argv)` against a hand-written `file://` tree,
with the fetch layer's own environment (`CHTYPES_ARTIFACTS_URL`,
`CHTYPES_CACHE`, `CHTYPES_TRUSTED_KEYS`) rather than any test hook.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from chtypes import __main__ as cli
from chtypes._ocifetch import _constants as C
from chtypes._ocifetch import _errors as fetch_errors
from chtypes.errors import ArtifactIncompatibleError
from ocifetch._registry_support import build_tree


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tree = build_tree(tmp_path / "registry")
    (tree.root / "tags").mkdir()
    # a version tag, a decorated one, and a referrers fallback tag: only the first is a version
    tags = {"name": "chtypes/v1", "tags": ["26.8", "26.8.15.10", "v26.8", f"sha256-{'0' * 64}"]}
    (tree.root / "tags" / "list").write_text(json.dumps(tags))
    monkeypatch.setenv("CHTYPES_ARTIFACTS_URL", tree.base_url)
    monkeypatch.setenv("CHTYPES_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("CHTYPES_TRUSTED_KEYS", tree.public_key.hex())
    monkeypatch.delenv("CHTYPES_ALLOW_UNSIGNED", raising=False)
    monkeypatch.setattr(cli, "detect_host_platform", lambda: "linux-arm64")
    monkeypatch.chdir(tmp_path)
    return tree


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_fetch_prints_the_library_path_and_where_names_the_cache(env, tmp_path, capsys) -> None:
    code, out, err = run(capsys, "fetch", "26.8")
    assert code == 0, err
    path = Path(out.strip())
    assert (path / "libchtypes.so").read_bytes() == env.library_bytes
    assert str(path).startswith(str(tmp_path / "cache"))
    code, out, _ = run(capsys, "where")
    assert (code, out.strip()) == (0, str(tmp_path / "cache"))


def test_verify_passes_then_fails_when_the_library_changes(env, capsys) -> None:
    run(capsys, "fetch", "26.8")
    code, out, err = run(capsys, "verify")
    assert (code, out, err) == (0, "", "")
    library = Path(run(capsys, "fetch", "--offline", "26.8")[1].strip()) / "libchtypes.so"
    library.write_bytes(b"tampered")
    code, out, err = run(capsys, "verify")
    assert code == C.ERROR_EXIT_CODES["CHTYPES_ARTIFACT_CORRUPT"] and "FAILED" in err and out == ""


def test_list_shows_installed_and_only_version_tags(env, capsys) -> None:
    code, out, _ = run(capsys, "list")
    assert code == 0 and "installed" not in out
    assert "published 26.8 support unknown\n" in out
    assert "published 26.8.15.10 support unknown\n" in out
    assert "v26.8" not in out and "sha256-" not in out
    run(capsys, "fetch", "26.8")
    _, out, _ = run(capsys, "list", "--offline")
    assert out.startswith("installed 26.8.15.10 linux-arm64 ") and "published" not in out
    assert out.count("\n") == 1


def test_fetch_all_takes_every_published_line(env, capsys) -> None:
    code, out, err = run(capsys, "fetch", "--all")
    assert code == 0, err
    assert len(out.strip().splitlines()) == 1  # one line published (26.8), host platform


def test_lock_then_frozen_then_offline(env, tmp_path, capsys) -> None:
    code, _, err = run(capsys, "fetch", "26.8", "--lock", "chtypes.lock")
    assert code == 0, err
    assert json.loads((tmp_path / "chtypes.lock").read_text())["schema"] == C.LOCK_SCHEMA
    code, out, err = run(capsys, "fetch", "26.8", "--frozen")  # default lock file name
    assert code == 0, err
    code, out, err = run(capsys, "fetch", "--frozen", "--all")
    assert code == 0 and len(out.strip().splitlines()) == 1, err
    code, _, err = run(capsys, "fetch", "--offline", "26.8")
    assert code == 0, err


def test_update_needs_a_lock_and_refuses_frozen(env, tmp_path, capsys) -> None:
    assert run(capsys, "fetch", "26.8", "--update")[0] == 2
    assert run(capsys, "fetch", "26.8", "--update", "--lock", "l", "--frozen")[0] == 2
    assert run(capsys, "fetch", "26.8", "--update", "--lock", "l", "--offline")[0] == 2
    run(capsys, "fetch", "26.8", "--lock", "chtypes.lock")
    code, out, err = run(capsys, "fetch", "--update", "--lock", "chtypes.lock")
    assert code == 0 and len(out.strip().splitlines()) == 1, err


def test_cache_platform_and_version_flags(env, tmp_path, capsys) -> None:
    other = tmp_path / "other"
    code, out, _ = run(capsys, "where", "--cache", str(other))
    assert (code, out.strip()) == (0, str(other))
    code, out, err = run(
        capsys, "fetch", "26.8", "--cache", str(other), "--platform", "linux-arm64"
    )
    assert code == 0 and out.startswith(str(other)), err
    assert run(capsys, "fetch", "26.8", "--platform", "plan9-x")[0] == 2
    assert run(capsys, "verify", "--cache", str(other))[0] == 0
    code, out, _ = run(capsys, "--version")
    assert code == 0 and re.fullmatch(r"chtypes \S+\n", out) and "+unknown" not in out
    for command in ("verify", "list", "where"):
        assert run(capsys, command, "--platform", "linux-arm64")[0] == 2


def test_help_is_stdout_exit_zero(env, capsys) -> None:
    for argv in (("--help",), ("-h",), ("fetch", "--help"), ("list", "-h")):
        code, out, err = run(capsys, *argv)
        assert code == 0 and "usage:" in out and err == "", argv


def test_frozen_without_a_pin_is_artifact_pinned(env, capsys) -> None:
    code, _, err = run(capsys, "fetch", "26.8", "--frozen")
    assert code == C.ERROR_EXIT_CODES["CHTYPES_ARTIFACT_PINNED"] or "lock" in err
    assert code != 0


def test_offline_miss_is_artifact_missing(env, capsys) -> None:
    code, _, err = run(capsys, "fetch", "--offline", "26.8")
    assert code == C.ERROR_EXIT_CODES["CHTYPES_ARTIFACT_MISSING"]
    assert "CHTYPES_ARTIFACT_MISSING" in err


def test_untrusted_key_exits_with_the_table_status(env, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CHTYPES_TRUSTED_KEYS", "11" * 32)
    code, _, err = run(capsys, "fetch", "26.8")
    assert code == C.ERROR_EXIT_CODES["CHTYPES_ARTIFACT_UNTRUSTED"]
    assert "CHTYPES_ARTIFACT_UNTRUSTED" in err


def test_unpublished_line_exits_with_its_status(env, capsys) -> None:
    code, _, err = run(capsys, "fetch", "25.1")
    assert code == C.ERROR_EXIT_CODES["CHTYPES_ARTIFACT_UNPUBLISHED"]


@pytest.mark.parametrize("argv", [["fetch"], ["fetch", "v26.8"], ["fetch", "--all", "26.8"], []])
def test_usage_errors_exit_2(env, capsys, argv) -> None:
    code, _, _ = run(capsys, *argv)
    assert code == 2


@pytest.mark.parametrize("code_name", sorted(C.ERROR_EXIT_CODES))
def test_every_code_in_the_table_maps_to_its_status(code_name, monkeypatch, capsys) -> None:
    """The exit status of each error code is the table's, never a number held here."""
    if code_name == "CHTYPES_ARTIFACT_INCOMPATIBLE":
        error: Exception = ArtifactIncompatibleError("boom", reason="x")
    else:
        cls = {c.code: c for c in vars(fetch_errors).values() if _is_error(c)}[code_name]
        error = cls("boom")

    def raiser(_args):
        raise error

    monkeypatch.setitem(cli._COMMANDS, "where", raiser)
    code, _, err = run(capsys, "where")
    assert code == C.ERROR_EXIT_CODES[code_name]
    assert code_name in err


def _is_error(obj) -> bool:
    return (
        isinstance(obj, type)
        and issubclass(obj, fetch_errors.FetchError)
        and obj is not fetch_errors.FetchError
    )


def test_python_dash_m_runs_the_same_cli(tmp_path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "chtypes", "where"],
        capture_output=True,
        text=True,
        env={"CHTYPES_CACHE": str(tmp_path / "c"), "PATH": ""},
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(tmp_path / "c")


def _zero_x_registry(root: Path) -> None:
    for rel, body in {"26.1/manifest.json": "{}", "26.1/libchtypes.so": "0.x"}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)


def test_verify_of_nothing_says_so(env, tmp_path, capsys) -> None:
    """A verify that verified nothing says so on stderr, so an empty pass never
    looks like a good one, and a 0.x registry used as the cache is named
    (public issue #486)."""
    code, out, err = run(capsys, "verify")
    assert (code, out) == (0, "")
    assert f"chtypes: verified 0 builds under {tmp_path / 'cache'}\n" in err
    zero_x = tmp_path / "zero-x"
    _zero_x_registry(zero_x)
    hint = f"{zero_x} holds a 0.x registry (26.1/manifest.json); chtypes 1.x uses an OCI layout at "
    code, _, err = run(capsys, "verify", "--cache", str(zero_x))
    assert code == 0 and f"verified 0 builds under {zero_x}" in err and hint in err
    code, _, err = run(capsys, "fetch", "--offline", "26.1", "--cache", str(zero_x))
    assert code == C.ERROR_EXIT_CODES["CHTYPES_ARTIFACT_MISSING"] and hint in err
    assert not (zero_x / "oci-layout").exists()
