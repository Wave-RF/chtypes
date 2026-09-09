"""Level 1 — ABI conformance, asserted on outputs rather than on exit codes."""

from __future__ import annotations

import hashlib
import json
import os
import resource
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import chtypes
from chtypes import Format, Outcome
from chtypes._native import NativeLibrary
from chtypes.fetch import cache_registry_dir


def test_library_file_name_comes_from_the_manifest(registry: chtypes.Registry) -> None:
    # Not from a hard-coded name, not from a glob, not from the platform. The
    # Linux artifacts in this tree still ship the historical `libchtypes_s1.so`.
    for lib in registry.libraries():
        assert Path(lib.path).name == lib.manifest.library
        assert Path(lib.path).is_file()


def test_library_names_itself(registry: chtypes.Registry) -> None:
    for lib in registry.libraries():
        # chs_clickhouse_version(), cross-checked against the manifest: nothing
        # is inferred from the directory or the file name.
        assert lib.version == lib.manifest.clickhouse_version
        assert lib.minor == chtypes.minor_of(lib.version)
        assert lib.minor == ".".join(lib.version.split(".")[:2])
        # The directory name is informational; the reported version is the truth.
        assert Path(lib.path).parent.name == lib.minor


def test_several_versions_coexist_in_one_process(registry: chtypes.Registry) -> None:
    libraries = registry.libraries()
    if len(libraries) < 2:
        pytest.skip(f"{registry.directory} holds only {len(libraries)} artifact")
    assert len({lib.version for lib in libraries}) == len(libraries)
    # Each one answers for itself, from its own ClickHouse.
    for lib in libraries:
        with lib.compile_ddl("x UInt8") as schema:
            result = schema.row(Format.JSON_EACH_ROW, b'{"x":7}')
        assert result.outcome is chtypes.Outcome.ACCEPTED
        assert result.value("x").text == "7"


def test_version_lookup_accepts_a_minor_line_and_an_exact_patch(
    registry: chtypes.Registry,
) -> None:
    for lib in registry.libraries():
        assert registry.for_version(lib.minor) is lib
        assert registry.for_version(lib.version) is lib
        # A drifted docker tag resolves to its minor line rather than silently
        # losing a whole version column.
        drifted = f"{lib.minor}.999.1"
        assert registry.for_version(drifted) is lib
        assert drifted in registry


def test_unresolvable_version_is_the_one_missing_artifact_error(
    registry: chtypes.Registry,
) -> None:
    """docs/fetch.md §7: one identifiable error, one message, verbatim."""
    with pytest.raises(chtypes.ArtifactMissingError) as caught:
        registry.for_version("99.1")
    err = caught.value
    assert isinstance(err, chtypes.RegistryError)  # `except RegistryError` still works
    assert err.code == chtypes.CODE_ARTIFACT_MISSING == "CHTYPES_ARTIFACT_MISSING"
    assert err.line == "99.1"
    assert err.platform == chtypes.host_platform()
    assert err.looked_in == tuple(str(p) for p in registry.search_path)
    assert str(err) == (
        f"chtypes: no artifact for ClickHouse 99.1 ({chtypes.host_platform()}). "
        f"Looked in: {', '.join(err.looked_in)}.\n"
        f"Install it:  python -m chtypes fetch 99.1\n"
        f"or set CHTYPES_AUTOFETCH=1 to fetch on first use."
    )
    # A drifted patch of a missing line names the LINE, which is what fetch takes.
    with pytest.raises(chtypes.ArtifactMissingError, match="fetch 99.1\n"):
        registry.for_version("99.1.2.3")
    # An empty version never means "pick one" on the registry path.
    with pytest.raises(chtypes.RegistryError):
        registry.for_version("")


def test_versions_are_ordered_by_release_not_by_string(registry: chtypes.Registry) -> None:
    versions = registry.versions()
    assert versions == tuple(sorted(set(versions), key=lambda v: [int(p) for p in v.split(".")]))
    if "25.8" in versions and "25.10" in versions:
        # 25.10 is a LATER minor than 25.8; a string sort would invert them.
        assert versions.index("25.8") < versions.index("25.10")


