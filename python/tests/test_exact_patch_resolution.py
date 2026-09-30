"""SDK#284: exact-patch resolution, the two-level cache layout, and lock
schema 2 — tested offline against ``tests/fixtures/fetch/two-patches/``,
which serves two patches of ClickHouse 25.8 side by side (§8 of the design).

Every case below is read off the fixture's own ``expected.json`` ->
``patches`` block, never restated by hand: a suite that hand-set the older
and newer versions would be testing its author's belief about the fixture,
not the resolution.  Per the common #284 brief, this file MUST fail loudly
if ``expected.json`` carries no ``patches`` block, rather than skip.
"""

from __future__ import annotations

import hashlib
import json
import time
import warnings
from pathlib import Path

import pytest

import chtypes
from chtypes import fetch as fetch_module
from chtypes.fetch import (
    ensure,
    fetch_lines,
    installed_patches,
    read_lock,
    write_lock,
)
from chtypes.registry import _pick_line, _pick_patch

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "fetch"
EXPECTED_FILE = FIXTURES / "expected.json"
PLATFORM = "linux-arm64"  # published by every fixture, whatever host runs this

pytestmark = [
    pytest.mark.skipif(
        not EXPECTED_FILE.is_file(),
        reason=(
            f"no fetch fixtures at {FIXTURES}: tests/fixtures/fetch is generated in the core "
            f"repository (docs/guides/fetch.md §9) and must be present for this suite to run"
        ),
    ),
    pytest.mark.usefixtures("at_fixture_revision"),
]


def _expected() -> dict:
    return json.loads(EXPECTED_FILE.read_text())


def _patches_block() -> dict:
    """``expected.json`` -> ``patches``. FAILS LOUDLY if absent — this is not
    a skip condition, per the common #284 brief: a suite that quietly skipped
    would look green while proving nothing about exact-patch resolution."""
    doc = _expected()
    if "patches" not in doc:
        raise AssertionError(
            f"{EXPECTED_FILE} carries no 'patches' block. tests/fixtures/fetch/two-patches/ is "
            f"required for SDK#284 and must be regenerated from the served set (docs/guides/"
            f"fetch.md §9) — this is a hard failure, not a skip."
        )
    return doc["patches"]


def _case(platform: str = PLATFORM) -> dict:
    block = _patches_block()
    (case,) = [c for c in block["cases"] if c["platform"] == platform]
    return case


def _test_keys() -> list[str]:
    return list(_expected()["trusted_keys"])


def _url() -> str:
    return f"file://{FIXTURES / 'two-patches'}"


@pytest.fixture
def dest(isolated_search_path: Path, tmp_path: Path) -> Path:
    return tmp_path / "registry"


@pytest.fixture
def names() -> tuple[str, str, str, str]:
    """(line, older, newer, missing) — the fixture's own values, never typed
    in by hand."""
    block = _patches_block()
    return block["line"], block["served"][0], block["served"][1], block["exact_miss"]["requested"]


def _kw(dest: Path) -> dict:
    return dict(platform=PLATFORM, url=_url(), dest=dest, trusted_keys=_test_keys())


def _library_sha256(directory: Path) -> str:
    manifest = chtypes.read_manifest(directory)
    assert manifest is not None
    return hashlib.sha256((directory / manifest.library).read_bytes()).hexdigest()


# ------------------------------------------------------ layout rule: the case


def test_fixture_serves_two_patches_of_one_line(names: tuple[str, str, str, str]) -> None:
    """Pins the fixture's own shape, so every case below rests on something
    measured rather than assumed: two DISTINCT patches, both carrying a
    channel suffix, of the SAME line, with a strictly-between exact-miss."""
    line, older, newer, missing = names
    case = _case()
    assert chtypes.minor_of(older) == chtypes.minor_of(newer) == line
    assert "-" in older and "-" in newer  # both carry a channel
    assert case["patches"][older]["library_sha256"] != case["patches"][newer]["library_sha256"]
    # strictly between, not merely above both (proves "newest served", not
    # "numerically nearest")
    older_key = tuple(int(p) for p in older.split("-")[0].split("."))
    newer_key = tuple(int(p) for p in newer.split("-")[0].split("."))
    missing_key = tuple(int(p) for p in missing.split("-")[0].split("."))
    assert older_key < missing_key < newer_key


