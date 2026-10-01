"""`scripts/fetch.sh`, the reference implementation, against `two-revisions/`
and, below, `two-patches/` (chtypes#284).

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


# ─────────────────────────────────────────────────────────────────────────
# two-patches/ (chtypes#284): exact-patch resolution, and the layout rule —
# the patch a LINE request selects installs FLAT at <dest>/<line>/; any OTHER
# exact patch installs at <dest>/patches/<line>/<clickhouse_version>/, and a
# line fetch that changes the flat occupant DEMOTES the outgoing one there,
# atomically, never deleting it (issue #284, "Layout rule: approved, with one
# amendment"). fetch.sh itself has no lock support and no registry-style
# fallback: an exact patch the release does not publish is always
# CHTYPES_ARTIFACT_UNPUBLISHED (design §7, R5) — never a same-line substitute.


def _patches() -> dict:
    """The `patches` block `tests/fixtures/fetch/two-patches/` publishes to
    `expected.json`. Fails loudly rather than skip: a suite that quietly ran
    nothing here would be worse than no suite (chtypes#284's implementer
    brief)."""
    doc = _expected()
    if "patches" not in doc:
        raise AssertionError(
            f"{EXPECTED_FILE} carries no 'patches' block — regenerate the fixtures from the "
            f"artifact producer's current set; chtypes#284's two-patches/ suite has nothing to "
            f"drive"
        )
    return doc["patches"]


def _run_fetch_sh(
    spelling: str,
    platform: str,
    dest: Path,
    *,
    fixture: str = "two-patches",
    abi_revision: int | None = None,
    extra_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CHTYPES_") and k != "XDG_CACHE_HOME"
    }
    env["CHTYPES_TRUSTED_KEYS"] = ",".join(_expected()["trusted_keys"])
    # Never the optional developer resolver beside a checkout: index.json is
    # the only authority here.
    env["CHTYPES_CORE_DIR"] = str(dest / "no-core-here")
    env["XDG_CACHE_HOME"] = str(dest / "xdg")
    if abi_revision is None:
        abi_revision = _patches()["abi_revision"]
    return subprocess.run(
        [
            "bash",
            str(FETCH_SH),
            spelling,
            "--platform",
            platform,
            "--url",
            f"file://{FIXTURES / fixture}",
            "--dest",
            str(dest / "reg"),
            "--abi-revision",
            str(abi_revision),
            *extra_args,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )


def _manifest_version(directory: Path) -> str | None:
    manifest = directory / "manifest.json"
    if not manifest.is_file():
        return None
    return json.loads(manifest.read_text()).get("clickhouse_version")


@pytest.mark.parametrize(
    "case",
    _patches().get("cases", []),
    ids=lambda c: c["platform"],
)
def test_fetch_sh_exact_patch_always_installs_nested(case: dict, tmp_path: Path) -> None:
    """ONLY a line spelling (or --all) writes the flat <dest>/<line>/ slot
    (chtypes#284, cross-binding placement rule, matching Go and Python). An
    exact-patch spelling ALWAYS installs at patches/<line>/<version>/ instead
    — on an empty destination, for EVERY served patch of the line, including
    the one that happens to be newest. See
    test_fetch_sh_exact_newest_patch_never_writes_the_empty_flat_slot below
    for that case asserted on its own, with the flat slot's absence checked
    explicitly."""
    rows = case["patches"]
    for i, (version, row) in enumerate(rows.items()):
        proc = _run_fetch_sh(version, case["platform"], tmp_path / str(i))
        assert proc.returncode == 0, proc.stderr
        installed = Path(proc.stdout.strip())
        assert installed == tmp_path / str(i) / "reg" / "patches" / case["line"] / version, (
            f"an exact request for {version} must install nested under patches/, "
            f"never the flat slot, whether or not it is the line's newest: {installed}"
        )
        library = installed / (json.loads((installed / "manifest.json").read_text())["library"])
        assert hashlib.sha256(library.read_bytes()).hexdigest() == row["library_sha256"]


def test_fetch_sh_exact_newest_patch_never_writes_the_empty_flat_slot(tmp_path: Path) -> None:
    """The headline case of the cross-binding placement rule: an exact
    request for the line's newest served patch, into a destination with
    NOTHING installed yet, still installs at patches/<line>/<version>/ — the
    flat slot stays absent rather than being created. Only a line spelling
    or --all ever writes it."""
    patches = _patches()
    case = patches["cases"][0]
    newest = patches["newest"]
    proc = _run_fetch_sh(newest, case["platform"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    installed = Path(proc.stdout.strip())
    assert installed == tmp_path / "reg" / "patches" / case["line"] / newest
    assert not (tmp_path / "reg" / case["line"]).exists(), (
        "the flat slot must not exist after an EXACT request, even for the line's "
        "newest patch into an empty destination"
    )
    library = installed / (json.loads((installed / "manifest.json").read_text())["library"])
    assert (
        hashlib.sha256(library.read_bytes()).hexdigest()
        == case["patches"][newest]["library_sha256"]
    )


def test_fetch_sh_line_request_ignores_a_non_newest_resolver_hint(tmp_path: Path) -> None:
    """chtypes#294: a LINE spelling must always select the line's newest
    published patch at this SDK's revision, even when the optional
    developer resolver (the `CHTYPES_CORE_DIR` path) names a different,
    older, but still-published patch as "the current upstream patch". The
    resolver's hint may surface in the install-time note; it must never
    narrow which row fetch.sh selects.

    Drives fetch.sh's own developer-resolver discovery (`find
    "$CHTYPES_CORE_DIR" -maxdepth 2 -name resolve-version.py`) with a stub
    resolver written into a tmp dir for this test alone — no sibling
    checkout required."""
    patches = _patches()
    case = patches["cases"][0]
    newest = patches["newest"]
    older = next(v for v in case["patches"] if v != newest)

    core_dir = tmp_path / "stub-core"
    core_dir.mkdir()
    (core_dir / "resolve-version.py").write_text(
        f"import json, sys\nprint(json.dumps({{'line': {case['line']!r}, 'exact': {older!r}}}))\n"
    )

    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CHTYPES_") and k != "XDG_CACHE_HOME"
    }
    env["CHTYPES_TRUSTED_KEYS"] = ",".join(_expected()["trusted_keys"])
    env["CHTYPES_CORE_DIR"] = str(core_dir)
    env["XDG_CACHE_HOME"] = str(tmp_path / "xdg")
    proc = subprocess.run(
        [
            "bash",
            str(FETCH_SH),
            case["line"],
            "--platform",
            case["platform"],
            "--url",
            f"file://{FIXTURES / patches['fixture']}",
            "--dest",
            str(tmp_path / "reg"),
            "--abi-revision",
            str(patches["abi_revision"]),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    installed = Path(proc.stdout.strip())
    assert installed == tmp_path / "reg" / case["line"]
    assert _manifest_version(installed) == newest, (
        f"a line request must install the line's newest published patch ({newest}) "
        f"even though the stub resolver suggested the older, still-published {older}"
    )
    library = installed / (json.loads((installed / "manifest.json").read_text())["library"])
    assert (
        hashlib.sha256(library.read_bytes()).hexdigest()
        == case["patches"][newest]["library_sha256"]
    )
    # The resolver's hint still reaches the operator, as a note -- it is
    # never a silent substitution.
    assert f"line {case['line']} points at {older} upstream today" in proc.stderr, proc.stderr


def test_fetch_sh_exact_request_is_a_noop_when_already_flat(tmp_path: Path) -> None:
    """The one exception to 'only a line spelling writes the flat slot': it
    is not a write at all. When the requested exact patch ALREADY sits in
    the flat slot and verifies — as a line fetch, or an older SDK, would
    have left it — the exact request is a no-op that reports the flat
    directory, never a second copy under patches/."""
    patches = _patches()
    case = patches["cases"][0]
    newest = patches["newest"]
    dest = tmp_path / "reg"

    # Seed the flat slot directly with the newest patch, exactly as a LINE
    # fetch would have left it (fetch.sh's own line-spelling path is exactly
    # what does this in practice; seeding it directly keeps this test's
    # assertion about the EXACT-request path independent of that one).
    seed = _run_fetch_sh(newest, case["platform"], tmp_path / "seed")
    assert seed.returncode == 0, seed.stderr
    seeded_dir = Path(seed.stdout.strip())
    line_dir = dest / case["line"]
    dest.mkdir(parents=True)
    shutil.copytree(seeded_dir, line_dir)
    assert _manifest_version(line_dir) == newest

    proc = _run_fetch_sh(newest, case["platform"], tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "already installed and verified" in proc.stderr, proc.stderr
    installed = Path(proc.stdout.strip())
    assert installed == line_dir, (
        "an exact request already satisfied by the flat slot must report it, not patches/"
    )
    assert not (dest / "patches").exists(), "the no-op must not create patches/ at all"


@pytest.mark.parametrize(
    "case",
    _patches().get("cases", []),
    ids=lambda c: c["platform"],
)
def test_fetch_sh_channel_less_spelling_matches_the_installed_patch(
    case: dict, tmp_path: Path
) -> None:
    """fetch.md Decision 7: a patch spelled without its channel matches that
    patch on any channel — `scripts/fetch.sh:724` compared by string equality
    before chtypes#284 and refused every channel-less spelling with exit 4."""
    rows = case["patches"]
    for i, (version, row) in enumerate(rows.items()):
        bare = version.rsplit("-", 1)[0]
        assert bare != version, f"fixture patch {version!r} carries no channel to strip"
        proc = _run_fetch_sh(bare, case["platform"], tmp_path / str(i))
        assert proc.returncode == 0, proc.stderr
        installed = Path(proc.stdout.strip())
        library = installed / (json.loads((installed / "manifest.json").read_text())["library"])
        assert hashlib.sha256(library.read_bytes()).hexdigest() == row["library_sha256"]
        # And it is read back as the SAME exact patch it matched, never the
        # line's other served patch.
        assert _manifest_version(installed) == version


def test_fetch_sh_exact_miss_never_falls_back(tmp_path: Path) -> None:
    """fetch.sh has no lock support and no registry-style fallback (design
    §7, R5): `fetch <exact patch>` is always a hard requirement. A miss is
    CHTYPES_ARTIFACT_UNPUBLISHED, exit 4, and nothing is installed — never a
    quiet substitute of the line's newest served patch, unlike a registry's
    `For`/`Ensure`."""
    patches = _patches()
    case = patches["cases"][0]
    requested = patches["exact_miss"]["requested"]
    proc = _run_fetch_sh(requested, case["platform"], tmp_path)
    assert proc.returncode == 4, proc.stderr
    assert "CHTYPES_ARTIFACT_UNPUBLISHED" in proc.stderr
    assert requested in proc.stderr
    assert proc.stdout == ""
    assert not (tmp_path / "reg").exists() or not any((tmp_path / "reg").iterdir())


def test_fetch_sh_line_fetch_demotes_the_outgoing_patch(tmp_path: Path) -> None:
    """The layout rule's demotion (issue #284): when a line fetch changes
    which patch occupies the flat slot, the outgoing patch is renamed into
    patches/<line>/<its version>/ — never deleted — before the incoming
    patch takes the flat slot. Verified: the demoted bytes are byte-identical
    to a fresh fetch of that same exact patch, and a later exact request for
    it resolves there with no download (a corrupt/missing tarball URL would
    fail the fetch if one were attempted)."""
    patches = _patches()
    case = patches["cases"][0]
    newest = patches["newest"]
    older = next(v for v in case["patches"] if v != newest)
    older_row = case["patches"][older]

    dest = tmp_path / "reg"
    # Seed the flat slot with the OLDER patch directly — simulating a state
    # from before the newer patch was published (or an older SDK's install):
    # fetching the line always selects the release's newest row, so there is
    # no sequence of fetch.sh calls against this fixture alone that leaves
    # the older patch sitting in the flat slot.
    seed = _run_fetch_sh(older, case["platform"], tmp_path / "seed")
    assert seed.returncode == 0, seed.stderr
    seeded_dir = Path(seed.stdout.strip())
    dest.mkdir(parents=True)
    line_dir = dest / case["line"]
    shutil.copytree(seeded_dir, line_dir)
    assert _manifest_version(line_dir) == older

    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CHTYPES_") and k != "XDG_CACHE_HOME"
    }
    env["CHTYPES_TRUSTED_KEYS"] = ",".join(_expected()["trusted_keys"])
    env["CHTYPES_CORE_DIR"] = str(tmp_path / "no-core-here")
    env["XDG_CACHE_HOME"] = str(tmp_path / "xdg")
    proc = subprocess.run(
        [
            "bash",
            str(FETCH_SH),
            case["line"],
            "--platform",
            case["platform"],
            "--url",
            f"file://{FIXTURES / patches['fixture']}",
            "--dest",
            str(dest),
            "--abi-revision",
            str(patches["abi_revision"]),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert f"demoting {case['line']}" in proc.stderr, proc.stderr

    installed = Path(proc.stdout.strip())
    assert installed == line_dir
    assert _manifest_version(line_dir) == newest

    demoted_dir = dest / "patches" / case["line"] / older
    assert _manifest_version(demoted_dir) == older
    demoted_manifest = json.loads((demoted_dir / "manifest.json").read_text())
    demoted_library = demoted_dir / demoted_manifest["library"]
    assert hashlib.sha256(demoted_library.read_bytes()).hexdigest() == older_row["library_sha256"]

    # An exact request for the demoted patch resolves there without a fetch:
    # "already installed and verified" is the same claim the ordinary
    # already-installed check makes, now made against the demoted directory.
    again = subprocess.run(
        [
            "bash",
            str(FETCH_SH),
            older,
            "--platform",
            case["platform"],
            "--url",
            f"file://{FIXTURES / patches['fixture']}",
            "--dest",
            str(dest),
            "--abi-revision",
            str(patches["abi_revision"]),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )
    assert again.returncode == 0, again.stderr
    assert "already installed and verified" in again.stderr, again.stderr
    assert Path(again.stdout.strip()) == demoted_dir
