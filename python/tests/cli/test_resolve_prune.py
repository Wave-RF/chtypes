"""`chtypes resolve` (public issue #493) and `chtypes prune` (public issue #494)
over the conformance fixtures' `basic` tree, in-process through `main(argv)`,
under the v1 fetch contract (conftest.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chtypes import __main__ as cli

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "fetch-v1"
PLATFORMS = ("linux-amd64", "linux-arm64", "darwin-arm64")


@pytest.fixture
def basic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The basic tree as the base, its test key trusted, a fresh cache: the
    cache path, which nothing has created yet."""
    tree = FIXTURES / "trees" / "basic" / "v2" / "chtypes" / "v1"
    if not tree.is_dir():
        pytest.skip(f"SKIPPED: the fetch-v1 fixtures are not beside this checkout ({tree})")
    cache = tmp_path / "cache"
    monkeypatch.setenv("CHTYPES_ARTIFACTS_URL", f"file://{tree}")
    monkeypatch.setenv(
        "CHTYPES_TRUSTED_KEYS", (FIXTURES / "test-key" / "public.hex").read_text().strip()
    )
    monkeypatch.setenv("CHTYPES_CACHE", str(cache))
    for name in (
        "CHTYPES_ALLOW_UNSIGNED",
        "CHTYPES_OFFLINE",
        "CHTYPES_CACHE_STRICT",
        "CHTYPES_TARGET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "detect_host_platform", lambda: "linux-arm64")
    return cache


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.mark.parametrize(
    "argv",
    [
        ("resolve",),
        ("resolve", "26.8", "26.9"),
        ("resolve", "v26.8"),
        ("resolve", "26.8", "--platform", "linux-arm64"),
        ("prune", "26.8"),
        ("prune", "--keep", "0"),
        ("prune", "--keep", "x"),
        ("prune", "--line", "26.8.15"),
        ("prune", "--line", "v26.8"),
        ("prune", "--offline"),
        ("prune", "--platform", "linux-arm64"),
    ],
)
def test_usage_errors_exit_2(basic, capsys, argv) -> None:
    code, out, _ = run(capsys, *argv)
    assert (code, out) == (2, ""), argv


def test_resolve_names_every_platform_and_installs_nothing(basic: Path, capsys) -> None:
    code, out, err = run(capsys, "resolve", "26.8")
    assert code == 0, err
    lines = out.splitlines()
    assert len(lines) == 3, out
    for line, platform in zip(lines, PLATFORMS, strict=True):
        word, version, plat, build, manifest = line.split(" ")
        assert (word, version, plat, build) == (
            "resolved",
            "26.8.15.10",
            platform,
            "20261001.183455",
        )
        assert manifest.startswith("sha256:") and len(manifest) == 71
    assert not basic.exists(), "resolve created the cache"

    code, out, _ = run(capsys, "resolve", "--json", "26.8")
    assert code == 0 and out.endswith("]\n") and out.count("\n") == 1
    assert out.startswith(
        '[{"platform":"linux-amd64","version":"26.8.15.10","build":"20261001.183455","manifest":"sha256:'
    )
    rows = json.loads(out)
    assert [(r["version"], r["platform"], r["build"], r["manifest"]) for r in rows] == [
        tuple(line.split(" ")[1:]) for line in lines
    ]

    code, out, err = run(capsys, "resolve", "26.8", "--offline")
    assert (code, out) == (1, "") and "CHTYPES_ARTIFACT_MISSING" in err
    assert run(capsys, "fetch", "26.8")[0] == 0
    code, out, _ = run(capsys, "resolve", "26.8", "--offline")
    assert (code, out) == (0, lines[1] + "\n")
    code, _, err = run(capsys, "resolve", "1.1")
    assert code == 4 and "CHTYPES_ARTIFACT_UNPUBLISHED" in err


def test_prune_removes_the_superseded_build(basic: Path, capsys) -> None:
    dirs = {}
    for spelling in ("26.7.10.3", "26.7", "26.8"):
        code, out, err = run(capsys, "fetch", spelling)
        assert code == 0, err
        dirs[spelling] = out.strip()
    older = f"would-prune 26.7.10.3 linux-arm64 {dirs['26.7.10.3']}\n"

    code, out, err = run(capsys, "prune", "--dry-run")
    assert (code, out) == (0, older) and f"chtypes: would prune 1 build(s) under {basic}\n" in err
    assert Path(dirs["26.7.10.3"]).is_dir()
    assert run(capsys, "prune", "--line", "26.8")[:2] == (0, "")
    assert run(capsys, "prune", "--keep", "2")[:2] == (0, "")
    code, out, err = run(capsys, "prune", "--line", "26.7")
    assert (code, out) == (0, older.replace("would-prune", "pruned")) and "pruned 1 build(s)" in err
    assert not Path(dirs["26.7.10.3"]).exists()
    _, out, _ = run(capsys, "list", "--offline")
    assert (
        "26.7.10.3" not in out and "installed 26.7.15.5 " in out and "installed 26.8.15.10 " in out
    )
    assert run(capsys, "prune")[:2] == (0, "")