# --------------------------------------------------------- fetch: no fallback


def test_two_exact_fetches_do_not_interfere(dest: Path, names: tuple[str, str, str, str]) -> None:
    """Two exact-patch fetches into one empty destination each land under
    ``patches/<line>/<version>/`` (SDK#284 "Layout rule": neither is a LINE
    request, so neither takes the flat slot) with no interference — the
    second fetch does not touch the first."""
    line, older, newer, _missing = names
    case = _case()
    kw = _kw(dest)
    older_dir = ensure(older, **kw)
    assert older_dir == dest / "patches" / line / older
    older_sha = _library_sha256(older_dir)
    assert older_sha == case["patches"][older]["library_sha256"]

    newer_dir = ensure(newer, **kw)
    assert newer_dir == dest / "patches" / line / newer
    assert _library_sha256(newer_dir) == case["patches"][newer]["library_sha256"]
    # the first fetch is untouched
    assert _library_sha256(older_dir) == older_sha
    assert not (dest / line).exists()  # neither was a line request: flat stays empty


def test_line_fetch_installs_the_newest_patch_flat(
    dest: Path, names: tuple[str, str, str, str]
) -> None:
    """``fetch <line>`` installs the NEWEST served patch, flat — the patch a
    line request selects (SDK#284 "Layout rule")."""
    line, _older, newer, _missing = names
    case = _case()
    installed = ensure(line, **_kw(dest))
    assert installed == dest / line
    manifest = chtypes.read_manifest(installed)
    assert manifest is not None and manifest.clickhouse_version == newer
    assert _library_sha256(installed) == case["patches"][newer]["library_sha256"]


def test_channel_stripped_exact_fetch_matches_decision_7(
    dest: Path, names: tuple[str, str, str, str]
) -> None:
    """fetch.md Decision 7: a spelling with no channel matches that patch on
    any channel — ``fetch <older-without-its-channel>`` installs ``older``,
    not the newest."""
    _line, older, _newer, _missing = names
    bare = older.split("-", 1)[0]
    installed = ensure(bare, **_kw(dest))
    manifest = chtypes.read_manifest(installed)
    assert manifest is not None and manifest.clickhouse_version == older


def test_fetch_of_missing_patch_never_falls_back(
    dest: Path, names: tuple[str, str, str, str]
) -> None:
    """R5/R6: ``fetch``/``ensure`` are a hard requirement. Only the REGISTRY
    falls back within a line — a fetch of an unpublished exact patch is
    always ``CHTYPES_ARTIFACT_UNPUBLISHED``, never a quiet substitution."""
    _line, _older, _newer, missing = names
    with pytest.raises(chtypes.ArtifactUnpublishedError) as caught:
        ensure(missing, **_kw(dest))
    assert caught.value.code == "CHTYPES_ARTIFACT_UNPUBLISHED"
    assert not dest.exists()


# --------------------------------------------------------------- demotion