def test_registry_walks_the_search_path(
    monkeypatch: pytest.MonkeyPatch, isolated_search_path: Path, tmp_path: Path
) -> None:
    """docs/fetch.md §1: explicit path, $CHTYPES_REGISTRY, the cache, the
    system locations — in that order; fetch writes to the first of the
    first three. `Registry()` no longer needs anything set (pre-1.0 change)."""
    cache = isolated_search_path
    reg = chtypes.Registry()
    assert reg.search_path == (cache,)  # system roots are patched out here
    assert reg.directory == cache
    assert reg.versions() == ()

    env_dir = tmp_path / "env-registry"
    monkeypatch.setenv(chtypes.ENV_REGISTRY, str(env_dir))
    reg = chtypes.Registry()
    assert reg.search_path == (env_dir, cache)
    assert reg.directory == env_dir

    explicit = tmp_path / "explicit"
    reg = chtypes.Registry(explicit)
    assert reg.search_path == (explicit, env_dir, cache)
    assert reg.directory == explicit
    # Explicit == env: de-duplicated, order kept.
    assert chtypes.Registry(env_dir).search_path == (env_dir, cache)
    # The public helper is the same walk, with the reserved system slots.
    monkeypatch.setattr(
        "chtypes.fetch.SYSTEM_REGISTRY_ROOTS", ("/usr/local/share/chtypes/artifacts",)
    )
    assert chtypes.registry_search_path(explicit) == (
        explicit,
        env_dir,
        cache,
        Path("/usr/local/share/chtypes/artifacts") / chtypes.host_platform(),
    )
    assert chtypes.fetch_destination() == env_dir  # never a system location
    # Another platform's artifacts never go into $CHTYPES_REGISTRY — this host
    # dlopens from it — but to that platform's own cache directory.
    foreign = "linux-amd64" if chtypes.host_platform() != "linux-amd64" else "linux-arm64"
    assert chtypes.fetch_destination(platform=foreign) == Path(cache_registry_dir(foreign))
    assert env_dir not in chtypes.registry_search_path(platform=foreign)
    assert chtypes.fetch_destination(tmp_path / "x", platform=foreign) == tmp_path / "x"


def test_registry_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch, registry: chtypes.Registry
) -> None:
    monkeypatch.setenv(chtypes.ENV_REGISTRY, str(registry.directory))
    assert chtypes.Registry().versions() == registry.versions()


def test_a_directory_without_a_manifest_is_not_a_version(
    tmp_path: Path, isolated_search_path: Path
) -> None:
    assert chtypes.read_manifest(tmp_path) is None
    (tmp_path / "manifest.json").write_text("not json at all")
    assert chtypes.read_manifest(tmp_path) is None
    (tmp_path / "manifest.json").write_text('{"clickhouse_version": "25.8.1"}')
    assert chtypes.read_manifest(tmp_path) is None  # no `library` field
    scratch = tmp_path / "25.8"
    scratch.mkdir()
    (scratch / "manifest.json").write_text('{"clickhouse_version": "25.8.1"}')
    # An empty registry is not an error at construction any more — the §7
    # error comes at open time, naming every directory searched.
    reg = chtypes.Registry(tmp_path)
    assert reg.versions() == ()
    assert "25.8" not in reg
    with pytest.raises(chtypes.ArtifactMissingError) as caught:
        reg.for_version("25.8")
    assert caught.value.looked_in == (str(tmp_path), str(isolated_search_path))


def test_manifest_ignores_unknown_fields(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps({"library": "libchtypes.so", "invented_later": 1})
    )
    manifest = chtypes.read_manifest(tmp_path)
    assert manifest is not None
    assert manifest.library == "libchtypes.so"


