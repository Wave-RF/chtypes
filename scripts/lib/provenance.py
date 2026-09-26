#!/usr/bin/env python3
"""provenance.py — say what a registry directory actually holds, before a
suite runs against it.

WHY THIS EXISTS. `scripts/check-standalone.sh` and `scripts/check-suite.sh`
printed the registry PATH they used (`CHTYPES_REGISTRY=$REG`) and never what
was IN it. A path is not a fingerprint: the per-user cache
`~/.cache/chtypes/artifacts/<os>-<arch>/` is the default registry for every
binding, and on a machine that also builds artifacts it is not a copy of what
is published — it can hold newer builds, older ABI revisions, or lines from
several different producer commits at once (chtypes#190). A green local run
then proves a suite passed against *something*, and the log cannot say what.

This prints, for each line a registry holds, the manifest fields that name
what will answer — `clickhouse_version`, `chtypes_build`, `abi_revision`,
`core_commit`, `library_sha256` — the registry's `sdk-goldens.json` identity if
one is installed, and a loud (never fatal) WARNING when a line's ABI revision
does not match the header this binding is compiled against
(`CHS_ABI_REVISION`, read from `include/chtypes.h` itself — never
hard-coded here, so a header bump changes what this warns about without this
file being touched).

    provenance.py --header <path/to/chtypes.h> [--registry <dir>]
    provenance.py --selftest

With no `--registry` (the `--no-artifacts` case) this says so and prints
nothing else. A registry directory that does not exist, a line whose
manifest.json is missing or unparseable, and a missing sdk-goldens.json are
each reported by name and never raise — a registry may legitimately hold
scratch directories (docs/guides/artifacts.md, "Loading", step 2: a directory
with no manifest is skipped silently by every loader, and this printer holds
to the same rule, only louder).

This intentionally does NOT check whether a build is one the current release
still serves — that is a live network read (`scripts/index-diff.sh` already
does it) and a different, optional question from "what answered here."
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ABI_REVISION_RE = re.compile(r"^\s*#\s*define\s+CHS_ABI_REVISION\s+(\d+)\b", re.MULTILINE)


def read_pinned_abi_revision(header_path: str) -> int:
    text = Path(header_path).read_text(encoding="utf-8")
    m = ABI_REVISION_RE.search(text)
    if not m:
        raise ValueError(f"{header_path} defines no CHS_ABI_REVISION")
    return int(m.group(1))


def sha256_prefix(path: Path, n: int = 12) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:n]


def _field(manifest: dict, key: str, width: int | None = None) -> str:
    val = manifest.get(key)
    if val is None:
        return "?"
    s = str(val)
    return s[:width] if width else s


def report(registry: str | None, header_path: str, out=None) -> None:
    # `out` defaults to None, resolved to `sys.stdout` HERE rather than in the
    # signature: a default of `out=sys.stdout` binds the stream object once,
    # at function-definition time, so `contextlib.redirect_stdout` in
    # `selftest()` below would silently have no effect on it.
    if out is None:
        out = sys.stdout
    pinned = read_pinned_abi_revision(header_path)
    print(f"provenance: this binding is pinned to ABI revision {pinned} ({header_path})", file=out)

    if registry is None:
        print("provenance: no artifact registry in use (--no-artifacts)", file=out)
        return

    reg = Path(registry)
    if not registry or not reg.is_dir():
        print(f"provenance: no artifact registry at {registry or '<empty>'}", file=out)
        return

    line_dirs = sorted((p for p in reg.iterdir() if p.is_dir()), key=lambda p: p.name)
    if not line_dirs:
        print(f"provenance: {registry} has no line directories", file=out)

    for line_dir in line_dirs:
        name = line_dir.name
        manifest_path = line_dir / "manifest.json"
        if not manifest_path.is_file():
            # A registry may legitimately hold scratch directories
            # (docs/guides/artifacts.md, Loading step 2) — every loader skips
            # them silently. This says so, out loud, instead.
            print(f"provenance: {name}: no manifest.json (scratch directory, skipped)", file=out)
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"provenance: {name}: manifest.json unparseable ({e})", file=out)
            continue
        if not isinstance(manifest, dict):
            print(f"provenance: {name}: manifest.json is not an object, skipped", file=out)
            continue

        minor = _field(manifest, "clickhouse_minor") if manifest.get("clickhouse_minor") else name
        version = _field(manifest, "clickhouse_version")
        build = _field(manifest, "chtypes_build")
        abi = manifest.get("abi_revision")
        core_commit = _field(manifest, "core_commit", 12)
        lib_sha = _field(manifest, "library_sha256", 12)
        print(
            f"provenance: {minor}: clickhouse={version} chtypes_build={build} "
            f"abi_revision={abi if abi is not None else '?'} core_commit={core_commit} "
            f"library_sha256={lib_sha}",
            file=out,
        )
        if isinstance(abi, int):
            if abi != pinned:
                print(
                    f"provenance: WARNING: {minor}'s artifact is ABI revision {abi}, this "
                    f"binding is pinned to {pinned} — the suite will skip or refuse this line",
                    file=out,
                )
        else:
            print(
                f"provenance: WARNING: {minor}'s manifest has no usable abi_revision "
                f"({abi!r}) — cannot compare it to this binding's pinned revision {pinned}",
                file=out,
            )

    goldens_path = reg / "sdk-goldens.json"
    if not goldens_path.is_file():
        print(f"provenance: no sdk-goldens.json at {registry}", file=out)
        return
    try:
        digest = sha256_prefix(goldens_path)
    except OSError as e:
        print(f"provenance: sdk-goldens.json present but unreadable ({e})", file=out)
        return
    print(f"provenance: sdk-goldens.json sha256={digest}", file=out)
    try:
        goldens = json.loads(goldens_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"provenance: sdk-goldens.json unparseable ({e})", file=out)
        return
    generated = goldens.get("generated") if isinstance(goldens, dict) else None
    if isinstance(generated, dict):
        at = generated.get("at", "?")
        builds = generated.get("builds", {})
        print(
            f"provenance: sdk-goldens.json generated.at={at} "
            f"generated.builds={json.dumps(builds, sort_keys=True)}",
            file=out,
        )
    else:
        print("provenance: sdk-goldens.json has no generated block", file=out)


def selftest() -> int:
    import contextlib
    import io
    import tempfile

    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    def run(registry: str | None, header: str) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            report(registry, header)
        return buf.getvalue()

    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        header = tmp / "chtypes.h"
        header.write_text("#define CHS_ABI_REVISION 5\n")

        registry = tmp / "registry"
        registry.mkdir()

        # A line whose artifact matches this binding's pinned revision.
        good = registry / "25.8"
        good.mkdir()
        (good / "manifest.json").write_text(
            json.dumps(
                {
                    "clickhouse_minor": "25.8",
                    "clickhouse_version": "25.8.28.1-lts",
                    "chtypes_build": 1790177616,
                    "abi_revision": 5,
                    "core_commit": "30441aea602896165a1c4168db65828b1e903903",
                    "library_sha256": "f54672243789b5816927c8d11d009bdc09be3959bd1e92839fd4391e9b8577e4",
                }
            )
        )

        # A line whose artifact is a stale, pre-relink revision — the exact
        # shape a branch relink or an old local build leaves behind.
        stale = registry / "24.8"
        stale.mkdir()
        (stale / "manifest.json").write_text(
            json.dumps(
                {
                    "clickhouse_minor": "24.8",
                    "clickhouse_version": "24.8.14.39-lts",
                    "chtypes_build": 1780000000,
                    "abi_revision": 4,
                    "core_commit": "aaaaaaaaaaaabbbbccccdddd0000",
                    "library_sha256": "bbbbccccddddeeeeffff000011112222",
                }
            )
        )

        # A scratch directory with no manifest at all — legitimate, per
        # docs/guides/artifacts.md's own loading rule, and must be named,
        # never crash the printer.
        (registry / "not-a-version").mkdir()

        pinned = read_pinned_abi_revision(str(header))
        out = run(str(registry), str(header))

        check(
            f"pinned to ABI revision {pinned}" in out,
            "did not announce the pinned revision read from the header",
        )
        check(
            "25.8: clickhouse=25.8.28.1-lts chtypes_build=1790177616 abi_revision=5 "
            "core_commit=30441aea6028 library_sha256=f54672243789" in out,
            "matching-revision line did not print the expected provenance fields",
        )
        check(
            "24.8: clickhouse=24.8.14.39-lts chtypes_build=1780000000 abi_revision=4 "
            "core_commit=aaaaaaaaaaaa library_sha256=bbbbccccdddd" in out,
            "mismatched-revision line did not print the expected provenance fields",
        )
        check(
            f"WARNING: 24.8's artifact is ABI revision 4, this binding is pinned to {pinned}" in out,
            "no WARNING printed for the mismatched-revision line",
        )
        check(
            "WARNING: 25.8" not in out,
            "a WARNING was printed for a line whose revision matches the header",
        )
        check(
            "not-a-version: no manifest.json (scratch directory, skipped)" in out,
            "the manifest-less scratch directory was not named",
        )
        check("no artifact registry" not in out, "a real registry was reported as absent")

        # THE point of this case: change a TEMP COPY of the header's number
        # and require the warning to move with it. A test that hard-sets the
        # expected revision instead of reading it from the header cannot
        # catch this printer regressing to a hard-coded number — so this
        # asserts against `read_pinned_abi_revision`'s own answer, not a
        # literal, and fails if the printer stops deriving from the header.
        header.write_text("#define CHS_ABI_REVISION 4\n")
        new_pinned = read_pinned_abi_revision(str(header))
        check(new_pinned == 4, "test setup: rewritten header did not read back as revision 4")
        out2 = run(str(registry), str(header))
        check(
            f"pinned to ABI revision {new_pinned}" in out2,
            "did not re-read the changed header's revision — looks hard-coded",
        )
        check(
            f"WARNING: 25.8's artifact is ABI revision 5, this binding is pinned to {new_pinned}"
            in out2,
            "the WARNING did not follow the header's number: 25.8 (revision 5) must now "
            "mismatch a revision-4 header",
        )
        check(
            "WARNING: 24.8" not in out2,
            "24.8 (revision 4) now matches the changed header and must not warn — the "
            "warning is still comparing against the old, hard-coded revision",
        )

        # --no-artifacts: no --registry at all.
        out3 = run(None, str(header))
        check(
            "no artifact registry in use (--no-artifacts)" in out3,
            "the no-registry case did not announce its absence",
        )
        check(
            "provenance:" not in out3.replace("provenance: this binding is pinned", "").replace(
                "provenance: no artifact registry in use (--no-artifacts)", ""
            ),
            "the no-registry case printed something beyond the pinned-revision and absence lines",
        )

        # A registry path that simply does not exist (not the --no-artifacts
        # case — a --registry/CHTYPES_REGISTRY pointing nowhere).
        out4 = run(str(tmp / "does-not-exist"), str(header))
        check(
            f"no artifact registry at {tmp / 'does-not-exist'}" in out4,
            "a nonexistent registry directory was not reported by its path",
        )

    if failures:
        for f in failures:
            print(f"SELFTEST FAILED: {f}", file=sys.stderr)
        return 1
    print(
        "provenance.py: selftest ok — provenance fields print per line, a mismatched ABI "
        "revision warns and a matching one does not, a manifest-less directory is named "
        "rather than crashing, the --no-artifacts and nonexistent-registry cases both "
        "announce absence, and the warning is derived from the header (not hard-coded): "
        "rewriting the header's revision moves which line warns"
    )
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--header", help="path to include/chtypes.h")
    ap.add_argument("--registry", default=None, help="registry directory in use; omit for --no-artifacts")
    ap.add_argument("--selftest", action="store_true", help="run this script's own correctness gate")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    if not args.header:
        ap.error("--header is required outside --selftest")
    report(args.registry, args.header)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
