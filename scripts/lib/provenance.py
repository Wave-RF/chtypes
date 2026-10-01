#!/usr/bin/env python3
"""provenance.py — say what a registry directory actually holds, before a
suite runs against it.

WHY THIS EXISTS. `scripts/check-standalone.sh` and `scripts/check-suite.sh`
printed the registry PATH they used (`CHTYPES_REGISTRY=$REG`) and never what
was IN it. A path is not a fingerprint: the per-user cache
`~/.cache/chtypes/artifacts/abi<R>/<os>-<arch>/` is the default registry for every
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

chtypes#320. A machine automating the artifact producer's ABI-revision
transition can read this job's check-run ANNOTATIONS anonymously but not its
job log (measured 2026-09-30: the log 403s unauthenticated). So every line
this prints is ALSO emitted as a `::notice title=chtypes artifact
provenance::<exact line>` workflow command — same text, no summarizing —
whenever `GITHUB_ACTIONS=true` (`scripts/lib/gha_annotate.py`, imported
below); outside a GitHub Actions run this prints exactly what it always has.
This is the single place every provenance line is produced, so the
annotation can never drift from the line a human reads in the log, which is
also why this is done here rather than by a caller filtering this script's
stdout afterward (that approach risks swallowing a real exit code behind the
filter — see check-suite.sh's own `set -o pipefail` discipline for why that
matters in this repository).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import gha_annotate

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


def emit_line(line: str, out) -> None:
    """Print one provenance line to `out`, and — at this single place every
    such line is produced — also hand it to `gha_annotate.notice()`, which
    emits it verbatim as a `::notice` workflow-command annotation when
    `GITHUB_ACTIONS=true` and does nothing otherwise (chtypes#320). `out` is
    passed straight through as the annotation's own `file=`, so a selftest
    that redirects `out` to a buffer captures the annotation line in the
    same place as the plain one, and a real run writes both to the same
    stream the rest of this script already uses."""
    print(line, file=out)
    gha_annotate.notice(line, file=out)


def report(registry: str | None, header_path: str, out=None) -> None:
    # `out` defaults to None, resolved to `sys.stdout` HERE rather than in the
    # signature: a default of `out=sys.stdout` binds the stream object once,
    # at function-definition time, so `contextlib.redirect_stdout` in
    # `selftest()` below would silently have no effect on it.
    if out is None:
        out = sys.stdout
    try:
        pinned = read_pinned_abi_revision(header_path)
    except (OSError, ValueError) as e:
        # The one line this function cannot possibly print normally — if the
        # header cannot even say what revision this binding is pinned to,
        # nothing else below can run either. Say so as an explicit "unknown"
        # annotation (chtypes#320, point 3: a line that could not be emitted
        # must never read as silence, which a bare exception here otherwise
        # would to anything watching only the annotations) before re-raising
        # exactly as before this existed — this function's error behavior is
        # otherwise unchanged.
        gha_annotate.unknown(f"this binding's pinned ABI revision ({header_path}: {e})", file=out)
        raise
    emit_line(f"provenance: this binding is pinned to ABI revision {pinned} ({header_path})", out)

    if registry is None:
        emit_line("provenance: no artifact registry in use (--no-artifacts)", out)
        return

    reg = Path(registry)
    if not registry or not reg.is_dir():
        emit_line(f"provenance: no artifact registry at {registry or '<empty>'}", out)
        return

    # Two levels (chtypes#284, "Layout rule"): the flat <minor>/ holding the
    # line's currently newest patch, and patches/<minor>/<clickhouse_version>/
    # holding every OTHER installed exact patch. `patches/` itself carries no
    # manifest.json of its own and is excluded from the flat listing below —
    # it is reported as its own, separately labeled set of entries instead of
    # once as a bare "scratch directory".
    line_dirs = sorted(
        (p for p in reg.iterdir() if p.is_dir() and p.name != "patches"), key=lambda p: p.name
    )
    nested_dirs: list[Path] = []
    patches_root = reg / "patches"
    if patches_root.is_dir():
        for minor_dir in sorted((p for p in patches_root.iterdir() if p.is_dir()), key=lambda p: p.name):
            nested_dirs.extend(
                sorted((p for p in minor_dir.iterdir() if p.is_dir()), key=lambda p: p.name)
            )

    if not line_dirs and not nested_dirs:
        emit_line(f"provenance: {registry} has no line directories", out)

    def report_one(display: str, minor_fallback: str, manifest_path: Path) -> None:
        if not manifest_path.is_file():
            # A registry may legitimately hold scratch directories
            # (docs/guides/artifacts.md, Loading step 2) — every loader skips
            # them silently. This says so, out loud, instead.
            emit_line(f"provenance: {display}: no manifest.json (scratch directory, skipped)", out)
            return
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            emit_line(f"provenance: {display}: manifest.json unparseable ({e})", out)
            return
        if not isinstance(manifest, dict):
            emit_line(f"provenance: {display}: manifest.json is not an object, skipped", out)
            return

        minor = _field(manifest, "clickhouse_minor") if manifest.get("clickhouse_minor") else minor_fallback
        version = _field(manifest, "clickhouse_version")
        build = _field(manifest, "chtypes_build")
        abi = manifest.get("abi_revision")
        core_commit = _field(manifest, "core_commit", 12)
        lib_sha = _field(manifest, "library_sha256", 12)
        emit_line(
            f"provenance: {display}: clickhouse={version} chtypes_build={build} "
            f"abi_revision={abi if abi is not None else '?'} core_commit={core_commit} "
            f"library_sha256={lib_sha}",
            out,
        )
        if isinstance(abi, int):
            if abi != pinned:
                emit_line(
                    f"provenance: WARNING: {minor}'s artifact is ABI revision {abi}, this "
                    f"binding is pinned to {pinned} — the suite will skip or refuse this line",
                    out,
                )
        else:
            emit_line(
                f"provenance: WARNING: {minor}'s manifest has no usable abi_revision "
                f"({abi!r}) — cannot compare it to this binding's pinned revision {pinned}",
                out,
            )

    for line_dir in line_dirs:
        report_one(line_dir.name, line_dir.name, line_dir / "manifest.json")
    for patch_dir in nested_dirs:
        minor = patch_dir.parent.name
        report_one(f"{minor} (patches/{patch_dir.name})", minor, patch_dir / "manifest.json")

    goldens_path = reg / "sdk-goldens.json"
    if not goldens_path.is_file():
        emit_line(f"provenance: no sdk-goldens.json at {registry}", out)
        return
    try:
        digest = sha256_prefix(goldens_path)
    except OSError as e:
        emit_line(f"provenance: sdk-goldens.json present but unreadable ({e})", out)
        return
    emit_line(f"provenance: sdk-goldens.json sha256={digest}", out)
    try:
        goldens = json.loads(goldens_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        emit_line(f"provenance: sdk-goldens.json unparseable ({e})", out)
        return
    generated = goldens.get("generated") if isinstance(goldens, dict) else None
    if isinstance(generated, dict):
        at = generated.get("at", "?")
        builds = generated.get("builds", {})
        emit_line(
            f"provenance: sdk-goldens.json generated.at={at} "
            f"generated.builds={json.dumps(builds, sort_keys=True)}",
            out,
        )
    else:
        emit_line("provenance: sdk-goldens.json has no generated block", out)


def selftest() -> int:
    import contextlib
    import io
    import tempfile

    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    def run(registry: str | None, header: str) -> str:
        # GITHUB_ACTIONS forced OFF (unset) for the duration, saved and
        # restored: every assertion below this point reads the output
        # expecting exactly the plain provenance lines with no ::notice
        # annotation mixed in, and that must hold whether or not this
        # selftest happens to be running inside REAL GitHub Actions CI
        # itself — scripts/check-suite.sh's own --selftest, which calls this
        # one, is wired into ci.yml's `abi` job, where GITHUB_ACTIONS=true is
        # already set by the runner. chtypes#320's own annotations are
        # proven separately and deterministically by run_annotated(), below.
        saved_gha = os.environ.get("GITHUB_ACTIONS")
        os.environ.pop("GITHUB_ACTIONS", None)
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                report(registry, header)
            return buf.getvalue()
        finally:
            if saved_gha is not None:
                os.environ["GITHUB_ACTIONS"] = saved_gha

    def run_annotated(registry: str | None, header: str) -> str:
        """Same as run(), with GITHUB_ACTIONS forced to 'true' for the
        duration — proves the ::notice/::warning annotations chtypes#320
        adds, deterministically, regardless of the ambient environment."""
        saved_gha = os.environ.get("GITHUB_ACTIONS")
        os.environ["GITHUB_ACTIONS"] = "true"
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                report(registry, header)
            return buf.getvalue()
        finally:
            if saved_gha is None:
                os.environ.pop("GITHUB_ACTIONS", None)
            else:
                os.environ["GITHUB_ACTIONS"] = saved_gha

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

        # A nested patch under patches/<minor>/<version>/ (chtypes#284,
        # "Layout rule") — a patch other than the one in the flat slot. A
        # DIFFERENT minor line from the flat cases above (26.7, not 25.8),
        # so its own ABI-revision mismatch cannot be confused with — or mask
        # — 25.8's flat case below. Reported separately, by its own label,
        # never folded into a flat line and never silently skipped as
        # "patches: no manifest.json".
        nested = registry / "patches" / "26.7" / "26.7.14.3-lts"
        nested.mkdir(parents=True)
        (nested / "manifest.json").write_text(
            json.dumps(
                {
                    "clickhouse_minor": "26.7",
                    "clickhouse_version": "26.7.14.3-lts",
                    "chtypes_build": 1780500000,
                    "abi_revision": 4,
                    "core_commit": "eeeeeeeeeeeeffffffffffffffffffff",
                    "library_sha256": "1111222233334444555566667777888899990000",
                }
            )
        )

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
        check(
            "26.7 (patches/26.7.14.3-lts): clickhouse=26.7.14.3-lts chtypes_build=1780500000 "
            "abi_revision=4 core_commit=eeeeeeeeeeee library_sha256=111122223333" in out,
            "a nested patches/<minor>/<version>/ install did not print its own provenance line",
        )
        check(
            f"WARNING: 26.7's artifact is ABI revision 4, this binding is pinned to {pinned}" in out,
            "the nested patch's own ABI-revision mismatch did not warn",
        )
        check(
            "patches: no manifest.json (scratch directory, skipped)" not in out,
            "the patches/ sibling tree itself was reported as a manifest-less scratch "
            "directory rather than excluded from the flat listing",
        )
        # With GITHUB_ACTIONS unset, there must be NO workflow-command
        # annotation at all — proves the gate below actually gates, rather
        # than merely adding the right line unconditionally.
        check(
            "::notice" not in out and "::warning" not in out,
            "a workflow-command annotation leaked into a non-GitHub-Actions run",
        )

        # chtypes#320 — the SAME scenario, annotated: every line above must
        # ALSO appear, verbatim, as `::notice title=chtypes artifact
        # provenance::<line>` when GITHUB_ACTIONS=true — a WARNING line
        # included, still wrapped as a `notice` (not a `warning`): that
        # level is reserved for "could not be emitted at all" (point 3,
        # checked separately below), not for a mismatch finding, which did
        # get emitted and is simply bad news.
        annotated = run_annotated(str(registry), str(header))
        check(
            f"::notice title=chtypes artifact provenance::provenance: this binding is pinned to ABI revision {pinned}"
            in annotated,
            "GITHUB_ACTIONS=true did not annotate the pinned-revision line",
        )
        check(
            "::notice title=chtypes artifact provenance::provenance: 25.8: clickhouse=25.8.28.1-lts "
            "chtypes_build=1790177616 abi_revision=5 core_commit=30441aea6028 library_sha256=f54672243789"
            in annotated,
            "GITHUB_ACTIONS=true did not annotate a matching-revision line",
        )
        check(
            f"::notice title=chtypes artifact provenance::provenance: WARNING: 24.8's artifact is ABI "
            f"revision 4, this binding is pinned to {pinned} — the suite will skip or refuse this line"
            in annotated,
            "GITHUB_ACTIONS=true did not annotate a WARNING line",
        )
        check(
            "::warning title=chtypes artifact provenance::" not in annotated,
            "a provenance WARNING finding was emitted as a ::warning annotation instead of a ::notice "
            "— ::warning is reserved for 'could not be emitted at all' (point 3)",
        )
        check(
            f"provenance: this binding is pinned to ABI revision {pinned}" in annotated,
            "GITHUB_ACTIONS=true suppressed the plain provenance line instead of adding to it",
        )

        # chtypes#320, point 3 — the one line report() cannot possibly
        # print: the header defines no CHS_ABI_REVISION at all, so nothing
        # past that point can run either. This must still emit an explicit
        # "unknown" annotation — never silence — before re-raising exactly
        # the same ValueError report() always raised here.
        bad_header = tmp / "no-revision.h"
        bad_header.write_text("// no CHS_ABI_REVISION here\n")
        saved_gha = os.environ.get("GITHUB_ACTIONS")
        os.environ["GITHUB_ACTIONS"] = "true"
        try:
            buf = io.StringIO()
            raised = False
            try:
                with contextlib.redirect_stdout(buf):
                    report(str(registry), str(bad_header))
            except ValueError:
                raised = True
            check(raised, "report() with no CHS_ABI_REVISION in the header did not raise ValueError")
            check(
                "::warning title=chtypes artifact provenance::unknown — this binding's pinned ABI revision"
                in buf.getvalue(),
                "the unreadable-header case did not emit an 'unknown' annotation before raising: "
                f"{buf.getvalue()!r}",
            )
        finally:
            if saved_gha is None:
                os.environ.pop("GITHUB_ACTIONS", None)
            else:
                os.environ["GITHUB_ACTIONS"] = saved_gha

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
        "rewriting the header's revision moves which line warns. chtypes#320: no annotation "
        "leaks with GITHUB_ACTIONS unset, every plain line (WARNING lines included) gets a "
        "matching ::notice with GITHUB_ACTIONS=true, and an unreadable header emits an "
        "explicit 'unknown' ::warning before re-raising rather than failing silently"
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