def test_verify_library_checks_the_bytes(tmp_path: Path) -> None:
    payload = b"not really a library"
    (tmp_path / "fake.so").write_bytes(payload)
    good = {
        "library": "fake.so",
        "library_bytes": len(payload),
        "library_sha256": hashlib.sha256(payload).hexdigest(),
    }
    (tmp_path / "manifest.json").write_text(json.dumps(good))
    chtypes.verify_library(tmp_path)  # passes

    (tmp_path / "manifest.json").write_text(json.dumps({**good, "library_sha256": "00" * 32}))
    with pytest.raises(chtypes.RegistryError, match="sha256"):
        chtypes.verify_library(tmp_path)

    (tmp_path / "manifest.json").write_text(json.dumps({**good, "library_bytes": 999}))
    with pytest.raises(chtypes.RegistryError, match="bytes"):
        chtypes.verify_library(tmp_path)


def test_verify_a_real_artifact(registry: chtypes.Registry) -> None:
    # The one integrity check that means anything, run against the real bytes: a
    # move that reported success and truncated a 232 MB library looks identical
    # to one that worked.
    smallest = min(registry.libraries(), key=lambda lib: lib.manifest.library_bytes or 1 << 62)
    chtypes.verify_library(Path(smallest.path).parent)


def test_returned_strings_are_freed(newest: chtypes.Library) -> None:
    """Every `char *` the library returns is released with that library's chs_free.

    A leak is asserted on an output, not assumed from a code read: 600 batches of
    100 rows return roughly 30 MB of documents, so forgetting `chs_free` shows up
    as tens of megabytes of resident growth.
    """
    body = b"".join(b'{"x":%d,"s":"hello world"}\n' % (i % 300) for i in range(100))
    with newest.compile_ddl("x UInt8, s String") as schema:
        for _ in range(50):  # warm up: interpreter and allocator arenas settle
            schema.rows(Format.JSON_EACH_ROW, body)
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        for _ in range(600):
            schema.rows(Format.JSON_EACH_ROW, body)
        after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    scale = 1 if sys.platform == "darwin" else 1024  # ru_maxrss: bytes vs KiB
    growth_mb = (after - before) * scale / 1e6
    assert growth_mb < 8, f"resident set grew {growth_mb:.1f} MB over 600 batches"