def test_line_fetch_demotes_the_outgoing_patch(
    dest: Path, names: tuple[str, str, str, str]
) -> None:
    """SDK#284 "Layout rule": when a line fetch changes which patch sits in
    the flat slot, the outgoing install is renamed — atomically, same
    filesystem, never deleted — into ``patches/<line>/<its version>/``
    BEFORE the incoming patch takes the flat slot.

    To get ``older`` into the flat slot in the first place (the fixture
    always serves both patches together, so a plain line fetch always picks
    ``newer``), a ``--frozen`` fetch pinning only ``older`` is used — itself
    a normal LINE request under the lock, per §6 F1.
    """
    line, older, newer, _missing = names
    kw = _kw(dest)
    case = _case()

    # older-only lock, schema 2, keyed by the exact patch.
    lock = dest.parent / "older-only.lock"
    write_lock(
        lock,
        {
            f"{PLATFORM}/{older}": {
                "file": case["patches"][older]["file"],
                "sha256": case["patches"][older]["sha256"],
                "abi_revision": chtypes.ABI_REVISION,
            }
        },
    )
    got = ensure(line, lock=lock, frozen=True, **kw)
    assert got == dest / line
    manifest = chtypes.read_manifest(got)
    assert manifest is not None and manifest.clickhouse_version == older
    older_sha = _library_sha256(got)

    # A plain (unfrozen) line fetch now picks `newer`, demoting `older`.
    got2 = ensure(line, **kw)
    assert got2 == dest / line
    manifest2 = chtypes.read_manifest(got2)
    assert manifest2 is not None and manifest2.clickhouse_version == newer

    demoted = dest / "patches" / line / older
    demoted_manifest = chtypes.read_manifest(demoted)
    assert demoted_manifest is not None and demoted_manifest.clickhouse_version == older
    assert _library_sha256(demoted) == older_sha
    assert _library_sha256(demoted) == case["patches"][older]["library_sha256"]

    # An exact request for `older` now resolves at `patches/`, offline — no fetch.
    offline_got = ensure(older, offline=True, **{**kw, "url": "file:///does-not-exist"})
    assert offline_got == demoted


# ------------------------------------------------------------------- §6 lock


def test_lock_records_both_patches_after_two_fetches(
    dest: Path, names: tuple[str, str, str, str]
) -> None:
    """§6: each fetch pins its OWN exact patch; nothing is removed."""
    line, older, newer, _missing = names
    kw = _kw(dest)
    lock = dest.parent / "chtypes.lock"
    ensure(older, lock=lock, **kw)
    ensure(line, lock=lock, **kw)  # installs `newer` flat
    pins = read_lock(lock)
    assert pins.keys() == {f"{PLATFORM}/{older}", f"{PLATFORM}/{newer}"}
    doc = json.loads(lock.read_text())
    assert doc["schema"] == fetch_module.LOCK_SCHEMA == 2


def test_frozen_with_lock_pinning_older_installs_older_while_newer_is_served(
    dest: Path, names: tuple[str, str, str, str]
) -> None:
    """The headline case (§8 P5): with a lock pinning only `older`, `fetch
    <line> --frozen` into an empty destination installs `older` while `newer`
    is served — never `CHTYPES_ARTIFACT_PINNED` for "the release offers
    something newer": selection never looks at unpinned rows any more."""
    line, older, _newer, _missing = names
    case = _case()
    lock = dest.parent / "pin-older.lock"
    write_lock(
        lock,
        {
            f"{PLATFORM}/{older}": {
                "file": case["patches"][older]["file"],
                "sha256": case["patches"][older]["sha256"],
            }
        },
    )
    got = ensure(line, lock=lock, frozen=True, **_kw(dest))
    assert got == dest / line
    manifest = chtypes.read_manifest(got)
    assert manifest is not None and manifest.clickhouse_version == older


def test_frozen_refuses_a_patch_the_lock_does_not_pin(
    dest: Path, names: tuple[str, str, str, str]
) -> None:
    _line, older, newer, _missing = names
    case = _case()
    lock = dest.parent / "pin-older.lock"
    write_lock(
        lock,
        {
            f"{PLATFORM}/{older}": {
                "file": case["patches"][older]["file"],
                "sha256": case["patches"][older]["sha256"],
            }
        },
    )
    ensure(older, lock=lock, frozen=True, **_kw(dest))
    with pytest.raises(chtypes.ArtifactPinnedError, match="pins nothing") as caught:
        ensure(newer, lock=lock, frozen=True, **_kw(dest))
    assert caught.value.code == "CHTYPES_ARTIFACT_PINNED"


