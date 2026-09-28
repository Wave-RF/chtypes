"""`scripts/fetch.sh`, the reference implementation, against `two-revisions/`.

fetch.sh has no suite of its own, and the four bindings each drive the shared
fixtures in-process; this file runs the script itself, in a subprocess, over the
same `expected.json` `revisions` cases every binding runs (docs/guides/fetch.md
§9). Its revision is its documented `--abi-revision N`, so no test-only override
is involved. Every expected value is read from `expected.json`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FETCH_SH = REPO / "scripts" / "fetch.sh"
FIXTURES = REPO / "tests" / "fixtures" / "fetch"
EXPECTED_FILE = FIXTURES / "expected.json"

pytestmark = pytest.mark.skipif(
    not (EXPECTED_FILE.is_file() and FETCH_SH.is_file() and shutil.which("bash")),
    reason=f"scripts/fetch.sh or its fixtures are absent ({FETCH_SH}, {FIXTURES}), or bash is",
)


def _expected() -> dict:
    return json.loads(EXPECTED_FILE.read_text()) if EXPECTED_FILE.is_file() else {}


def _revisions() -> dict:
    return _expected().get("revisions") or {}


def _pick_at(revisions: dict, case: dict, rev: int) -> dict:
    if rev == revisions["low_revision"]:
        return case["at_low_revision"]
    if rev == revisions["high_revision"]:
        return case["at_high_revision"]
    raise AssertionError(f"revision {rev} is neither low nor high in expected.json")


def _fetch_sh(case: dict, rev: int, dest: Path) -> subprocess.CompletedProcess[str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CHTYPES_") and k != "XDG_CACHE_HOME"
    }
    env["CHTYPES_TRUSTED_KEYS"] = ",".join(_expected()["trusted_keys"])
    # Never the optional developer resolver beside a checkout: the fixture's
    # index.json is the only authority here.
    env["CHTYPES_CORE_DIR"] = str(dest / "no-core-here")
    env["XDG_CACHE_HOME"] = str(dest / "xdg")
    return subprocess.run(
        [
            "bash",
            str(FETCH_SH),
            case["line"],
            "--platform",
            case["platform"],
            "--url",
            f"file://{FIXTURES / _revisions()['fixture']}",
            "--dest",
            str(dest / "reg"),
            "--abi-revision",
            str(rev),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )


def _assert_fetched(case: dict, rev: int, dest: Path) -> None:
    revisions = _revisions()
    want = _pick_at(revisions, case, rev)
    assert want["abi_revision"] == rev
    proc = _fetch_sh(case, rev, dest)
    assert proc.returncode == 0, proc.stderr
    installed = Path(proc.stdout.strip())
    assert installed == dest / "reg" / case["line"]
    # The row it chose, by its own announcement, is expected.json's file ...
    assert f"==> {want['file']}  (" in proc.stderr, proc.stderr
    # ... and the bytes that landed are that row's library, per the index.
    index = json.loads((FIXTURES / revisions["fixture"] / "index.json").read_text())
    (row,) = [a for a in index["artifacts"] if a["file"] == want["file"]]
    assert (row["sha256"], row["build"], row["abi_revision"]) == (
        want["sha256"],
        want["build"],
        want["abi_revision"],
    )
    library = installed / row["library"]
    assert hashlib.sha256(library.read_bytes()).hexdigest() == row["library_sha256"]


@pytest.mark.parametrize(
    ("case", "which"),
    [(c, w) for c in _revisions().get("cases", []) for w in ("low_revision", "high_revision")],
    ids=lambda v: v if isinstance(v, str) else f"{v['platform']}-{v['line']}",
)
def test_fetch_sh_picks_the_row_at_each_revision(case: dict, which: str, tmp_path: Path) -> None:
    """Every case, at the low and at the high revision, through --abi-revision."""
    _assert_fetched(case, _revisions()[which], tmp_path)


@pytest.mark.parametrize(
    "discriminating",
    _revisions().get("discriminating_lines", []),
    ids=lambda d: f"abi{d['revision']}-{d['line']}",
)
def test_fetch_sh_filter_changes_the_answer(discriminating: dict, tmp_path: Path) -> None:
    """On each discriminating line, the pick at that revision differs from the
    unfiltered one — the filter decided it."""
    cases = [c for c in _revisions()["cases"] if c["line"] == discriminating["line"]]
    assert cases, f"no case for discriminating line {discriminating['line']}"
    for i, case in enumerate(cases):
        want = _pick_at(_revisions(), case, discriminating["revision"])
        assert want["file"] != case["unfiltered"]["file"]
        _assert_fetched(case, discriminating["revision"], tmp_path / str(i))


def test_fetch_sh_one_past_the_high_revision_is_unpublished(tmp_path: Path) -> None:
    """The negative control: exit 4, CHTYPES_ARTIFACT_UNPUBLISHED, nothing installed."""
    revisions = _revisions()
    assert revisions.get("cases"), "expected.json carries no revisions cases"
    case = revisions["cases"][0]
    proc = _fetch_sh(case, revisions["high_revision"] + 1, tmp_path)
    assert proc.returncode == 4, proc.stderr
    assert "CHTYPES_ARTIFACT_UNPUBLISHED" in proc.stderr
    assert f"at ABI revision {revisions['high_revision'] + 1} (from --abi-revision)" in proc.stderr
    assert proc.stdout == ""
    assert not (tmp_path / "reg" / case["line"]).exists()