def test_missing_optional_symbols_degrade_to_unsupported(
    newest: chtypes.Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An artifact that predates a feature must decline, never fail to load."""
    native = newest._native
    with newest.compile_ddl("ts DateTime, v UInt8") as schema:
        monkeypatch.delitem(native._fn, "chs_schema_engine")
        monkeypatch.delitem(native._fn, "chs_schema_ttl")
        monkeypatch.delitem(native._fn, "chs_row")
        with pytest.raises(chtypes.UnsupportedError, match="predates engine support") as engine:
            schema.set_engine("SummingMergeTree", "ts")
        # The peer decline type: never a SchemaError, no `.code`, and the
        # frozen [-2] rendering whatever sentinel the shim saw internally.
        assert not isinstance(engine.value, chtypes.SchemaError)
        assert "[-2]" in str(engine.value)
        with pytest.raises(chtypes.UnsupportedError, match="predates TTL support"):
            schema.set_ttl("ts + INTERVAL 1 DAY")
        with pytest.raises(chtypes.UnsupportedError, match="predates chs_row"):
            schema.row(Format.JSON_EACH_ROW, b'{"v":1}')
        # chs_rows is mandatory, so the batch path still answers.
        assert (
            schema.rows(Format.JSON_EACH_ROW, b'{"ts":"2026-01-01 00:00:00","v":1}\n').rows_read
            == 1
        )


def test_a_closed_schema_refuses_work(newest: chtypes.Library) -> None:
    schema = newest.compile_ddl("x UInt8")
    schema.close()
    schema.close()  # idempotent
    with pytest.raises(chtypes.ChtypesError, match="closed"):
        schema.row(Format.JSON_EACH_ROW, b'{"x":1}')


def test_shutdown_then_the_process_exits(registry: chtypes.Registry) -> None:
    """`chs_shutdown` before teardown, and the process exits rather than hanging.

    Resolving a DEFAULT starts a periodic-reload thread on ClickHouse's global
    pool. Nothing joins it until a static destructor runs, at which point it is
    still looping — and the process hangs AFTER printing everything, which reads
    as a harness bug rather than a teardown bug.
    """
    script = textwrap.dedent(
        f"""
        import chtypes
        from chtypes import Format
        registry = chtypes.Registry({str(registry.directory)!r})
        library = registry.libraries()[-1]
        schema = library.compile_ddl("a UInt8, ts DateTime DEFAULT now()")
        result = schema.row(Format.JSON_EACH_ROW, b'{{"a":1}}')
        assert result.substituted, result
        schema.close()
        registry.close()
        print("exited cleanly")
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stderr
    assert "exited cleanly" in done.stdout


def test_the_host_timezone_does_not_leak_into_answers(registry: chtypes.Registry) -> None:
    """`chs_init` is passed UTC, and the process environment is never consulted.

    Without that, the host's TZ changes what a bare DateTime column stores — an
    answer that depends on where the gateway runs rather than on the table.
    """
    script = textwrap.dedent(
        f"""
        import chtypes
        from chtypes import Format
        registry = chtypes.Registry({str(registry.directory)!r})
        library = registry.libraries()[-1]
        with library.compile_ddl("ts DateTime") as schema:
            print(schema.row(Format.JSON_EACH_ROW, b'{{"ts":1700000000}}').value("ts").text)
        with library.compile_ddl("ts DateTime DEFAULT now()") as schema:
            pinned = schema.row(
                Format.JSON_EACH_ROW, b"{{}}", {{"chtypes_now_epoch_nanos": "1700000000000000000"}}
            )
            print(pinned.value("ts").text)
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "TZ": "America/New_York"},
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ['"2023-11-14', '22:13:20"', '"2023-11-14', '22:13:20"']


def test_set_default_settings_excludes_the_row_path(newest: chtypes.Library) -> None:
    """The one genuinely dangerous call on the ABI, under real contention.

    `chs_set_default_settings` REPLACES the seeded settings list wholesale while
    `chs_row` / `chs_rows` read that same list by reference (spec/c-abi.md
    §Thread-safety: it "MUST be serialized against all other calls"). ctypes
    releases the GIL for the whole duration of a foreign call, so Python threads
    genuinely can be inside `chs_rows` when a seed lands — the GIL is not the
    exclusion, `_native._RWLock` is.

    This drives it: readers hammer `rows()` on their OWN schema (one handle per
    thread, because a single `chs_schema *` is single-threaded by the ABI) while
    a writer swaps the process-wide seed underneath them. The assertion is not
    "it did not crash" — a race that only sometimes corrupts would pass that. It
    is that every one of the thousands of answers is byte-for-byte the answer the
    same call gives with no writer running at all, that no thread raised, and
    that both sides actually ran (a test where the writer never got the lock
    would prove nothing, and is what a readers-preferring lock would produce).

    What this test does NOT claim: that removing the exclusion crashes. Measured
    2026-08-26 with `_RWLock.write` monkey-patched to `read` — i.e. no exclusion
    at all — the same workload ran 54,131 batch reads against 47,814 seeds with
    zero mismatches and zero faults. That is not evidence the race is benign; it
    is what a use-after-free looks like when the allocator hands the same bytes
    back. The reason for the lock is the ABI's own normative line
    (`chs_set_default_settings` "MUST be serialized against all other calls")
    plus the source fact that `g_default_settings.assign(...)` frees the buffer
    the row path is holding a reference into. This test is the regression guard
    on the exclusion — that it holds, that it does not deadlock, and that it
    costs the readers nothing — not the reproduction of the fault.
    """
    import threading
    import time

    body = b'{"a": 1}\n{"a": 2}\n{"a": 3}\n'

    # The answer with nothing else running, computed first: the oracle the
    # contended answers are compared against.
    with newest.compile_ddl("a UInt8") as quiet:
        want = [[v.text for v in row.values] for row in quiet.rows(Format.JSON_EACH_ROW, body).rows]
    assert want == [["1"], ["2"], ["3"]]

    n_readers = 8
    deadline = time.monotonic() + 2.0
    reads = [0] * n_readers
    swaps = 0
    errors: list[BaseException] = []
    start = threading.Barrier(n_readers + 1)

    def reader(slot: int) -> None:
        try:
            with newest.compile_ddl("a UInt8") as schema:
                start.wait(timeout=30)
                while time.monotonic() < deadline:
                    got = schema.rows(Format.JSON_EACH_ROW, body)
                    assert [[v.text for v in r.values] for r in got.rows] == want
                    assert got.outcome is Outcome.ACCEPTED
                    assert all(r.unsupported_settings == () for r in got.rows)
                    reads[slot] += 1
        except BaseException as exc:  # noqa: BLE001 - re-raised in the main thread
            errors.append(exc)

    def writer() -> None:
        nonlocal swaps
        try:
            start.wait(timeout=30)
            while time.monotonic() < deadline:
                # Both payloads are inert for a `a UInt8` row, so a reader that
                # observed either one still owes the same answer. What is being
                # exercised is the REPLACEMENT of the list, not its content.
                newest.set_default_settings(
                    {"chtypes_default_eval_wall_nanos": "2000000000"} if swaps % 2 else {}
                )
                swaps += 1
                # Yield between seeds. The lock is writer-PREFERRING, so a
                # writer that re-queues instantly holds the readers off almost
                # completely (measured: 20 batch reads in 2 s against a tight
                # swap loop). That priority is the right one — a seed must land
                # rather than be starved by a steady stream of rows — and a
                # tight loop is not the shape of a call the ABI restricts to
                # process start. 1 ms apart is still hundreds of swaps landing
                # INSIDE the readers' run, which is the exclusion under test.
                time.sleep(0.001)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the main thread
            errors.append(exc)

    threads = [threading.Thread(target=reader, args=(i,), daemon=True) for i in range(n_readers)]
    threads.append(threading.Thread(target=writer, daemon=True))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    newest.set_default_settings({})  # leave the process as it was found

    assert not errors, errors[0]
    assert all(not t.is_alive() for t in threads), "a thread did not finish — deadlock"
    # Contention actually happened on both sides. Without these the test could
    # pass by never letting the two meet.
    assert min(reads) > 0, f"a reader never ran: {reads}"
    assert sum(reads) > 200, f"too few reads to be contention: {sum(reads)}"
    assert swaps > 100, f"the writer was starved: {swaps} swaps"
    print(
        f"\ncontention measured: {sum(reads)} batch reads across {n_readers} threads "
        f"({sum(reads) / 2.0:.0f}/s) against {swaps} default-settings swaps, "
        f"0 mismatches, 0 exceptions"
    )


# ---------------------------------------------------------------- image refcount


def test_closing_one_registry_leaves_another_answering(registry: chtypes.Registry) -> None:
    """Two Registries over one artifact directory share dlopen'd IMAGES.

    Library.close() is refcounted on the resolved path (spec/bindings.md
    §Teardown, 2026-08-26): closing the first registry must be a no-op at the
    C boundary while the second still holds the images. Before the refcount,
    this exact sequence joined the DEFAULT evaluator's threads under the
    survivor's live libraries.
    """
    a = chtypes.Registry(registry.directory)
    b = chtypes.Registry(registry.directory)
    a.close()
    # The survivor still answers — including a DEFAULT evaluation, which is
    # exactly the machinery chs_shutdown tears down.
    lib = b.libraries()[-1]
    with lib.compile_ddl("a UInt8, d UInt8 DEFAULT a + 1") as schema:
        got = schema.rows(Format.JSON_EACH_ROW, b'{"a": 4}\n')
        assert got.outcome is Outcome.ACCEPTED
        assert got.rows[0].value("d").text == "5"
    # And the closed one refuses further resolution rather than lying.
    with pytest.raises(chtypes.ChtypesError):
        a.for_version(lib.minor)
    b.close()  # refs on the session registry keep every image alive


def test_library_close_is_refcounted_per_image(tmp_path: Path) -> None:
    """The exact counting contract, in a subprocess so the LAST close is
    reachable without tearing down this suite's own session registry: close #1
    of 2 calls nothing, close #2 calls chs_shutdown once, close #3 is a no-op.
    """
    candidates = os.environ.get(chtypes.ENV_REGISTRY) or str(Path(chtypes.default_registry_dir()))
    versions = sorted(d for d in Path(candidates).iterdir() if (d / "manifest.json").is_file())
    if not versions:
        pytest.skip(f"no artifacts under {candidates}")
    # A one-version registry keeps the subprocess cheap: symlink one version
    # in — one this binding can LOAD. Mid-relink a registry legitimately
    # holds artifacts at an older ABI revision, which the loader refuses BY
    # DESIGN (spec/c-abi.md §The ABI revision); refcounting can only be
    # measured through an artifact that loads, so stage the newest loadable
    # one and skip only when there is none. The refusal itself is exercised
    # by the loader's own gate, not weakened here.
    staged = None
    refusals: list[str] = []
    for candidate in reversed(versions):
        manifest = chtypes.read_manifest(candidate)
        if manifest is None:
            continue
        try:
            NativeLibrary(str(candidate / manifest.library))
        except chtypes.RegistryError as exc:
            refusals.append(str(exc))
            continue
        staged = candidate
        break
    if staged is None:
        detail = refusals[-1] if refusals else "none found"
        pytest.skip(
            f"no artifact under {candidates} loads at ABI revision {chtypes.ABI_REVISION}: {detail}"
        )
    (tmp_path / staged.name).symlink_to(staged)
    script = textwrap.dedent(
        """
        import sys

        import chtypes
        from chtypes import registry as reg_mod

        calls = {"n": 0}
        real = reg_mod.NativeLibrary.shutdown
        reg_mod.NativeLibrary.shutdown = lambda self: calls.__setitem__("n", calls["n"] + 1)

        a = chtypes.Registry(sys.argv[1])
        b = chtypes.Registry(sys.argv[1])
        a.close()
        assert calls["n"] == 0, f"first close reached the C boundary: {calls}"
        # The survivor still answers after its sibling closed.
        with b.libraries()[0].compile_ddl("x UInt8") as schema:
            row = schema.row(chtypes.Format.JSON_EACH_ROW, b'{"x": 256}')
            assert row.outcome is chtypes.Outcome.ACCEPTED
        a.close()
        assert calls["n"] == 0, f"a second close on the same wrapper released again: {calls}"
        b.close()
        assert calls["n"] == 1, f"the LAST close must shut the image down exactly once: {calls}"
        b.close()
        assert calls["n"] == 1, f"close is idempotent per wrapper: {calls}"
        print("REFCOUNT-OK", calls["n"])
        """
    )
    # Isolated: the staged directory is the ONLY registry on the subprocess's
    # search path, or `libraries()` would load every line of this machine's
    # cache beside the one staged version and the count would be off.
    env = {k: v for k, v in os.environ.items() if k != chtypes.ENV_REGISTRY}
    env["XDG_CACHE_HOME"] = str(tmp_path / "xdg-cache")
    proc = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "REFCOUNT-OK 1" in proc.stdout, proc.stdout


# ---------------------------------------------------------------- introspection


def test_the_introspection_trio_is_exposed(newest: chtypes.Library) -> None:
    """spec/bindings.md §Introspection: the same three questions in every SDK."""
    families = newest.registered_families()
    assert "String" in families
    assert len(families) > 100  # 139 on the 25.8 artifact; the registry grows
    flags = newest.function_flags()
    lines = [ln for ln in flags.split("\n") if ln]
    assert len(lines) > 500
    by_name = {ln.split("\t", 1)[0]: ln.split("\t") for ln in lines}
    # now() is one of the four admitted clock reads: registered, and not
    # deterministic in the scope of a query.
    assert "now" in by_name
    assert len(by_name["now"]) == 6
    # The trio's third member is asserted in test_types.py (reference_type).