def test_schema1_lock_converts_on_read_and_rewrites_as_schema2(
    dest: Path, names: tuple[str, str, str, str]
) -> None:
    """§6: a schema-1 lock (built at test time, keyed by LINE) still reads —
    the exact patch is recovered from the entry's own ``file`` name — and
    ``fetch <line> --lock`` on it rewrites the whole file as schema 2."""
    line, _older, newer, _missing = names
    case = _case()
    lock = dest.parent / "schema1.lock"
    lock.write_text(
        json.dumps(
            {
                "schema": 1,
                "artifacts": {
                    f"{PLATFORM}/{line}": {
                        "file": case["patches"][newer]["file"],
                        "sha256": case["patches"][newer]["sha256"],
                    }
                },
            }
        )
    )
    converted = read_lock(lock)
    assert converted.keys() == {f"{PLATFORM}/{newer}"}

    got = ensure(line, lock=lock, **_kw(dest))
    assert got == dest / line
    assert json.loads(lock.read_text())["schema"] == 2
    assert read_lock(lock).keys() == {f"{PLATFORM}/{newer}"}


def test_all_frozen_installs_only_what_the_lock_pins(
    dest: Path, names: tuple[str, str, str, str]
) -> None:
    """§6 F6: `--all --frozen` installs the newest PINNED patch of each line
    the lock pins for the platform — here, only `older` of `25.8`."""
    _line, older, _newer, _missing = names
    case = _case()
    lock = dest.parent / "pin-older.lock"
    write_lock(
        lock,
        {
            f"{PLATFORM}/{older}": {
                "file": case["patches"][older]["file"],
                "sha256": case["patches"][older]["sha256"],
            }
        },
    )
    got = fetch_lines(all_lines=True, lock=lock, frozen=True, **_kw(dest))
    assert len(got) == 1
    manifest = chtypes.read_manifest(got[0])
    assert manifest is not None and manifest.clickhouse_version == older


# ---------------------------------------------------------------- offline


def test_offline_channel_stripped_matches_and_missing_is_unreachable(
    dest: Path, names: tuple[str, str, str, str]
) -> None:
    line, older, newer, missing = names
    kw = _kw(dest)
    ensure(older, **kw)  # -> patches/25.8/<older>/
    ensure(line, **kw)  # -> 25.8/ (flat, holds `newer`)
    offline_kw = {**kw, "url": "file:///does-not-exist", "offline": True}

    # a channel-stripped offline request matches the installed `older`, at patches/
    bare_older = older.split("-", 1)[0]
    got = ensure(bare_older, **offline_kw)
    assert got == dest / "patches" / line / older

    # the bare line resolves to the flat slot's `newer`
    got_line = ensure(line, **offline_kw)
    assert got_line == dest / line

    # missing is CHTYPES_SOURCE_UNREACHABLE offline — never a fallback (R5)
    with pytest.raises(chtypes.SourceUnreachableError) as caught:
        ensure(missing, **offline_kw)
    assert caught.value.code == "CHTYPES_SOURCE_UNREACHABLE"


# ------------------------------------------------------------- fetch.installed_patches


def test_installed_patches_lists_both_flat_and_nested(
    dest: Path, names: tuple[str, str, str, str]
) -> None:
    line, older, newer, _missing = names
    kw = _kw(dest)
    ensure(older, **kw)  # patches/
    ensure(line, **kw)  # flat, `newer`
    patches = installed_patches(dest)
    by_version = {p.version: p for p in patches}
    assert set(by_version) == {older, newer}
    assert by_version[newer].flat is True
    assert by_version[newer].directory == dest / line
    assert by_version[older].flat is False
    assert by_version[older].directory == dest / "patches" / line / older
    assert all(p.minor == line for p in patches)


# ------------------------------------------------------- Registry.resolve()
#
# These need a library that actually `dlopen`s to prove a SUCCESSFUL
# fallback load, which `two-patches/`'s fixture "libraries" (a few bytes of
# text, like every fixture in this directory) cannot give — the same reason
# the design's P17/P18 ask for a REAL installed artifact rather than the
# fixture. They run against the session `registry` fixture (skips loudly, by
# name, when this machine has no real artifact — docs/guides/fetch.md §9) and
# need only ONE real installed line, not two: the "requested" side of the
# fallback is a made-up patch of that line's own minor, chosen so it cannot
# collide with a real published patch.


def _unlikely_patch(minor: str, tag: int) -> str:
    """A syntactically valid patch spelling of `minor` that is not, and will
    not become, a real ClickHouse release — used as the "requested" half of a
    fallback pair so this file's warnings never collide with another test's."""
    return f"{minor}.777.{tag}"


def test_registry_resolve_exact_miss_falls_back_and_warns_once(
    registry: chtypes.Registry,
) -> None:
    """SDK#284 §1/§3: a patch request no artifact publishes resolves to the
    newest INSTALLED patch of the line, `Exact=False`, and warns once per
    (requested, actual) pair — a second resolution for the SAME pair does not
    warn again."""
    exact_line = registry.versions()[-1]
    real = registry.for_version(exact_line)
    missing = _unlikely_patch(exact_line, 1)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        resolution = registry.resolve(missing)
    assert isinstance(resolution, chtypes.Resolution)
    assert resolution.requested == missing
    assert resolution.version == real.version
    assert resolution.exact is False
    assert resolution.library is real
    fallback_warnings = [w for w in caught if issubclass(w.category, chtypes.PatchFallbackWarning)]
    assert len(fallback_warnings) == 1
    assert missing in str(fallback_warnings[0].message)
    assert real.version in str(fallback_warnings[0].message)

    # A second resolve() for the SAME pair does not warn again.
    with warnings.catch_warnings(record=True) as caught2:
        warnings.simplefilter("always")
        resolution2 = registry.resolve(missing)
    assert resolution2.version == real.version
    assert not [w for w in caught2 if issubclass(w.category, chtypes.PatchFallbackWarning)]

    # An exact hit never warns and is Exact=True.
    with warnings.catch_warnings(record=True) as caught3:
        warnings.simplefilter("always")
        exact_resolution = registry.resolve(real.version)
    assert exact_resolution.exact is True
    assert exact_resolution.library is real
    assert not [w for w in caught3 if issubclass(w.category, chtypes.PatchFallbackWarning)]

    # A line request is always Exact=True.
    line_resolution = registry.resolve(exact_line)
    assert line_resolution.exact is True
    assert line_resolution.library is real
    assert registry.for_version(exact_line) is real


def test_patch_fallback_warning_records_pair_before_raising(registry: chtypes.Registry) -> None:
    """Design risk R-h: the (requested, actual) pair is recorded BEFORE the
    warning is raised, so a caller's own filter turning the warning into an
    exception does not cause a retry to warn (and raise) again — the
    fallback library has already loaded by the time the warning fires."""
    exact_line = registry.versions()[-1]
    real = registry.for_version(exact_line)
    missing = _unlikely_patch(exact_line, 2)

    with warnings.catch_warnings():
        warnings.simplefilter("error", chtypes.PatchFallbackWarning)
        with pytest.raises(chtypes.PatchFallbackWarning):
            registry.resolve(missing)
        # The pair is already recorded: a retry returns the library with NO
        # second warning (which, under this filter, would again raise).
        resolution = registry.resolve(missing)
    assert resolution.library is real
    assert resolution.exact is False


# ------------------------------------------------------------------ R-c


def test_fallback_recheck_is_bounded_to_60_seconds(
    registry: chtypes.Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-c: a fallen-back patch request re-checks the search path at most
    once every 60 s per (destination, requested patch) per process — between
    checks, the previously-resolved fallback is reused with no directory
    read."""
    exact_line = registry.versions()[-1]
    real = registry.for_version(exact_line)
    missing = _unlikely_patch(exact_line, 3)

    now = [1_000_000.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])  # shared with chtypes.registry's `time`

    calls = {"n": 0}
    real_iter = fetch_module._iter_patch_dirs

    def counting(root, minor):  # noqa: ANN001 - test shim
        calls["n"] += 1
        yield from real_iter(root, minor)

    monkeypatch.setattr("chtypes.registry._iter_patch_dirs", counting)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", chtypes.PatchFallbackWarning)
        resolution = registry.resolve(missing)
        assert resolution.library is real
        first_calls = calls["n"]
        assert first_calls > 0

        # 30s later: within the window, no new directory read.
        now[0] += 30
        registry.resolve(missing)
        assert calls["n"] == first_calls

        # 61s after the FIRST check: the window has passed, a fresh check runs.
        now[0] += 31
        registry.resolve(missing)
        assert calls["n"] > first_calls


# --------------------------------------------------------------- P11 / P19
# Resolution without dlopen, over directories built from fake manifests at
# test time — the "resolution seam" (docs/guides/fetch.md §9), so this needs
# no fixture, no real artifact, and cannot skip.


def _stand_in(root: Path, minor: str, version: str, *, nested: bool = False) -> Path:
    if nested:
        sub = root / "patches" / minor / version
    else:
        sub = root / minor
    sub.mkdir(parents=True)
    (sub / "libchtypes.so").write_bytes(b"not a shared library")
    (sub / "manifest.json").write_text(
        json.dumps(
            {
                "library": "libchtypes.so",
                "clickhouse_version": version,
                "clickhouse_minor": minor,
            }
        )
    )
    return sub


def test_resolution_seam_over_fake_manifests(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    # root_a: only a flat 25.8 at an OLDER patch. root_b: the same line
    # nested at a NEWER patch, plus another line entirely.
    _stand_in(root_a, "25.8", "25.8.1.1-lts")
    _stand_in(root_b, "25.8", "25.8.9.9-lts", nested=True)
    _stand_in(root_b, "26.7", "26.7.1.1-stable")

    search_path = (root_a, root_b)

    # A line resolves to its newest patch, over the WHOLE search path (R3):
    # the first directory holding ANY patch of 25.8 is root_a, so its own
    # single patch wins even though root_b holds a newer one — R3 does not
    # cross directories once the first has something.
    found = _pick_line(search_path, "25.8")
    assert found == (root_a / "25.8", "25.8.1.1-lts")

    # A matching EXACT patch is found anywhere on the path, not only in the
    # first directory that holds the line (R4 step 2) — root_b's nested
    # install, even though root_a is first and holds a DIFFERENT patch.
    found_patch = _pick_patch(search_path, "25.8", "25.8.9.9-lts")
    assert found_patch == (root_b / "patches" / "25.8" / "25.8.9.9-lts", "25.8.9.9-lts")
    # a channel-stripped spelling matches under Decision 7
    assert _pick_patch(search_path, "25.8", "25.8.9.9") == found_patch

    # another line entirely is not found at all
    assert _pick_line(search_path, "99.1") is None
    assert _pick_patch(search_path, "99.1", "99.1.1.1") is None


def test_registry_holding_only_nested_installs_is_not_empty(
    tmp_path: Path, isolated_search_path: Path
) -> None:
    """A registry directory that holds ONLY `patches/` installs (no flat
    slot at all) is not "empty" at construction, and resolves the line to
    its one nested patch. ``isolated_search_path`` keeps this machine's own
    real artifact cache off the search path, so `versions()` sees only what
    this test built."""
    root = tmp_path / "registry"
    root.mkdir()
    _stand_in(root, "25.8", "25.8.9.9-lts", nested=True)
    registry = chtypes.Registry(root)
    assert registry.versions() == ("25.8",)
    assert "25.8" in registry
    assert "25.8.9.9-lts" in registry
    with pytest.raises(chtypes.ChtypesError) as caught:
        registry.for_version("25.8")  # found, then refused by dlopen: a fake library
    assert not isinstance(caught.value, chtypes.ArtifactMissingError)
