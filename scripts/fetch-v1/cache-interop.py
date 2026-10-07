#!/usr/bin/env python3
r"""scripts/fetch-v1/cache-interop.py — a cache one binding writes, every binding reads
(docs/guides/fetch-v1.md §1, "The verified.json record").

    scripts/fetch-v1/cache-interop.py --cli go=<cmd> --cli python=<cmd> --cli ts=<cmd> --cli rust=<cmd>
                                      [--other-user <name>] [--system-dir <a default system dir>] [--faults-as-self]
    scripts/fetch-v1/cache-interop.py --abi 2 --cli <binding>=<cmd> ... [--other-user <name>] [--coarse-clock]
                                      [--platform <os-arch>] [--processes N] [--rounds N] [--coarse-rounds N]
    scripts/fetch-v1/cache-interop.py --selftest

The first form is the 1.x matrix, parts 1 to 7 below, for the bindings at ABI
v1. `--abi 2` is the ABI v2 matrix, "ABI v2 MODE" at the end, for the
bindings at ABI v2 (spec/binding-majors.json; the workflow derives which
form each binding gets, never this script).

Each `--cli` names a binding's `chtypes` command line (shell-split, so a
wrapper script or `node dist/cli.js` both work). The script starts
scripts/fetch-v1/server.py on loopback over tests/fixtures/fetch-v1 and runs
ONLY the four CLIs, never a library call, against it:

  1. WRITER x READER. For each writer binding: `chtypes fetch 26.8` into a
     fresh cache. Then each reader binding runs `fetch 26.8 --offline`,
     `list --offline` and `verify` on that same cache. A pair passes only if
     the reader exits 0, names the writer's directory, lists the same
     version, verifies, and LEAVES THE RECORD BYTE-FOR-BYTE UNCHANGED (a
     reader that quietly re-verified and rewrote it did not read it). The
     record each writer wrote must also satisfy
     spec/fetch-v1/schema/verified.schema.json, its `library_sha256` must be
     the sha256 of the library file on disk, and the four writers' records
     must be EQUAL as parsed JSON (key order and whitespace are free).
  2. 0.x rows. A cache directory a 0.x install left behind must never crash
     1.0, including when CHTYPES_CACHE points straight at it:
       - a 0.x registry directory (no oci-layout, no index.json): every
         reader's `fetch --offline` and `list --offline` answer not-installed,
         then an online fetch installs beside the old files and every old file
         is byte-for-byte unchanged;
       - an unpacked directory holding a 0.x manifest.json and no
         verified.json, without blobs: not installed;
       - the same with blobs: re-verified and installed, the stale directory
         replaced.
  3. WRITER x READER x LINE (public issue #481). The fixture tree of the
     `request-scope-*` cases serves two lines, 26.3 and 26.8. Each writer
     fetches 26.8 into a fresh cache. Each reader, on its own copy of that
     cache, then fetches 26.3 ONLINE (the path a line with no install of its
     own takes, which once answered 26.3 with the 26.8 install), and then
     answers both lines with `fetch --offline`. A cell passes only if every
     answer is a build within the line asked for, both lines are installed
     side by side, and the writer's 26.8 record is byte-for-byte unchanged.
  4. CONCURRENT INSTALLS (public issue #482). N processes (8), released
     together by a barrier, each run `fetch 26.8` into ONE fresh cache: each
     binding alone, then all four mixed, for several rounds. A read-only
     watcher polls `unpacked/sha256/` every millisecond. A set passes only if
     every process exits 0 naming the one final directory, that directory
     holds a library whose sha256 is its record's, the watcher saw no torn
     record or library, no entry vanishing, and no complete entry REPLACED
     (its inode changing: an installer deleted an entry another process may
     already be using), nothing else is left under `unpacked/sha256/`, and
     every binding's `verify` passes on the final cache.

  5. READ-ONLY COMMANDS (public issue #486). For each reader, on a cache
     path that does not exist and on a 0.x registry directory: `list
     --offline`, `verify` and `fetch 26.8 --offline`, run as the cache's own
     owner, so a write would succeed if one were attempted. A cell passes only
     if the tree is unchanged (no path created, removed or rewritten; a
     missing root stays missing), `verify` says on stderr that it verified 0
     builds under the root, and on the 0.x directory both `verify` and the
     offline fetch name it with the 0.x hint.
  6. CROSS-UID, WRITER x READER (public issue #486; only with --other-user).
     Each writer fetches 26.8 into a fresh cache as this user at umask 022;
     each reader then runs `fetch 26.8 --offline`, `list --offline` and
     `verify` as the other user (`sudo -n -u <name>`), and must succeed and
     name the writer's directory. Three controls run first, or every cell is
     INVALID: the other user's uid differs from this one and is not root, it
     can read a 0644 file here, and it is refused a 0600 one ("Permission
     denied"), so the identity switch and the modes are both real. The CLIs
     must be installed where the other user can run them.

  7. WRITER x READER x FAULT x MODE (public issue #486; only with
     --other-user). Each writer's cache is faulted by this user: none,
     root-000, unpacked-000, entry-000, record-000, record-garbage-noblobs
     (the record overwritten, blobs/ removed) and layout-0x (a fresh 0.x
     registry dir in place of the cache). Each reader then runs `fetch 26.8
     --offline`, `list --offline` and `verify` as the other user, in the
     default mode and with --strict, and, with --system-dir, once more with
     that default system dir holding a readable unpacked 26.8. A positive
     control first proves the other user is refused the faulted path
     (`Permission denied`), or the row is INVALID. Every reader must give the
     SAME answer, read from its exit status and output: the default mode
     answers "not installed" with a warning naming the path (or from the
     system dir, with the warning), and strict mode exits 9 with
     CHTYPES_CACHE_UNUSABLE naming the path and the reason, never falling
     through to the system dir.

Every process runs at umask 022, set here. Prints the 4x4 table, the 0.x
rows, one table per line, the concurrency sets, the read-only rows and the
cross-uid table, writes the same to $GITHUB_STEP_SUMMARY when set, and exits
nonzero if any cell fails. Without --other-user the cross-uid block is skipped
LOUDLY, by name.

ABI v2 MODE (`--abi 2`; public issue #511). A 2.0.0-dev CLI honors no source
or trust override (spec/abi-v2/docs.md r6), so no fixture server can feed it:
these rows drive the real, unmodified CLIs against the channel's real
registry, the staging `chtypes/v2-dev` repository, as a user's install does,
with the real library (~300 MB unpacked) in every cell. What they are judged
against never comes from a CLI: the channel (registry, cache subroot, record
schema, key id, abi) is read from scripts/release-channel.sh, the one file
that defines it for this branch's releases; the lines and each line's
manifest digest for the platform are read from the registry's own OCI API;
the record's shape is spec/fetch-v1/schema/verified.schema.json with its
`schema` constant set to the channel's (r5 changes the number and nothing
else). Every command names its cache with --cache, so each layout is the
subroot <cache>/<subroot> (r5). The platform is this host's (linux) unless
--platform names one. The two newest lines the registry lists are HIGH and
LOW. The parts carry the numbers of the 1.x parts they mirror; a v2 cache has
no 0.x past (part 2), and its cache faults (part 7) are not yet rows here.

  1. WRITER x READER. Each writer fetches HIGH online into a fresh cache. It
     must name <cache>/<subroot>/unpacked/sha256/<the registry's manifest
     hex>, write nothing beside the subroot, and leave a record that passes
     the schema, whose library hashes to its library_sha256, whose
     digests.manifest, signed_by, predicate.abi and platform are the
     registry's and the channel's, and whose version is within HIGH. The
     four writers' records must be EQUAL as parsed JSON. Then every reader,
     on that same cache, runs `fetch HIGH --offline` (the load path),
     `list --offline`, `verify` and `where`: it must name the writer's
     entry, list exactly that build, verify it, print the subroot, and
     change NOTHING in the cache (every path's inode, size and mtime, so a
     reader that re-verified and replaced a record it could not read fails
     even when it wrote the same bytes).
  3. WRITER x READER x LINE (#481). On its own copy of each writer's cache,
     each reader fetches LOW ONLINE, then answers LOW and HIGH offline: each
     answer must be the registry's build of the line asked for, the cache
     must hold exactly two records, `list --offline` both builds, and the
     writer's HIGH record must be untouched.
  4. CONCURRENT INSTALLS (#482). --processes (8) processes released
     together each fetch HIGH online into ONE fresh cache: each binding
     alone, then all of them mixed, --rounds (3) rounds per set, with part
     4's watcher on the subroot. Every process must exit 0 naming the
     registry's entry, the one entry's library must be its record's, nothing
     may be left beside it or outside the subroot, and every binding's
     `verify` must pass on the final cache. Failures are counted per binding
     with their exact error code.
     THE COARSE-CLOCK LEG (--coarse-clock, Linux; #516). The same sets,
     --coarse-rounds (2) rounds each, with scripts/fetch-v1/coarse-clock.c
     preloaded into every CLI, rounding CLOCK_REALTIME and gettimeofday()
     down to 100 µs. Its control runs first, or every row is INVALID: under
     the shim 20000 clock readings in a probe are all multiples of 100 µs and
     the shim logs that it loaded and rounded; without it they are not all
     multiples, so the check can fail. Each row then requires, from the
     shim's log, that the shim loaded into every CLI process's own image (not
     only a wrapper shell), and reports in how many it coarsened the clock (a
     CLI that never reads the wall clock through libc, as Go's runtime does
     not, is reported, not coarsened).
  5. READ-ONLY COMMANDS (#486). For each reader, on an absent root, an empty
     root and a root holding an empty subroot: `list --offline`, `list`,
     `verify`, `where` and `fetch HIGH --offline` must change nothing (an
     absent root stays absent), `verify` must say it verified 0 builds under
     the subroot, `where` must print the subroot, `list` must name both lines
     as published and nothing as installed, and the offline fetch must be
     CHTYPES_ARTIFACT_MISSING.
  6. CROSS-UID (#486; with --other-user). Each writer's part-1 cache, written
     at umask 022, is read by every reader as the other user exactly as in
     part 1, after the same three controls as the 1.x part 6.

The run fails if any cell fails, if a cell that should exist does not, or if
the registry served other builds at the end than at the start (a mid-run
publish; rerun). The selftest drives the same code over stand-in CLIs and no
network: a clean run passes every cell, and three planted defects (a reader
that cannot read a record, an installer that fails when another got there
first, a `list` that creates the subroot) each fail exactly the cells they
reach; on Linux it also proves the shim's control both ways and that a CLI
the shim never reached is INVALID.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "fetch-v1"
BINDINGS = ("go", "python", "ts", "rust")
PLATFORM = "linux-arm64"
CASE = "line-ok"
SPELLING = "26.8"
# Part 3: any request-scope case routes to the tree that serves both lines.
LINES_CASE = "request-scope-lower-line"
LINE_HIGH = "26.8"
LINE_LOW = "26.3"
# Part 4: processes per set, and rounds per set.
CONCURRENCY = 8
CONCURRENCY_ROUNDS = 3
HEX64 = re.compile(r"^[0-9a-f]{64}$")
# Parts 5 and 6: the umask every writer runs at, and the 0.x hint's own words.
UMASK = 0o022
ZERO_X_HINT = (
    "{root} holds a 0.x registry (26.1/manifest.json); chtypes 1.x uses an OCI layout at "
    "${{XDG_CACHE_HOME:-~/.cache}}/chtypes/v1"
)
# Run by each concurrent process before it execs its CLI: announce readiness,
# then wait for the barrier file, so all of them start together. A preload
# (the ABI v2 coarse-clock leg's shim) is handed over in
# CACHE_INTEROP_EXEC_PRELOAD and becomes LD_PRELOAD only for the exec, so the
# barrier's own interpreter never loads it and the shim's log names the CLI.
BARRIER = (
    "import os, sys, time\n"
    "open(sys.argv[1], 'w').close()\n"
    "deadline = time.monotonic() + 120\n"
    "while not os.path.exists(sys.argv[2]):\n"
    "    if time.monotonic() > deadline:\n"
    "        sys.exit(97)\n"
    "    time.sleep(0.0005)\n"
    "preload = os.environ.pop('CACHE_INTEROP_EXEC_PRELOAD', '')\n"
    "if preload:\n"
    "    os.environ['LD_PRELOAD'] = preload\n"
    "os.execvp(sys.argv[3], sys.argv[3:])\n"
)


def sha256_file(path: Path) -> str:
    """The sha256 of a file, read in chunks (an ABI v2 library is ~300 MB)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): sha256_file(p) for p in sorted(root.rglob("*")) if p.is_file()}


def tree_state(root: Path) -> dict[str, str] | None:
    """Every path under `root`, directories included, with a file's sha256 or
    "dir": None when `root` does not exist. Unlike `tree_hashes`, an empty
    directory that appeared is a change."""
    if not os.path.lexists(root):
        return None
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        out[rel] = "dir" if p.is_dir() and not p.is_symlink() else sha256_file(p)
    return out


def records_equal(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """The members in which two parsed records differ (empty when equal)."""
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def render_table(title: str, rows: list[str], cols: list[str], cells: dict[tuple[str, str], str]) -> str:
    width = max(len(r) for r in rows + [title])
    colw = [max(len(c), 4) for c in cols]
    lines = [title.ljust(width) + " | " + " | ".join(c.ljust(w) for c, w in zip(cols, colw, strict=True))]
    lines.append("-" * len(lines[0]))
    for r in rows:
        lines.append(r.ljust(width) + " | " + " | ".join(cells.get((r, c), "-").ljust(w) for c, w in zip(cols, colw, strict=True)))
    return "\n".join(lines)


def as_user(user: str, env: dict[str, str], argv: list[str]) -> list[str]:
    """`argv` run as `user` through `sudo -n`, with exactly the CHTYPES_*
    variables, PATH and HOME of `env` and nothing else (`env -i`), since sudo
    resets the environment anyway."""
    keep = {k: v for k, v in env.items() if k.startswith("CHTYPES_") or k in ("PATH", "HOME")}
    return ["sudo", "-n", "-u", user, "env", "-i", *(f"{k}={v}" for k, v in sorted(keep.items())), *argv]


class Harness:
    def __init__(self, clis: dict[str, list[str]], base: str, trusted_key_hex: str, work: Path) -> None:
        self.clis = clis
        self.base = base
        self.work = work
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("CHTYPES_") and k != "XDG_CACHE_HOME"}
        self.env["CHTYPES_ARTIFACTS_URL"] = base
        self.env["CHTYPES_TRUSTED_KEYS"] = trusted_key_hex
        self.env["HOME"] = str(work / "home")
        (work / "home").mkdir(exist_ok=True)

    def env_for(self, base: str | None) -> dict[str, str]:
        if base is None:
            return self.env
        return {**self.env, "CHTYPES_ARTIFACTS_URL": base}

    def run(
        self, binding: str, *args: str, base: str | None = None, user: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        argv = [*self.clis[binding], *args]
        if user is not None:
            argv = as_user(user, self.env_for(base), argv)
        return subprocess.run(argv, env=self.env_for(base), capture_output=True, text=True, timeout=300, check=False)

    def fetch(
        self, binding: str, spelling: str, cache: Path, *, offline: bool, base: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        args = ["fetch", spelling, "--platform", PLATFORM, "--cache", str(cache)]
        if offline:
            args.append("--offline")
        return self.run(binding, *args, base=base)


def only_record(cache: Path) -> Path:
    found = sorted(cache.glob("unpacked/sha256/*/verified.json"))
    if len(found) != 1:
        raise RuntimeError(f"expected exactly one verified.json under {cache}, found {len(found)}")
    return found[0]


def check_writer(h: Harness, writer: str, expected: dict[str, str], schema: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    sys.path.insert(0, str(ROOT / "scripts" / "fetch-v1"))
    import schema_check  # type: ignore[import-not-found]

    cache = h.work / f"cache-{writer}"
    done = h.fetch(writer, SPELLING, cache, offline=False)
    if done.returncode != 0:
        raise RuntimeError(f"{writer} fetch {SPELLING} exited {done.returncode}: {done.stderr.strip()[-600:]}")
    record_path = only_record(cache)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    try:
        schema_check.validate(record, schema, schema)
    except schema_check.SchemaError as e:
        raise RuntimeError(f"{writer}'s verified.json breaks verified.schema.json: {e.message} (at {e.path})") from e
    lib = record_path.parent / record["library"]
    if not lib.is_file() or sha256_file(lib) != record["library_sha256"]:
        raise RuntimeError(f"{writer}'s record library_sha256 is not the sha256 of {lib}")
    for key, want in expected.items():
        if record[key] != want:
            raise RuntimeError(f"{writer}'s record has {key}={record[key]!r}, the fixture says {want!r}")
    return cache, record


def check_reader(h: Harness, reader: str, cache: Path, expected: dict[str, str]) -> str:
    record_path = only_record(cache)
    before = record_path.read_bytes()
    got = h.fetch(reader, SPELLING, cache, offline=True)
    if got.returncode != 0:
        return f"FAIL fetch --offline exit {got.returncode}: {got.stderr.strip()[-200:]}"
    if os.path.realpath(got.stdout.strip()) != os.path.realpath(record_path.parent):
        return f"FAIL fetch --offline named {got.stdout.strip()!r}, not {record_path.parent}"
    listed = h.run(reader, "list", "--offline", "--cache", str(cache))
    if listed.returncode != 0 or f"installed {expected['version']} {PLATFORM}" not in listed.stdout:
        return f"FAIL list --offline exit {listed.returncode}, output {listed.stdout.strip()[-200:]!r}"
    verified = h.run(reader, "verify", "--cache", str(cache))
    if verified.returncode != 0:
        return f"FAIL verify exit {verified.returncode}: {verified.stderr.strip()[-200:]}"
    if record_path.read_bytes() != before:
        return "FAIL the reader rewrote the record"
    return "ok"


def upgrade_rows(h: Harness, readers: list[str], missing_exit: int, expected: dict[str, str]) -> dict[tuple[str, str], str]:
    cells: dict[tuple[str, str], str] = {}
    r0x = "0.x registry dir: offline, then online beside it"
    rnb = "0.x unpacked dir, no blobs: not installed"
    rbl = "0.x unpacked dir, with blobs: re-verified"
    for reader in readers:
        # (a) a 0.x registry directory, used as the whole 1.0 cache.
        cache = h.work / f"0x-registry-{reader}"
        shutil.copytree(FIXTURES / "layouts" / "upgrade-0x-registry", cache)
        old = tree_hashes(cache)
        off = h.fetch(reader, SPELLING, cache, offline=True)
        listed = h.run(reader, "list", "--offline", "--cache", str(cache))
        if off.returncode != missing_exit:
            cells[(r0x, reader)] = f"FAIL offline exit {off.returncode}, want {missing_exit}: {off.stderr.strip()[-160:]}"
        elif listed.returncode != 0 or "installed" in listed.stdout:
            cells[(r0x, reader)] = f"FAIL list --offline exit {listed.returncode}, output {listed.stdout.strip()[-160:]!r}"
        else:
            on = h.fetch(reader, SPELLING, cache, offline=False)
            after = tree_hashes(cache)
            touched = sorted(p for p, digest in old.items() if after.get(p) != digest)
            if on.returncode != 0:
                cells[(r0x, reader)] = f"FAIL online fetch exit {on.returncode}: {on.stderr.strip()[-160:]}"
            elif touched:
                cells[(r0x, reader)] = f"FAIL online fetch changed old files: {touched}"
            elif not list(cache.glob("unpacked/sha256/*/verified.json")):
                cells[(r0x, reader)] = "FAIL online fetch wrote no verified.json"
            else:
                cells[(r0x, reader)] = "ok"
        # (b) an unpacked directory with a 0.x manifest.json, no verified.json.
        cache = h.work / f"0x-noblobs-{reader}"
        shutil.copytree(FIXTURES / "layouts" / "upgrade-0x-unpacked-noblobs", cache)
        off = h.run(reader, "fetch", "26.3", "--platform", PLATFORM, "--cache", str(cache), "--offline")
        listed = h.run(reader, "list", "--offline", "--cache", str(cache))
        if off.returncode != missing_exit:
            cells[(rnb, reader)] = f"FAIL offline exit {off.returncode}, want {missing_exit}: {off.stderr.strip()[-160:]}"
        elif listed.returncode != 0 or "installed" in listed.stdout:
            cells[(rnb, reader)] = f"FAIL list --offline exit {listed.returncode}, output {listed.stdout.strip()[-160:]!r}"
        else:
            cells[(rnb, reader)] = "ok"
        cache = h.work / f"0x-blobs-{reader}"
        shutil.copytree(FIXTURES / "layouts" / "upgrade-0x-unpacked-reverify", cache)
        off = h.run(reader, "fetch", "26.3", "--platform", PLATFORM, "--cache", str(cache), "--offline")
        if off.returncode != 0:
            cells[(rbl, reader)] = f"FAIL offline exit {off.returncode}: {off.stderr.strip()[-160:]}"
        else:
            records = sorted(cache.glob("unpacked/sha256/*/verified.json"))
            stale = sorted(cache.glob("unpacked/sha256/*/manifest.json"))
            cells[(rbl, reader)] = "ok" if len(records) == 1 and not stale else f"FAIL records={len(records)} stale 0.x files={len(stale)}"
    return cells


def within(request: str, version: str) -> bool:
    """Whether a four-part version lies within a request: equal to an exact
    (four-part) one, a component prefix of a floating one (fetch-v1.md §4)."""
    want = request.split(".")
    got = version.split(".")
    return len(got) == 4 and len(want) <= 4 and got[: len(want)] == want


def named_dir(proc: subprocess.CompletedProcess[str]) -> str:
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""


def read_record(entry: Path) -> dict[str, Any] | None:
    try:
        rec = json.loads((entry / "verified.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def answered_line(proc: subprocess.CompletedProcess[str], line: str, what: str) -> tuple[Path | None, str]:
    """The directory a fetch named, if it exited 0 and its record's version
    lies within `line`; otherwise None and why."""
    if proc.returncode != 0:
        return None, f"FAIL {what} exit {proc.returncode}: {proc.stderr.strip()[-200:]}"
    entry = Path(named_dir(proc))
    rec = read_record(entry)
    if rec is None:
        return None, f"FAIL {what} named {str(entry)!r}, which holds no readable record"
    if not within(line, str(rec.get("version", ""))):
        return None, f"FAIL {what} answered {line} with {rec.get('version')!r}"
    return entry, "ok"


def line_rows(h: Harness, names: list[str], base: str) -> dict[tuple[str, str], str]:
    """Part 3: writer x reader x line, two lines in one cache."""
    cells: dict[tuple[str, str], str] = {}
    for writer in names:
        seed = h.work / f"lines-{writer}"
        wrote = h.fetch(writer, LINE_HIGH, seed, offline=False, base=base)
        entry, why = answered_line(wrote, LINE_HIGH, f"{writer} fetch {LINE_HIGH}")
        if entry is None:
            for reader in names:
                cells[(f"line {LINE_LOW}: {writer} wrote {LINE_HIGH}", reader)] = f"FAIL (no cache: {why})"
                cells[(f"line {LINE_HIGH}: {writer} wrote {LINE_HIGH}", reader)] = f"FAIL (no cache: {why})"
            continue
        high_rel = entry.resolve().relative_to(seed.resolve())
        for reader in names:
            cache = h.work / f"lines-{writer}-{reader}"
            shutil.copytree(seed, cache)
            high_record = cache / high_rel / "verified.json"
            before = high_record.read_bytes()
            low_key = (f"line {LINE_LOW}: {writer} wrote {LINE_HIGH}", reader)
            high_key = (f"line {LINE_HIGH}: {writer} wrote {LINE_HIGH}", reader)
            # The lower line has no install of its own: an ONLINE fetch is the
            # path that once answered it with the higher line's install.
            added, why = answered_line(h.fetch(reader, LINE_LOW, cache, offline=False, base=base), LINE_LOW, f"fetch {LINE_LOW}")
            if added is not None:
                off, why = answered_line(
                    h.fetch(reader, LINE_LOW, cache, offline=True, base=base), LINE_LOW, f"fetch {LINE_LOW} --offline"
                )
            records = sorted(cache.glob("unpacked/sha256/*/verified.json"))
            if why == "ok" and len(records) != 2:
                why = f"FAIL the cache holds {len(records)} records, want one per line"
            cells[low_key] = why
            off_high, why_high = answered_line(
                h.fetch(reader, LINE_HIGH, cache, offline=True, base=base), LINE_HIGH, f"fetch {LINE_HIGH} --offline"
            )
            if off_high is not None and high_record.read_bytes() != before:
                why_high = f"FAIL the {LINE_HIGH} record was rewritten"
            cells[high_key] = why_high
    return cells


def entry_state(entry: Path) -> str:
    """complete, unrecorded, torn-record, torn-library or gone: what a reader
    would find in one final `unpacked/sha256/<hex>/` directory at this
    instant ("gone": the directory itself went away while it was read)."""
    record = entry / "verified.json"
    try:
        text = record.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "unrecorded" if entry.is_dir() else "gone"
    except OSError:
        return "torn-record"
    try:
        rec = json.loads(text)
        lib = entry / rec["library"]
        size = lib.stat().st_size
    except FileNotFoundError:
        return "torn-library"
    except (ValueError, KeyError, TypeError, OSError):
        return "torn-record"
    return "complete" if size == rec.get("library_bytes") else "torn-library"


class Watcher(threading.Thread):
    """Polls a cache's `unpacked/sha256/` read-only, every millisecond, and
    counts what a concurrent reader could have met: an entry with no record,
    a torn record or library, a complete entry vanishing or going incomplete,
    and a complete entry REPLACED (another inode in its place: the directory a
    process may already be using was deleted)."""

    def __init__(self, cache: Path) -> None:
        super().__init__(daemon=True)
        self.base = cache / "unpacked" / "sha256"
        self.events: Counter[str] = Counter()
        self.inodes: dict[str, set[int]] = {}
        self.complete: set[str] = set()
        self.stop = threading.Event()

    def scan(self) -> None:
        try:
            names = os.listdir(self.base)
        except FileNotFoundError:
            names = []
        present: set[str] = set()
        for name in names:
            if not HEX64.match(name):
                continue
            entry = self.base / name
            try:
                ino = os.stat(entry).st_ino
            except FileNotFoundError:
                continue
            state = entry_state(entry)
            if state == "gone":
                continue
            present.add(name)
            if state == "complete":
                seen = self.inodes.setdefault(name, set())
                if seen and ino not in seen:
                    self.events["REPLACED"] += 1
                seen.add(ino)
                self.complete.add(name)
            else:
                if name in self.complete:
                    self.events["VANISHED"] += 1
                    self.complete.discard(name)
                self.events[state.upper()] += 1
        for name in self.complete - present:
            self.events["VANISHED"] += 1
            self.complete.discard(name)

    def run(self) -> None:
        while not self.stop.is_set():
            self.scan()
            time.sleep(0.001)
        self.scan()


def run_together(
    argvs: list[list[str]], env: dict[str, str], gate: Path, watch: Path, timeout: int = 300
) -> tuple[list[tuple[int, subprocess.CompletedProcess[str]]], Counter[str]]:
    """Starts every argv through BARRIER, waits until all of them are ready,
    starts a Watcher on `watch` (the directory holding `unpacked/`) and
    releases them together. Returns each process's pid and result, in argv
    order, and the watcher's events. `gate` names the marker files."""
    ready = gate.parent / f"{gate.name}.ready"
    barrier = gate.parent / f"{gate.name}.go"
    ready.mkdir()
    procs = []
    for i, argv in enumerate(argvs):
        procs.append(subprocess.Popen(
            [sys.executable, "-c", BARRIER, str(ready / str(i)), str(barrier), *argv],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ))
    deadline = time.monotonic() + 120
    while len(os.listdir(ready)) < len(argvs) and time.monotonic() < deadline:
        time.sleep(0.01)
    watcher = Watcher(watch)
    watcher.start()
    barrier.touch()
    results = []
    for p in procs:
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
        results.append((p.pid, subprocess.CompletedProcess(p.args, p.returncode, out, err)))
    watcher.stop.set()
    watcher.join()
    return results, watcher.events


def concurrent_round(h: Harness, members: list[str], cache: Path) -> tuple[int, list[str], Counter[str]]:
    """One round: len(members) processes released together, each fetching
    SPELLING into `cache`. Returns how many installed a verified library, the
    failures, and the watcher's events (plus any leftover)."""
    argvs = [[*h.clis[b], "fetch", SPELLING, "--platform", PLATFORM, "--cache", str(cache)] for b in members]
    ran, events = run_together(argvs, h.env, cache, cache)
    results = [(binding, proc) for binding, (_pid, proc) in zip(members, ran, strict=True)]
    installed = 0
    failures: list[str] = []
    entries = [p for p in sorted((cache / "unpacked" / "sha256").glob("*")) if HEX64.match(p.name)]
    for i, (binding, proc) in enumerate(results):
        if proc.returncode != 0:
            # The first line names the error; a raw runtime error's stack follows it.
            first = next((ln.strip() for ln in proc.stderr.splitlines() if ln.strip()), "")
            failures.append(f"#{i + 1} {binding} exit {proc.returncode}: {first[:240]}")
            continue
        entry = Path(named_dir(proc))
        rec = read_record(entry)
        if rec is None or len(entries) != 1 or os.path.realpath(entry) != os.path.realpath(entries[0]):
            failures.append(f"#{i + 1} {binding} named {str(entry)!r}, not the one final entry")
            continue
        lib = entry / str(rec.get("library", ""))
        if not lib.is_file() or sha256_file(lib) != rec.get("library_sha256"):
            failures.append(f"#{i + 1} {binding}: the library under {entry} is not its record's")
            continue
        installed += 1
    for left in sorted((cache / "unpacked" / "sha256").glob("*")):
        if not HEX64.match(left.name):
            events["LEFTOVER"] += 1
    for binding in sorted(set(h.clis)):
        verified = h.run(binding, "verify", "--cache", str(cache))
        if verified.returncode != 0:
            failures.append(f"{binding} verify on the final cache exit {verified.returncode}: {verified.stderr.strip()[-160:]}")
    return installed, failures, events


def concurrency_rows(h: Harness, names: list[str]) -> dict[tuple[str, str], str]:
    """Part 4: each binding alone, then all of them mixed, CONCURRENCY
    processes per round, CONCURRENCY_ROUNDS rounds per set."""
    sets = [(f"{b} x{CONCURRENCY}", [b] * CONCURRENCY) for b in names]
    if len(names) > 1:
        sets.append((f"mixed x{CONCURRENCY} ({'+'.join(names)})", [names[i % len(names)] for i in range(CONCURRENCY)]))
    cells: dict[tuple[str, str], str] = {}
    for label, members in sets:
        installed = 0
        failures: list[str] = []
        events: Counter[str] = Counter()
        for rnd in range(CONCURRENCY_ROUNDS):
            got, failed, seen = concurrent_round(h, members, h.work / f"conc-{label.split()[0]}-{rnd}")
            installed += got
            failures += [f"round {rnd + 1}: {f}" for f in failed]
            events.update(seen)
        total = len(members) * CONCURRENCY_ROUNDS
        summary = f"{installed}/{total} installed; events: {', '.join(f'{k} x{v}' for k, v in sorted(events.items())) or 'none'}"
        bad = failures or installed != total or events
        cells[(label, "result")] = ("FAIL " if bad else "ok ") + summary
        for f in failures:
            print(f"cache-interop: {label}: {f}", file=sys.stderr)
    return cells


def read_only_rows(h: Harness, names: list[str], missing_exit: int) -> dict[tuple[str, str], str]:
    """Part 5: read-only commands create nothing (public issue #486)."""
    cells: dict[tuple[str, str], str] = {}
    for reader in names:
        for label, make in (
            ("read-only commands: no cache dir", None),
            ("read-only commands: 0.x registry dir", "upgrade-0x-registry"),
        ):
            root = h.work / f"ro-{reader}" / ("zero-x" if make else "absent")
            root.parent.mkdir(parents=True, exist_ok=True)
            if make:
                shutil.copytree(FIXTURES / "layouts" / make, root)
            before = tree_state(root)
            listed = h.run(reader, "list", "--offline", "--cache", str(root))
            verified = h.run(reader, "verify", "--cache", str(root))
            off = h.fetch(reader, SPELLING, root, offline=True)
            after = tree_state(root)
            hint = ZERO_X_HINT.format(root=root)
            if after != before:
                created = sorted(set(after or {}) - set(before or {}))
                changed = sorted(p for p in (before or {}) if (after or {}).get(p) != (before or {})[p])
                why = f"FAIL the tree changed: created {created[:6]}, changed {changed[:6]}"
                if before is None:
                    why = f"FAIL the missing root was created, with {sorted(after or {})[:6]}"
            elif listed.returncode != 0 or listed.stdout.strip():
                why = f"FAIL list --offline exit {listed.returncode}, output {listed.stdout.strip()[-120:]!r}"
            elif verified.returncode != 0 or f"verified 0 builds under {root}" not in verified.stderr:
                why = f"FAIL verify exit {verified.returncode}, did not say it verified 0 builds: {verified.stderr.strip()[-160:]!r}"
            elif off.returncode != missing_exit:
                why = f"FAIL fetch --offline exit {off.returncode}, want {missing_exit}: {off.stderr.strip()[-160:]}"
            elif make and hint not in off.stderr:
                why = f"FAIL fetch --offline did not name the 0.x registry: {off.stderr.strip()[-200:]!r}"
            elif make and hint not in verified.stderr:
                why = f"FAIL verify did not name the 0.x registry: {verified.stderr.strip()[-200:]!r}"
            else:
                why = "ok"
            cells[(label, reader)] = why
    return cells


def other_user_controls(h: Harness, user: str) -> str:
    """Part 6's preflight: "ok", or why the cross-uid cells cannot be trusted.
    The identity switch must be real, and a mode must take effect for it."""
    probe = subprocess.run(as_user(user, h.env, ["id", "-u"]), capture_output=True, text=True, check=False)
    if probe.returncode != 0:
        return f"sudo -n -u {user} id -u exit {probe.returncode}: {probe.stderr.strip()[-160:]}"
    uid = probe.stdout.strip()
    if not uid.isdigit() or int(uid) in (0, os.getuid()):
        return f"{user} is uid {uid!r}, which is root or this user ({os.getuid()})"
    control = h.work / "xuid-control"
    control.mkdir()
    public, private = control / "public", control / "private"
    public.write_text("readable\n")
    os.chmod(public, 0o644)
    fd = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, b"private\n")
    os.close(fd)
    if stat.S_IMODE(os.stat(private).st_mode) != 0o600:
        return f"{private} is not 0600"
    read = subprocess.run(as_user(user, h.env, ["cat", str(public)]), capture_output=True, text=True, check=False)
    if read.returncode != 0 or read.stdout != "readable\n":
        return f"{user} cannot read a 0644 file here (exit {read.returncode}: {read.stderr.strip()[-160:]}); the work dir is not reachable"
    denied = subprocess.run(as_user(user, h.env, ["cat", str(private)]), capture_output=True, text=True, check=False)
    if denied.returncode == 0 or "Permission denied" not in denied.stderr:
        return f"{user} was not refused a 0600 file (exit {denied.returncode}: {denied.stderr.strip()[-160:]!r})"
    return "ok"


def cross_uid_rows(h: Harness, names: list[str], user: str, expected: dict[str, str]) -> dict[tuple[str, str], str]:
    """Part 6: a cache one uid writes, another uid reads (public issue #486)."""
    cells: dict[tuple[str, str], str] = {}
    control = other_user_controls(h, user)
    for writer in names:
        key = f"{writer} writes (as {os.getuid()})"
        if control != "ok":
            for reader in names:
                cells[(key, reader)] = f"INVALID control: {control}"
            continue
        cache = h.work / f"xuid-{writer}"
        done = h.fetch(writer, SPELLING, cache, offline=False)
        if done.returncode != 0:
            for reader in names:
                cells[(key, reader)] = f"FAIL (no cache: {writer} fetch exit {done.returncode}: {done.stderr.strip()[-160:]})"
            continue
        entry = Path(named_dir(done))
        record = entry / "verified.json"
        before = record.read_bytes()
        for reader in names:
            got = h.run(reader, "fetch", SPELLING, "--platform", PLATFORM, "--cache", str(cache), "--offline", user=user)
            listed = h.run(reader, "list", "--offline", "--cache", str(cache), user=user)
            verified = h.run(reader, "verify", "--cache", str(cache), user=user)
            if got.returncode != 0:
                why = f"FAIL fetch --offline as {user} exit {got.returncode}: {got.stderr.strip()[-200:]}"
            elif os.path.realpath(named_dir(got)) != os.path.realpath(entry):
                why = f"FAIL fetch --offline as {user} named {named_dir(got)!r}, not {entry}"
            elif listed.returncode != 0 or f"installed {expected['version']} {PLATFORM}" not in listed.stdout:
                why = f"FAIL list --offline as {user} exit {listed.returncode}, output {listed.stdout.strip()[-160:]!r}"
            elif verified.returncode != 0 or "verified 0 builds" in verified.stderr:
                why = f"FAIL verify as {user} exit {verified.returncode}: {verified.stderr.strip()[-160:]!r}"
            elif record.read_bytes() != before:
                why = "FAIL the record changed"
            else:
                why = "ok"
            cells[(key, reader)] = why
    return cells


FAULTS = ("none", "root-000", "unpacked-000", "entry-000", "record-000", "record-garbage-noblobs", "layout-0x")
# The reason strict mode gives each fault, and whether the default mode warns.
FAULT_REASON = {
    "root-000": ("unreadable_root", True),
    "unpacked-000": ("unreadable_root", True),
    "entry-000": ("unreadable_entry", True),
    "record-000": ("unreadable_entry", True),
    "record-garbage-noblobs": ("unacceptable_record", False),
    "layout-0x": ("layout_0x", False),
}
UNUSABLE = re.compile(r"(\S+) is unusable as a cache: ([a-z_0-9]+)")
# Every shared code (spec/fetch-v1/constants.json), so an answer with another
# code reads as that code, never as none.
ERROR_CODES = tuple(json.loads((ROOT / "spec" / "fetch-v1" / "constants.json").read_text(encoding="utf-8"))["errors"])
WARNED = re.compile(r"(\S+) could not be read \(([A-Z0-9 ]*)\)")


def observe(cmd: str, proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """What one command answered, read from its exit status and output only:
    the same shape for every binding, so readers can be compared."""
    m = UNUSABLE.search(proc.stderr)
    out: dict[str, Any] = {
        "exit": proc.returncode,
        "code": min(
            (c for c in ERROR_CODES if re.search(rf"\b{c}\b", proc.stderr)), key=proc.stderr.index, default=""
        ),
        "unusable": (m.group(1), m.group(2)) if m else None,
        "warned": sorted({w.group(1) for w in WARNED.finditer(proc.stderr)}),
    }
    if cmd == "fetch":
        out["answer"] = os.path.realpath(named_dir(proc)) if proc.returncode == 0 else ""
    elif cmd == "list":
        out["listed"] = sorted(os.path.realpath(ln.split()[-1]) for ln in proc.stdout.splitlines() if ln.startswith("installed "))
    else:
        out["verified_none"] = "verified 0 builds under" in proc.stderr
    return out


def expected_answer(
    cmd: str, fault: str, strict: bool, entry: Path, sys_entry: Path | None, faulted: Path | None
) -> dict[str, Any]:
    """What every reader must answer (docs/guides/fetch-v1.md §1, the cache
    faults): `faulted` is the path a fault names, `sys_entry` the system dir's
    copy of the build when one is configured."""
    reason, warns = FAULT_REASON.get(fault, ("", False))
    if fault != "none" and strict:
        base: dict[str, Any] = {"exit": 9, "code": "CHTYPES_CACHE_UNUSABLE", "unusable": (str(faulted), reason), "warned": []}
        return {**base, **({"answer": ""} if cmd == "fetch" else {"listed": []} if cmd == "list" else {"verified_none": False})}
    warned = [str(faulted)] if fault != "none" and warns else []
    cache_ok = fault == "none"
    answer = entry if cache_ok else sys_entry
    if cmd == "fetch":
        if answer is None:
            return {"exit": 1, "code": "CHTYPES_ARTIFACT_MISSING", "unusable": None, "warned": warned, "answer": ""}
        return {"exit": 0, "code": "", "unusable": None, "warned": warned, "answer": os.path.realpath(answer)}
    listed = sorted(os.path.realpath(p) for p in ([entry] if cache_ok else []) + ([sys_entry] if sys_entry else []))
    if cmd == "list":
        return {"exit": 0, "code": "", "unusable": None, "warned": warned, "listed": listed}
    return {"exit": 0, "code": "", "unusable": None, "warned": warned, "verified_none": not listed}


def apply_fault(fault: str, cache: Path, entry: Path) -> Path | None:
    """Faults a copy of a writer's cache, as this user. Returns the path the
    error and the warning must name (None for no fault)."""
    if fault == "none":
        return None
    if fault == "root-000":
        os.chmod(cache, 0)
        return cache
    if fault == "unpacked-000":
        target = cache / "unpacked" / "sha256"
        os.chmod(target, 0)
        return target
    if fault == "entry-000":
        os.chmod(entry, 0)
        return entry
    if fault == "record-000":
        os.chmod(entry / "verified.json", 0)
        return entry / "verified.json"
    if fault == "record-garbage-noblobs":
        (entry / "verified.json").write_text("not json {")
        shutil.rmtree(cache / "blobs", ignore_errors=True)
        return entry / "verified.json"
    if fault == "layout-0x":
        shutil.rmtree(cache)
        shutil.copytree(FIXTURES / "layouts" / "upgrade-0x-registry", cache)
        return cache
    raise ValueError(fault)


def unfault(path: Path) -> None:
    """Undoes the chmods under `path`, so its tree can be read again: the
    mode is restored before a directory is listed."""
    try:
        is_dir = path.is_dir() and not path.is_symlink()
        os.chmod(path, 0o755 if is_dir else 0o644)
    except OSError:
        return
    if is_dir:
        for child in path.iterdir():
            unfault(child)


def fault_control(h: Harness, user: str | None, fault: str, faulted: Path | None) -> str:
    """The positive control: the other user is refused the faulted path, so
    the fault is real for the reader. "ok", or why the row is INVALID."""
    if faulted is None or fault in ("record-garbage-noblobs", "layout-0x"):
        return "ok"
    probe = ["ls", str(faulted)] if fault != "record-000" else ["cat", str(faulted)]
    argv = probe if user is None else as_user(user, h.env, probe)
    got = subprocess.run(argv, capture_output=True, text=True, check=False)
    if got.returncode == 0 or "Permission denied" not in got.stderr:
        who = user or "this user"
        return f"{who} was not refused {faulted} (exit {got.returncode}: {got.stderr.strip()[-120:]!r})"
    return "ok"


def fault_rows(
    h: Harness, names: list[str], user: str | None, system_dir: Path | None
) -> tuple[dict[tuple[str, str], str], list[str]]:
    """Part 7: writer x reader x fault x mode, read as the other user (or, with
    --faults-as-self, a local development run, as this one: a mode denies the
    owner too). Returns the cells and one table title per (mode, system dir)
    block."""
    cells: dict[tuple[str, str], str] = {}
    blocks: list[str] = []
    for writer in names:
        pristine = h.work / f"nxn-{writer}"
        done = h.fetch(writer, SPELLING, pristine, offline=False)
        if done.returncode != 0:
            for fault in FAULTS:
                for mode in ("default", "strict"):
                    for with_sys in ((False, True) if system_dir else (False,)):
                        label = f"{mode}{' +system dir' if with_sys else ''}: {writer} writes, {fault}"
                        for reader in names:
                            cells[(label, reader)] = f"FAIL (no cache: {done.stderr.strip()[-120:]})"
            continue
        hexname = Path(named_dir(done)).name
        for fault in FAULTS:
            cache = h.work / f"nxn-{writer}-{fault}"
            shutil.copytree(pristine, cache)
            entry = cache / "unpacked" / "sha256" / hexname
            faulted = apply_fault(fault, cache, entry)
            control = fault_control(h, user, fault, faulted)
            for with_sys in ((False, True) if system_dir else (False,)):
                sys_entry = None
                if with_sys:
                    assert system_dir is not None
                    shutil.copytree(pristine / "unpacked", system_dir / "unpacked")
                    sys_entry = system_dir / "unpacked" / "sha256" / hexname
                for mode in ("default", "strict"):
                    label = f"{mode}{' +system dir' if with_sys else ''}: {writer} writes, {fault}"
                    title = f"{mode}{', with a system dir' if with_sys else ''}"
                    if title not in blocks:
                        blocks.append(title)
                    flag = ["--strict"] if mode == "strict" else []
                    answers: dict[str, list[dict[str, Any]]] = {}
                    for reader in names:
                        if control != "ok":
                            cells[(label, reader)] = f"INVALID control: {control}"
                            continue
                        runs = [
                            ("fetch", h.run(reader, "fetch", SPELLING, "--platform", PLATFORM, "--cache", str(cache), "--offline", *flag, user=user)),
                            ("list", h.run(reader, "list", "--offline", "--cache", str(cache), *flag, user=user)),
                            ("verify", h.run(reader, "verify", "--cache", str(cache), *flag, user=user)),
                        ]
                        got = [observe(cmd, proc) for cmd, proc in runs]
                        answers[reader] = got
                        want = [expected_answer(cmd, fault, mode == "strict", entry, sys_entry, faulted) for cmd, _ in runs]
                        bad = [f"{cmd}: got {g}, want {w}" for (cmd, _), g, w in zip(runs, got, want, strict=True) if g != w]
                        cells[(label, reader)] = "ok" if not bad else "FAIL " + "; ".join(bad)[:600]
                    # One answer: every reader's observation is the same.
                    if len({json.dumps(a, sort_keys=True) for a in answers.values()}) > 1:
                        for reader in answers:
                            if cells[(label, reader)] == "ok":
                                cells[(label, reader)] = "FAIL the readers disagree"
                if with_sys:
                    assert system_dir is not None
                    shutil.rmtree(system_dir / "unpacked")
            unfault(cache)
    return cells, blocks


def main_run(
    clis: dict[str, list[str]],
    other_user: str | None = None,
    system_dir: Path | None = None,
    faults_as_self: bool = False,
) -> int:
    cases = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))["cases"]
    line_ok = next(c for c in cases if c["id"] == CASE)
    expected = {"version": line_ok["expect"]["version"], "build": line_ok["expect"]["build"], "platform": PLATFORM}
    constants = json.loads((ROOT / "spec" / "fetch-v1" / "constants.json").read_text(encoding="utf-8"))
    missing_exit = constants["errors"]["CHTYPES_ARTIFACT_MISSING"]
    trusted = constants["test_keys"][0]["ed25519_hex"]
    schema = json.loads((ROOT / "spec" / "fetch-v1" / "schema" / "verified.schema.json").read_text(encoding="utf-8"))
    names = [b for b in BINDINGS if b in clis]

    server = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "fetch-v1" / "server.py"), "--fixtures", str(FIXTURES), "--port", "0"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert server.stdout is not None
        listening = server.stdout.readline().split()
        if len(listening) < 2 or listening[0] != "LISTENING":
            print(f"cache-interop: the fixture server did not start: {listening}", file=sys.stderr)
            return 1
        base = f"http://127.0.0.1:{listening[1]}/s-{CASE}/chtypes/v1"
        os.umask(UMASK)
        work = Path(tempfile.mkdtemp(prefix="cache-interop-"))
        # Traversable by another uid (part 6); everything under it is written at UMASK.
        os.chmod(work, 0o755)
        h = Harness(clis, base, trusted, work)

        cells: dict[tuple[str, str], str] = {}
        records: dict[str, dict[str, Any]] = {}
        caches: dict[str, Path] = {}
        failures = 0
        for writer in names:
            try:
                caches[writer], records[writer] = check_writer(h, writer, expected, schema)
            except RuntimeError as e:
                print(f"cache-interop: WRITER {writer}: {e}", file=sys.stderr)
                for reader in names:
                    cells[(f"{writer} writes", reader)] = "FAIL (no cache)"
                failures += 1
        for writer, cache in caches.items():
            for reader in names:
                cells[(f"{writer} writes", reader)] = check_reader(h, reader, cache, expected)
        first = next(iter(records), None)
        for writer, rec in records.items():
            if first is not None and (diff := records_equal(records[first], rec)):
                print(f"cache-interop: {writer}'s record differs from {first}'s in {diff}", file=sys.stderr)
                for reader in names:
                    cells[(f"{writer} writes", reader)] += f" [record differs from {first}: {','.join(diff)}]"
        cells.update(upgrade_rows(h, names, missing_exit, expected))
        lines_base = f"http://127.0.0.1:{listening[1]}/s-{LINES_CASE}/chtypes/v1"
        cells.update(line_rows(h, names, lines_base))
        cells.update(concurrency_rows(h, names))
        cells.update(read_only_rows(h, names, missing_exit))
        fault_blocks: list[str] = []
        if other_user is not None or faults_as_self:
            fault_cells, fault_blocks = fault_rows(h, names, other_user, system_dir)
            cells.update(fault_cells)
        if other_user is not None:
            cells.update(cross_uid_rows(h, names, other_user, expected))
        else:
            print("cache-interop: SKIPPED the cross-uid block (part 6): no --other-user given", file=sys.stderr)

        row_labels = [f"{w} writes" for w in names]
        table = render_table("writer \\ reader", row_labels, names, cells)
        up_rows = sorted({r for (r, _c) in cells if r.startswith("0.x ")})
        up_table = render_table("0.x upgrade \\ reader", up_rows, names, cells)
        line_tables = []
        for line in (LINE_LOW, LINE_HIGH):
            rows = [f"line {line}: {w} wrote {LINE_HIGH}" for w in names]
            line_tables.append(render_table(f"line {line}: writer \\ reader", rows, names, cells))
        conc_rows = [r for (r, c) in cells if c == "result"]
        conc_table = render_table(f"concurrent fetch {SPELLING}, {CONCURRENCY_ROUNDS} rounds", conc_rows, ["result"], cells)
        ro_rows = sorted({r for (r, _c) in cells if r.startswith("read-only commands: ")})
        ro_table = render_table("read-only commands \\ reader", ro_rows, names, cells)
        out = f"{table}\n\n{up_table}\n\n" + "\n\n".join(line_tables) + f"\n\n{conc_table}\n\n{ro_table}\n"
        if other_user is not None:
            xuid_rows = [f"{w} writes (as {os.getuid()})" for w in names]
            out += "\n" + render_table(f"cross-uid: writer \\ reader as {other_user}", xuid_rows, names, cells) + "\n"
        else:
            out += "\ncross-uid: SKIPPED (no --other-user)\n"
        for title in fault_blocks:
            prefix = title.replace(", with a system dir", " +system dir") + ": "
            rows = [r for r in dict.fromkeys(r for (r, _c) in cells) if r.startswith(prefix)]
            who = other_user or "this user"
            out += "\n" + render_table(f"faults, {title} (as {who}): writer, fault \\ reader", rows, names, cells) + "\n"
        print(out)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write("### v1-cache-interop\n\n```\n" + out + "```\n")
        bad = [(r, c, v) for (r, c), v in cells.items() if not v.startswith("ok")]
        for r, c, v in bad:
            print(f"FAILED {r} / {c}: {v}", file=sys.stderr)
        conc_sets = len(names) + (1 if len(names) > 1 else 0)
        expected_cells = len(names) * len(names) + 3 * len(names) + 2 * len(names) * len(names) + conc_sets
        expected_cells += 2 * len(names) + (len(names) * len(names) if other_user is not None else 0)
        if other_user is not None or faults_as_self:
            expected_cells += len(names) * len(FAULTS) * 2 * (2 if system_dir else 1) * len(names)
        if len(cells) != expected_cells:
            print(f"cache-interop: {len(cells)} cells, expected {expected_cells}", file=sys.stderr)
            return 1
        return 1 if bad or failures else 0
    finally:
        server.stdin and server.stdin.close()
        server.terminate()


# ============================================================ ABI v2 mode
#
# The module docstring's "ABI v2 MODE" says what each part proves. Everything
# a CLI is judged against comes from somewhere other than a CLI: the channel
# from scripts/release-channel.sh, what is served from the channel's registry
# itself, the record's shape from spec/fetch-v1/schema/verified.schema.json.

V2_ROUNDS = 3  # natural-clock rounds per concurrency set
V2_COARSE_ROUNDS = 2  # coarse-clock rounds per concurrency set
COARSE_GRAIN_NS = 100_000  # 100 µs, the grain the install-race probe used (public PR #516)
SHIM_SOURCE = ROOT / "scripts" / "fetch-v1" / "coarse-clock.c"
CHANNEL_SCRIPT = ROOT / "scripts" / "release-channel.sh"
VERIFIED_SCHEMA = ROOT / "spec" / "fetch-v1" / "schema" / "verified.schema.json"
MISSING_EXIT = json.loads((ROOT / "spec" / "fetch-v1" / "constants.json").read_text(encoding="utf-8"))["errors"][
    "CHTYPES_ARTIFACT_MISSING"
]
# The interpreters a CLI command line may pass through before the binding's
# own process image: a wrapper script's shell, or `env`.
SHELLS = {"bash", "sh", "dash", "zsh", "env"}
V2_USER_AGENT = "cache-interop.py (Wave-RF/chtypes CI)"


@dataclass(frozen=True)
class Channel:
    """The release channel the ABI v2 CLIs speak (spec/abi-v2/docs.md r5, r6)."""

    name: str
    base: str
    subroot: str
    record_schema: int
    key_id: str
    abi: int


def channel_from_script(script: Path = CHANNEL_SCRIPT) -> Channel:
    """The channel, read from the ONE file that defines it for this branch's
    releases (`scripts/release-channel.sh get <NAME>`), never typed here."""

    def get(name: str) -> str:
        out = subprocess.run(["bash", str(script), "get", name], capture_output=True, text=True, timeout=60, check=False)
        value = out.stdout.strip()
        if out.returncode != 0 or not value:
            raise RuntimeError(f"{script} get {name} exited {out.returncode}: {out.stderr.strip()[-200:]!r}")
        return value

    ch = Channel(
        name=get("CHANNEL"),
        base=get("REGISTRY_BASE"),
        subroot=get("CACHE_SUBROOT"),
        record_schema=int(get("RECORD_SCHEMA")),
        key_id=get("KEY_ID"),
        abi=int(get("ABI")),
    )
    if ch.subroot in ("", ".", "..") or "/" in ch.subroot:
        raise RuntimeError(f"the channel's cache subroot {ch.subroot!r} is not one directory name")
    if not ch.base.startswith("https://"):
        raise RuntimeError(f"the channel's registry {ch.base!r} is not https")
    return ch


class Registry:
    """What the channel's registry serves, read anonymously over its OCI API:
    its lines, and each line's manifest digest for a platform. Every writer is
    judged against this, never against another CLI's answer."""

    def __init__(self, base: str) -> None:
        scheme, _, rest = base.partition("://")
        host, _, repo = rest.partition("/")
        if scheme != "https" or not host or not repo:
            raise ValueError(f"not an https registry base with a repository path: {base!r}")
        self.api = f"{scheme}://{host}/v2/{repo}"

    def _get(self, url: str, accept: str) -> Any:
        req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": V2_USER_AGENT})
        last: Exception | None = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last = e
                if e.code < 500 and e.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                last = e
            time.sleep(2**attempt)
        raise RuntimeError(f"GET {url}: {last}")

    def lines(self) -> list[str]:
        """The two-part tags the registry lists, newest first."""
        tags = self._get(f"{self.api}/tags/list", "application/json").get("tags") or []
        two = [t for t in tags if re.fullmatch(r"[0-9]+\.[0-9]+", t)]
        return sorted(two, key=lambda t: tuple(int(x) for x in t.split(".")), reverse=True)

    def digest(self, line: str, platform: str) -> str:
        """The digest of the platform manifest the line's index names."""
        osname, _, arch = platform.partition("-")
        index = self._get(f"{self.api}/manifests/{line}", "application/vnd.oci.image.index.v1+json")
        found = [
            m.get("digest", "")
            for m in index.get("manifests", [])
            if (m.get("platform") or {}).get("os") == osname and (m.get("platform") or {}).get("architecture") == arch
        ]
        if len(found) != 1 or not re.fullmatch(r"sha256:[0-9a-f]{64}", found[0]):
            raise RuntimeError(f"the {line} index names {len(found)} {platform} manifests: {found}")
        return found[0]


def host_platform() -> str | None:
    """This host's platform key, when the registry can serve it (Linux only)."""
    if not sys.platform.startswith("linux"):
        return None
    return {"x86_64": "linux-amd64", "aarch64": "linux-arm64", "arm64": "linux-arm64"}.get(os.uname().machine)


class V2Harness:
    """Runs the ABI v2 CLIs: no CHTYPES_* variable reaches them (a dev CLI
    honors no override anyway, rule r6), HOME is a scratch directory, and
    every command names its cache explicitly."""

    def __init__(
        self, clis: dict[str, list[str]], work: Path, channel: Channel, platform: str, extra_env: dict[str, str] | None = None
    ) -> None:
        self.clis = clis
        self.work = work
        self.channel = channel
        self.platform = platform
        drop = ("XDG_CACHE_HOME", "LD_PRELOAD", "CACHE_INTEROP_EXEC_PRELOAD", "COARSE_CLOCK_LOG", "COARSE_CLOCK_NS")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("CHTYPES_") and k not in drop}
        self.env.update(extra_env or {})
        self.env["HOME"] = str(work / "home")
        (work / "home").mkdir(exist_ok=True)

    def layout(self, cache: Path) -> Path:
        return cache / self.channel.subroot

    def entry(self, cache: Path, digest: str) -> Path:
        return self.layout(cache) / "unpacked" / "sha256" / digest.split(":", 1)[1]

    def run(self, binding: str, *args: str, user: str | None = None, timeout: int = 900) -> subprocess.CompletedProcess[str]:
        argv = [*self.clis[binding], *args]
        if user is not None:
            argv = as_user(user, self.env, argv)
        try:
            return subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(argv, 124, "", f"cache-interop: timed out after {timeout}s")

    def fetch(self, binding: str, spelling: str, cache: Path, *, offline: bool, user: str | None = None) -> subprocess.CompletedProcess[str]:
        args = ["fetch", spelling, "--platform", self.platform, "--cache", str(cache)]
        if offline:
            args.append("--offline")
        return self.run(binding, *args, user=user)


def first_code(text: str) -> str:
    """The first shared error code a CLI's stderr names, or ""."""
    hits = [(m.start(), c) for c in ERROR_CODES for m in [re.search(rf"\b{c}\b", text)] if m]
    return min(hits)[1] if hits else ""


def first_error(proc: subprocess.CompletedProcess[str]) -> str:
    """A failure in one line: its code, and its first line of stderr."""
    first = next((ln.strip() for ln in proc.stderr.splitlines() if ln.strip()), "")
    code = first_code(proc.stderr)
    return f"{code + ': ' if code and code not in first else ''}{first[:240]}"


def installed_lines(proc: subprocess.CompletedProcess[str]) -> set[tuple[str, str, str]]:
    """`list`'s "installed <version> <platform> <dir>" lines, as (version, platform, real dir)."""
    out = set()
    for ln in proc.stdout.splitlines():
        parts = ln.split(maxsplit=3)
        if len(parts) == 4 and parts[0] == "installed":
            out.add((parts[1], parts[2], os.path.realpath(parts[3])))
    return out


def tree_stat(root: Path) -> dict[str, tuple[str, int, int, int]] | None:
    """Every path under `root`, `root` itself included as ".", with its kind,
    inode, size and mtime: None when `root` does not exist. A file rewritten
    in place, replaced by a rename, created or removed reads as a change;
    reading never does, and nothing is hashed."""
    if not os.path.lexists(root):
        return None
    out: dict[str, tuple[str, int, int, int]] = {}
    for p in [root, *sorted(root.rglob("*"))]:
        st = os.lstat(p)
        kind = "dir" if stat.S_ISDIR(st.st_mode) else "link" if stat.S_ISLNK(st.st_mode) else "file"
        out["." if p == root else str(p.relative_to(root))] = (kind, st.st_ino, st.st_size, st.st_mtime_ns)
    return out


def tree_changes(before: dict[str, Any] | None, after: dict[str, Any] | None) -> str:
    """"" when the two tree_stat snapshots agree, else what changed."""
    if before == after:
        return ""
    if before is None:
        return f"the missing root was created, holding {sorted(after or {})[:6]}"
    if after is None:
        return "the root was removed"
    created = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(p for p in set(before) & set(after) if before[p] != after[p])
    return f"created {created[:6]}, removed {removed[:6]}, changed {changed[:6]}"


def file_sig(path: Path) -> tuple[str, int, int]:
    """A record's bytes, inode and mtime: a reader that re-verified and
    replaced it, even byte-for-byte, changes the inode."""
    st = os.stat(path)
    return (hashlib.sha256(path.read_bytes()).hexdigest(), st.st_ino, st.st_mtime_ns)


def record_schema_for(channel: Channel) -> dict[str, Any]:
    """verified.schema.json with its `schema` constant set to the channel's
    record schema: rule r5 changes the record's schema number and nothing else
    (spec/abi-v2/docs.md r5, "Record schema 2"), so every other member is the
    schema-1 record's."""
    schema = copy.deepcopy(json.loads(VERIFIED_SCHEMA.read_text(encoding="utf-8")))
    schema["properties"]["schema"] = {"const": channel.record_schema}
    return schema


def v2_writer(h: V2Harness, digest: str, writer: str, line: str, cache: Path, schema: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    """`writer` fetches `line` online into a fresh `cache`. Returns the entry
    and its record, or raises naming what is wrong with them."""
    sys.path.insert(0, str(ROOT / "scripts" / "fetch-v1"))
    import schema_check  # type: ignore[import-not-found]

    done = h.fetch(writer, line, cache, offline=False)
    if done.returncode != 0:
        raise RuntimeError(f"fetch {line} exited {done.returncode}: {first_error(done)}")
    entry = h.entry(cache, digest)
    named = named_dir(done)
    if os.path.realpath(named) != os.path.realpath(entry):
        raise RuntimeError(
            f"fetch {line} named {named!r}, not the {h.channel.subroot} subroot's entry for the registry's {h.platform} manifest {digest} ({entry})"
        )
    outside = sorted(set(os.listdir(cache)) - {h.channel.subroot})
    if outside:
        raise RuntimeError(f"wrote outside its {h.channel.subroot} subroot (rule r5): {outside}")
    record = read_record(entry)
    if record is None:
        raise RuntimeError(f"{entry} holds no readable verified.json")
    try:
        schema_check.validate(record, schema, schema)
    except schema_check.SchemaError as e:
        raise RuntimeError(f"its verified.json breaks verified.schema.json at schema {h.channel.record_schema}: {e.message} (at {e.path})") from e
    lib = entry / record["library"]
    if not lib.is_file() or lib.stat().st_size != record["library_bytes"] or sha256_file(lib) != record["library_sha256"]:
        raise RuntimeError(f"the library {lib} is not its record's (library_sha256, library_bytes)")
    wants = {
        "digests.manifest": (record["digests"]["manifest"], digest),
        "signed_by": (record["signed_by"], h.channel.key_id),
        "predicate.abi": (record["predicate"].get("abi"), h.channel.abi),
        "platform": (record["platform"], h.platform),
    }
    for key, (got, want) in wants.items():
        if got != want:
            raise RuntimeError(f"its record has {key}={got!r}; the channel and its registry say {want!r}")
    if not within(line, record["version"]):
        raise RuntimeError(f"it answered {line} with {record['version']!r}")
    return entry, record


def v2_reader(
    h: V2Harness, reader: str, cache: Path, entry: Path, record: dict[str, Any], line: str, user: str | None = None
) -> str:
    """Part 1 and part 6's cell: `reader` (as `user`, when given) loads, lists,
    verifies and locates what a writer installed, and changes nothing."""
    who = f" as {user}" if user else ""
    before = tree_stat(cache)
    got = h.fetch(reader, line, cache, offline=True, user=user)
    listed = h.run(reader, "list", "--offline", "--cache", str(cache), user=user)
    verified = h.run(reader, "verify", "--cache", str(cache), user=user)
    where = h.run(reader, "where", "--cache", str(cache), user=user)
    after = tree_stat(cache)
    want_listed = {(record["version"], h.platform, os.path.realpath(entry))}
    if got.returncode != 0:
        return f"FAIL fetch --offline{who} exit {got.returncode}: {first_error(got)}"
    if os.path.realpath(named_dir(got)) != os.path.realpath(entry):
        return f"FAIL fetch --offline{who} named {named_dir(got)!r}, not {entry}"
    if listed.returncode != 0 or installed_lines(listed) != want_listed:
        return f"FAIL list --offline{who} exit {listed.returncode}, listed {sorted(installed_lines(listed))}, want {sorted(want_listed)}"
    if verified.returncode != 0 or "verified 0 builds" in verified.stderr or "MISMATCH" in verified.stderr:
        return f"FAIL verify{who} exit {verified.returncode}: {verified.stderr.strip()[-200:]!r}"
    if where.returncode != 0 or os.path.realpath(where.stdout.strip()) != os.path.realpath(h.layout(cache)):
        return f"FAIL where{who} exit {where.returncode} printed {where.stdout.strip()!r}, not {h.layout(cache)}"
    if changes := tree_changes(before, after):
        return f"FAIL the read-only commands{who} changed the cache: {changes}"
    return "ok"


def v2_answered(proc: subprocess.CompletedProcess[str], line: str, want: Path, what: str) -> str:
    """A fetch named `want`, whose record holds a build within `line`."""
    if proc.returncode != 0:
        return f"FAIL {what} exit {proc.returncode}: {first_error(proc)}"
    named = named_dir(proc)
    if os.path.realpath(named) != os.path.realpath(want):
        rec = read_record(Path(named)) if named else None
        return f"FAIL {what} named {named!r} ({(rec or {}).get('version')!r}), not {want}"
    rec = read_record(want)
    if rec is None or not within(line, str(rec.get("version", ""))):
        return f"FAIL {what} answered {line} with {(rec or {}).get('version')!r}"
    return "ok"


def v2_line_cells(
    h: V2Harness, writer: str, seed: Path, high_entry: Path, lines: tuple[str, str], low_digest: str, names: list[str]
) -> dict[tuple[str, str], str]:
    """Part 3: on its own copy of the writer's cache (which holds the higher
    line), each reader fetches the lower line ONLINE, then answers both lines
    offline. Every answer must be the registry's build of the line asked for."""
    high, low = lines
    cells: dict[tuple[str, str], str] = {}
    high_rel = high_entry.relative_to(seed)
    for reader in names:
        cache = h.work / f"lines-{writer}-{reader}"
        shutil.copytree(seed, cache, symlinks=True)
        try:
            high_record = cache / high_rel / "verified.json"
            before = file_sig(high_record)
            low_entry = h.entry(cache, low_digest)
            why = v2_answered(h.fetch(reader, low, cache, offline=False), low, low_entry, f"fetch {low}")
            if why == "ok":
                why = v2_answered(h.fetch(reader, low, cache, offline=True), low, low_entry, f"fetch {low} --offline")
            if why == "ok":
                records = sorted(h.layout(cache).glob("unpacked/sha256/*/verified.json"))
                if len(records) != 2:
                    why = f"FAIL the cache holds {len(records)} records, want one per line"
            cells[(f"line {low}: {writer} wrote {high}", reader)] = why
            why = v2_answered(h.fetch(reader, high, cache, offline=True), high, cache / high_rel, f"fetch {high} --offline")
            if why == "ok":
                listed = installed_lines(h.run(reader, "list", "--offline", "--cache", str(cache)))
                versions = sorted(v for v, _p, _d in listed)
                if len(listed) != 2 or not any(within(high, v) for v in versions) or not any(within(low, v) for v in versions):
                    why = f"FAIL list --offline listed {versions}, want one build of each line"
            if why == "ok" and file_sig(high_record) != before:
                why = f"FAIL the {high} record was rewritten or replaced"
            cells[(f"line {high}: {writer} wrote {high}", reader)] = why
        finally:
            shutil.rmtree(cache, ignore_errors=True)
    return cells


def clock_reach(log: Path, pids: list[int]) -> dict[int, tuple[bool, bool, str]]:
    """Per pid, from the coarse-clock shim's log: whether the CLI's own process
    image loaded the shim (the LAST image that pid loaded it into is not a
    shell), whether that image's clock was coarsened (it rounded a reading),
    and that image's executable."""
    events: dict[int, list[tuple[str, str]]] = {}
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    for ln in text.splitlines():
        m = re.fullmatch(r"(loaded|rounded) pid=(\d+) exe=(.*)", ln)
        if m:
            events.setdefault(int(m.group(2)), []).append((m.group(1), m.group(3)))
    out: dict[int, tuple[bool, bool, str]] = {}
    for pid in pids:
        evs = events.get(pid, [])
        last = max((i for i, (ev, _exe) in enumerate(evs) if ev == "loaded"), default=-1)
        exe = evs[last][1] if last >= 0 else ""
        in_cli = bool(exe) and os.path.basename(exe) not in SHELLS
        coarsened = in_cli and any(ev == "rounded" and e == exe for ev, e in evs[last + 1 :])
        out[pid] = (in_cli, coarsened, exe)
    return out


def build_shim(work: Path) -> tuple[Path | None, str]:
    """Compiles scripts/fetch-v1/coarse-clock.c into `work`: (path, "ok") or (None, why)."""
    so = work / "coarse-clock.so"
    cc = shutil.which("cc") or shutil.which("gcc")
    if cc is None:
        return None, "no C compiler (cc) on PATH"
    out = subprocess.run([cc, "-shared", "-fPIC", "-O2", "-o", str(so), str(SHIM_SOURCE), "-ldl"], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        return None, f"cc exited {out.returncode}: {out.stderr.strip()[-300:]}"
    return so, "ok"


CLOCK_PROBE = "import time\nprint(sum(1 for _ in range(20000) if time.time_ns() % {grain}))\n"


def shim_control(shim: Path, work: Path, grain_ns: int = COARSE_GRAIN_NS) -> str:
    """The coarse-clock leg's control: "ok", or why its rows are INVALID.
    Positive: under the shim at `grain_ns`, 20000 wall-clock readings are all
    multiples of COARSE_GRAIN_NS, and the shim logs that it loaded into the
    probe and rounded a reading. Negative: without the shim, the same readings
    are not all multiples, so the positive half can fail."""
    log = work / f"shim-control-{grain_ns}.log"
    probe = [sys.executable, "-c", CLOCK_PROBE.format(grain=COARSE_GRAIN_NS)]
    env = {**os.environ, "LD_PRELOAD": str(shim), "COARSE_CLOCK_LOG": str(log), "COARSE_CLOCK_NS": str(grain_ns)}
    p = subprocess.Popen(probe, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate(timeout=120)
    if p.returncode != 0 or out.strip() != "0":
        return f"under the shim, {out.strip() or '?'} of 20000 readings were not multiples of {COARSE_GRAIN_NS} ns (exit {p.returncode}: {err.strip()[-160:]!r})"
    in_cli, coarsened, exe = clock_reach(log, [p.pid])[p.pid]
    if not (in_cli and coarsened):
        return f"the shim's log does not show it loaded into and rounding the probe ({exe or 'no line'})"
    env = {k: v for k, v in os.environ.items() if k not in ("LD_PRELOAD", "COARSE_CLOCK_LOG", "COARSE_CLOCK_NS")}
    bare = subprocess.run(probe, env=env, capture_output=True, text=True, timeout=120, check=False)
    if bare.returncode != 0 or not bare.stdout.strip().isdigit() or int(bare.stdout.strip()) == 0:
        return f"without the shim every reading was already a multiple of {COARSE_GRAIN_NS} ns ({bare.stdout.strip()!r}), so the check cannot fail"
    return "ok"


def v2_round(
    h: V2Harness, members: list[str], cache: Path, line: str, digest: str, verifiers: list[str], shim: Path | None
) -> dict[str, Any]:
    """One concurrent round: len(members) processes released together, each
    fetching `line` online into one fresh `cache`, under the coarse-clock shim
    when `shim` is given. Returns per process (binding, ok, code, detail, in
    the CLI, coarsened), the watcher's events, and the verify failures."""
    env = dict(h.env)
    log = cache.parent / f"{cache.name}.clock.log"
    if shim is not None:
        env.update({"CACHE_INTEROP_EXEC_PRELOAD": str(shim), "COARSE_CLOCK_LOG": str(log), "COARSE_CLOCK_NS": str(COARSE_GRAIN_NS)})
    argvs = [[*h.clis[b], "fetch", line, "--platform", h.platform, "--cache", str(cache)] for b in members]
    layout = h.layout(cache)
    ran, events = run_together(argvs, env, cache, layout, timeout=900)
    want = h.entry(cache, digest)
    base = layout / "unpacked" / "sha256"
    hexes = [p for p in sorted(base.glob("*")) if HEX64.match(p.name)] if base.is_dir() else []
    lib_ok: dict[str, bool] = {}
    reach = clock_reach(log, [pid for pid, _proc in ran]) if shim is not None else {}
    procs: list[tuple[str, bool, str, str, bool, bool]] = []
    for binding, (pid, proc) in zip(members, ran, strict=True):
        in_cli, coarsened, _exe = reach.get(pid, (False, False, ""))
        if proc.returncode != 0:
            code = first_code(proc.stderr) or f"exit {proc.returncode}"
            procs.append((binding, False, code, first_error(proc), in_cli, coarsened))
            continue
        named = named_dir(proc)
        if len(hexes) != 1 or os.path.realpath(named) != os.path.realpath(want):
            procs.append((binding, False, "WRONG-ENTRY", f"named {named!r}; entries {[p.name[:12] for p in hexes]}, want {want.name[:12]}", in_cli, coarsened))
            continue
        if named not in lib_ok:
            rec = read_record(want) or {}
            lib = want / str(rec.get("library", ""))
            lib_ok[named] = lib.is_file() and sha256_file(lib) == rec.get("library_sha256")
        if not lib_ok[named]:
            procs.append((binding, False, "TORN-LIBRARY", f"the library under {want} is not its record's", in_cli, coarsened))
            continue
        procs.append((binding, True, "", "", in_cli, coarsened))
    if base.is_dir():
        events["LEFTOVER"] += sum(1 for p in base.iterdir() if not HEX64.match(p.name))
    if cache.is_dir():
        events["OUTSIDE-SUBROOT"] += sum(1 for p in cache.iterdir() if p.name != h.channel.subroot)
    verify_failures = []
    for vb in verifiers:
        v = h.run(vb, "verify", "--cache", str(cache))
        if v.returncode != 0 or "verified 0 builds" in v.stderr:
            verify_failures.append(f"{vb} verify on the final cache exit {v.returncode}: {v.stderr.strip()[-160:]!r}")
    shutil.rmtree(cache, ignore_errors=True)
    return {"procs": procs, "events": +events, "verify": verify_failures}


def v2_concurrency_cells(
    h: V2Harness, names: list[str], line: str, digest: str, processes: int, rounds: int, shim: Path | None, tag: str
) -> tuple[dict[tuple[str, str], str], dict[str, Counter[str]]]:
    """Part 4 (natural clock) or its coarse-clock leg: each binding alone, then
    all of them mixed, `processes` per round, `rounds` rounds per set. Returns
    the cells and, per binding, the counts (installs, failures by code, and
    under the shim, the processes it reached and the ones it coarsened)."""
    sets = [(f"{b} x{processes}", [b] * processes) for b in names]
    if len(names) > 1:
        sets.append((f"mixed x{processes} ({'+'.join(names)})", [names[i % len(names)] for i in range(processes)]))
    cells: dict[tuple[str, str], str] = {}
    totals: dict[str, Counter[str]] = {b: Counter() for b in names}
    for label, members in sets:
        per: dict[str, Counter[str]] = {b: Counter() for b in dict.fromkeys(members)}
        events: Counter[str] = Counter()
        verify: list[str] = []
        for rnd in range(rounds):
            progress(f"concurrent installs, {tag} clock: {label}, round {rnd + 1} of {rounds}")
            got = v2_round(h, members, h.work / f"conc-{tag}-{label.split()[0]}-{rnd}", line, digest, names, shim)
            events.update(got["events"])
            verify += [f"round {rnd + 1}: {v}" for v in got["verify"]]
            for binding, ok, code, detail, in_cli, coarsened in got["procs"]:
                c = per[binding]
                c["n"] += 1
                c["ok"] += ok
                c["in_cli"] += in_cli
                c["coarsened"] += coarsened
                if not ok:
                    c[f"code:{code}"] += 1
                    print(f"cache-interop: {tag} {label} round {rnd + 1}: {binding}: {code}: {detail}", file=sys.stderr)
        for v in verify:
            print(f"cache-interop: {tag} {label}: {v}", file=sys.stderr)
        parts = []
        bad = bool(events or verify)
        for binding, c in per.items():
            totals[binding].update(c)
            codes = ", ".join(f"{k[5:]} x{v}" for k, v in sorted(c.items()) if k.startswith("code:"))
            text = f"{binding} {c['ok']}/{c['n']} installed{' (failed: ' + codes + ')' if codes else ''}"
            if shim is not None:
                text += f", shim in the CLI {c['in_cli']}/{c['n']}, clock coarsened {c['coarsened']}/{c['n']}"
                if c["in_cli"] != c["n"]:
                    bad = True
            parts.append(text)
            bad = bad or c["ok"] != c["n"]
        ev = ", ".join(f"{k} x{v}" for k, v in sorted(events.items())) or "none"
        summary = "; ".join(parts) + f"; watcher events: {ev}" + (f"; {len(verify)} verify failure(s)" if verify else "")
        if shim is not None and any(per[b]["in_cli"] != per[b]["n"] for b in per):
            summary = "INVALID the shim did not reach every CLI process: " + summary
        cells[(label, "result")] = ("FAIL " if bad else "ok ") + summary
    return cells, totals


def v2_read_only_cells(h: V2Harness, names: list[str], lines: tuple[str, str]) -> dict[tuple[str, str], str]:
    """Part 5: list (offline and online), verify, where and fetch --offline
    create nothing, on an absent root, an empty root and an empty subroot."""
    cells: dict[tuple[str, str], str] = {}
    high = lines[0]
    for reader in names:
        for i, (label, prep) in enumerate((("absent root", 0), ("empty root", 1), ("empty subroot", 2))):
            root = h.work / f"ro-{reader}-{i}"
            if prep >= 1:
                root.mkdir()
            if prep == 2:
                (root / h.channel.subroot).mkdir()
            layout = os.path.join(str(root), h.channel.subroot)
            before = tree_stat(root)
            off = h.run(reader, "list", "--offline", "--cache", str(root))
            on = h.run(reader, "list", "--cache", str(root))
            verified = h.run(reader, "verify", "--cache", str(root))
            where = h.run(reader, "where", "--cache", str(root))
            fetched = h.fetch(reader, high, root, offline=True)
            after = tree_stat(root)
            published = set(on.stdout.split())
            if changes := tree_changes(before, after):
                why = f"FAIL a read-only command changed the tree: {changes}"
            elif off.returncode != 0 or off.stdout.strip():
                why = f"FAIL list --offline exit {off.returncode}, output {off.stdout.strip()[-120:]!r}"
            elif on.returncode != 0 or installed_lines(on) or not all(ln in published for ln in lines):
                why = f"FAIL list exit {on.returncode}, output {on.stdout.strip()[-160:]!r}, want both of {list(lines)} published and nothing installed"
            elif verified.returncode != 0 or f"verified 0 builds under {layout}" not in verified.stderr:
                why = f"FAIL verify exit {verified.returncode}, did not say it verified 0 builds under {layout}: {verified.stderr.strip()[-160:]!r}"
            elif where.returncode != 0 or where.stdout.strip() != layout:
                why = f"FAIL where exit {where.returncode} printed {where.stdout.strip()!r}, not {layout}"
            elif fetched.returncode != MISSING_EXIT or first_code(fetched.stderr) != "CHTYPES_ARTIFACT_MISSING":
                why = f"FAIL fetch --offline exit {fetched.returncode}, want {MISSING_EXIT} with CHTYPES_ARTIFACT_MISSING: {first_error(fetched)}"
            else:
                why = "ok"
            cells[(f"read-only commands: {label}", reader)] = why
            shutil.rmtree(root, ignore_errors=True)
    return cells


def progress(msg: str) -> None:
    """One line on stderr as each phase starts, so a long run shows where it is."""
    print(f"cache-interop: [{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def v2_expected_cells(n: int, other_user: bool, coarse: bool) -> int:
    sets = n + (1 if n > 1 else 0)
    return n * n + (n * n if other_user else 0) + 2 * n * n + sets + (sets if coarse else 0) + 3 * n


def main_run_v2(
    clis: dict[str, list[str]],
    *,
    other_user: str | None = None,
    coarse_clock: bool = False,
    platform: str | None = None,
    processes: int = CONCURRENCY,
    rounds: int = V2_ROUNDS,
    coarse_rounds: int = V2_COARSE_ROUNDS,
    channel: Channel | None = None,
    registry: Any = None,
    extra_env: dict[str, str] | None = None,
    work_base: Path | None = None,
    summary: bool = True,
) -> int:
    """The ABI v2 matrix. Exits 0 only when every cell is ok, every cell that
    should exist does, and the registry served the same builds at the end as
    at the start."""
    names = [b for b in BINDINGS if b in clis]
    channel = channel or channel_from_script()
    registry = registry or Registry(channel.base)
    platform = platform or host_platform()
    if platform is None:
        print("cache-interop: --platform is required off Linux (the registry serves linux builds only)", file=sys.stderr)
        return 2
    lines = registry.lines()
    if len(lines) < 2:
        print(f"cache-interop: the registry lists {lines}; the line rows need two lines", file=sys.stderr)
        return 1
    high, low = lines[0], lines[1]
    digests = {ln: registry.digest(ln, platform) for ln in (high, low)}
    print(f"cache-interop: ABI v2 matrix: channel {channel.name}, registry {channel.base}, cache subroot {channel.subroot}/, "
          f"record schema {channel.record_schema}, key {channel.key_id}, abi {channel.abi} (scripts/release-channel.sh)")
    print(f"cache-interop: platform {platform}; lines {high} ({digests[high]}) and {low} ({digests[low]}), read from the registry")
    os.umask(UMASK)
    work = Path(tempfile.mkdtemp(prefix="cache-interop-v2-", dir=work_base))
    os.chmod(work, 0o755)
    try:
        h = V2Harness(clis, work, channel, platform, extra_env)
        for b in names:
            v = h.run(b, "--version", timeout=120)
            print(f"cache-interop: {b}: `{shlex.join(clis[b])} --version` exit {v.returncode}: {v.stdout.strip() or v.stderr.strip()[-160:]!r}")
        schema = record_schema_for(channel)
        cells: dict[tuple[str, str], str] = {}
        records: dict[str, dict[str, Any]] = {}
        xuid = other_user_controls(h, other_user) if other_user is not None else "ok"  # type: ignore[arg-type]
        uid = os.getuid()
        for writer in names:
            progress(f"{writer} writes {high}; every reader reads it{', as ' + other_user + ' too' if other_user else ''}; then the line cells")
            cache = work / f"cache-{writer}"
            try:
                entry, record = v2_writer(h, digests[high], writer, high, cache, schema)
            except RuntimeError as e:
                print(f"cache-interop: WRITER {writer}: {e}", file=sys.stderr)
                for reader in names:
                    cells[(f"{writer} writes", reader)] = f"FAIL (no cache: {str(e)[:200]})"
                    if other_user is not None:
                        cells[(f"{writer} writes (as {uid})", reader)] = "FAIL (no cache)"
                    cells[(f"line {low}: {writer} wrote {high}", reader)] = "FAIL (no cache)"
                    cells[(f"line {high}: {writer} wrote {high}", reader)] = "FAIL (no cache)"
                shutil.rmtree(cache, ignore_errors=True)
                continue
            records[writer] = record
            for reader in names:
                cells[(f"{writer} writes", reader)] = v2_reader(h, reader, cache, entry, record, high)
            if other_user is not None:
                for reader in names:
                    key = (f"{writer} writes (as {uid})", reader)
                    cells[key] = f"INVALID control: {xuid}" if xuid != "ok" else v2_reader(h, reader, cache, entry, record, high, user=other_user)
            cells.update(v2_line_cells(h, writer, cache, entry, (high, low), digests[low], names))
            shutil.rmtree(cache, ignore_errors=True)
        first = next(iter(records), None)
        for writer, rec in records.items():
            if first is not None and (diff := records_equal(records[first], rec)):
                print(f"cache-interop: {writer}'s record differs from {first}'s in {diff}", file=sys.stderr)
                for reader in names:
                    cells[(f"{writer} writes", reader)] += f" [record differs from {first}: {','.join(diff)}]"
                    if not cells[(f"{writer} writes", reader)].startswith("FAIL"):
                        cells[(f"{writer} writes", reader)] = "FAIL " + cells[(f"{writer} writes", reader)]
        conc, natural = v2_concurrency_cells(h, names, high, digests[high], processes, rounds, None, "natural")
        cells.update(conc)
        coarse: dict[str, Counter[str]] = {}
        coarse_note = "coarse-clock leg: SKIPPED by name (no --coarse-clock)"
        if coarse_clock:
            progress("the coarse-clock leg: building the shim and running its control")
            shim, why = build_shim(work)
            if shim is not None:
                why = shim_control(shim, work)
            coarse_note = f"coarse-clock leg: CLOCK_REALTIME rounded down to {COARSE_GRAIN_NS} ns by scripts/fetch-v1/coarse-clock.c; control: {why}"
            if shim is None or why != "ok":
                sets = [f"{b} x{processes}" for b in names] + ([f"mixed x{processes} ({'+'.join(names)})"] if len(names) > 1 else [])
                for label in sets:
                    cells[(f"coarse: {label}", "result")] = f"INVALID control: {why}"
            else:
                got, coarse = v2_concurrency_cells(h, names, high, digests[high], processes, coarse_rounds, shim, "coarse")
                cells.update({(f"coarse: {r}", c): v for (r, c), v in got.items()})
        progress("the read-only commands")
        cells.update(v2_read_only_cells(h, names, (high, low)))
        moved = {ln: registry.digest(ln, platform) for ln in (high, low)}

        out = render_table(f"ABI v2, {high}: writer \\ reader", [f"{w} writes" for w in names], names, cells) + "\n"
        if other_user is not None:
            out += "\n" + render_table(f"cross-uid: writer \\ reader as {other_user}", [f"{w} writes (as {uid})" for w in names], names, cells) + "\n"
        else:
            out += "\ncross-uid: SKIPPED (no --other-user)\n"
        for ln in (low, high):
            out += "\n" + render_table(f"line {ln}: writer \\ reader", [f"line {ln}: {w} wrote {high}" for w in names], names, cells) + "\n"
        conc_rows = [r for (r, c) in cells if c == "result" and not r.startswith("coarse: ")]
        out += "\n" + render_table(f"concurrent fetch {high}, natural clock, {rounds} rounds", conc_rows, ["result"], cells) + "\n"
        coarse_rows = [r for (r, c) in cells if c == "result" and r.startswith("coarse: ")]
        out += f"\n{coarse_note}\n"
        if coarse_rows:
            out += render_table(f"concurrent fetch {high}, coarse clock, {coarse_rounds} rounds", coarse_rows, ["result"], cells) + "\n"
        count_cells: dict[tuple[str, str], str] = {}
        for b in names:
            n = natural.get(b, Counter())
            count_cells[(b, "natural")] = f"{n['n'] - n['ok']}/{n['n']} failed"
            c = coarse.get(b)
            count_cells[(b, "coarse clock")] = (
                f"{c['n'] - c['ok']}/{c['n']} failed, coarsened {c['coarsened']}/{c['n']}" if c else "not run"
            )
        out += "\n" + render_table("concurrent installs per binding", names, ["natural", "coarse clock"], count_cells) + "\n"
        ro_rows = sorted({r for (r, _c) in cells if r.startswith("read-only commands: ")})
        out += "\n" + render_table("read-only commands \\ reader", ro_rows, names, cells) + "\n"
        print(out)
        if summary and os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                f.write("### v1-cache-interop: the ABI v2 matrix\n\n```\n" + out + "```\n")
        bad = [(r, c, v) for (r, c), v in cells.items() if not v.startswith("ok")]
        for r, c, v in bad:
            print(f"FAILED {r} / {c}: {v}", file=sys.stderr)
        rc = 1 if bad else 0
        want = v2_expected_cells(len(names), other_user is not None, coarse_clock)
        if len(cells) != want:
            print(f"cache-interop: {len(cells)} cells, expected {want}", file=sys.stderr)
            rc = 1
        if moved != digests:
            print(f"cache-interop: the registry moved during the run ({digests} at the start, {moved} at the end): "
                  "every cell was judged against the start; rerun", file=sys.stderr)
            rc = 1
        return rc
    finally:
        shutil.rmtree(work, ignore_errors=True)


# A stand-in for one ABI v2 CLI, for the selftest only: the same commands and
# output shapes, a schema-2 record in the subroot, an install by renaming a
# complete temporary directory, and three planted defects (FAKE_V2_PLANT,
# "<defect>:<binding>"): `reader-rejects` reads no record at all,
# `concurrent` installs by a mkdir that a second installer fails on, and
# `ro-creates` makes the subroot on `list`.
FAKE_V2_CLI = r'''
import hashlib, json, os, shutil, sys, time
binding, args = sys.argv[1], sys.argv[2:]
cfg = json.load(open(os.environ["FAKE_V2_CONFIG"]))
plant = os.environ.get("FAKE_V2_PLANT", "")
time.time()  # one wall-clock read, as a real CLI makes

def take(name):
    if name in args:
        i = args.index(name)
        value = args[i + 1]
        del args[i:i + 2]
        return value
    return None

def has(name):
    if name in args:
        args.remove(name)
        return True
    return False

if args == ["--version"]:
    print("chtypes fake-" + binding)
    sys.exit(0)
cmd = args.pop(0)
cache, platform, offline = take("--cache"), take("--platform"), has("--offline")
layout = os.path.join(cache, cfg["subroot"])
base = os.path.join(layout, "unpacked", "sha256")

def within(req, version):
    want, got = req.split("."), version.split(".")
    return got[:len(want)] == want

def installed():
    try:
        names = sorted(os.listdir(base))
    except FileNotFoundError:
        return []
    out = []
    for n in names:
        try:
            rec = json.load(open(os.path.join(base, n, "verified.json")))
        except (OSError, ValueError):
            continue
        if len(n) == 64 and rec.get("schema") == cfg["schema"] and plant != "reader-rejects:" + binding:
            out.append((n, rec))
    return out

if cmd == "where":
    print(layout)
elif cmd == "list":
    if plant == "ro-creates:" + binding:
        os.makedirs(layout, exist_ok=True)
    for n, rec in installed():
        print("installed %s %s %s" % (rec["version"], rec["platform"], os.path.join(base, n)))
    if not offline:
        for line in cfg["lines"]:
            print("published %s support unknown" % line)
elif cmd == "verify":
    found = installed()
    if not found:
        print("chtypes: verified 0 builds under " + layout, file=sys.stderr)
    for n, rec in found:
        lib = open(os.path.join(base, n, rec["library"]), "rb").read()
        if hashlib.sha256(lib).hexdigest() != rec["library_sha256"]:
            print("MISMATCH " + n, file=sys.stderr)
            sys.exit(1)
elif cmd == "fetch":
    spelling = args[0]
    hits = [(n, rec) for n, rec in installed() if within(spelling, rec["version"])]
    if hits:
        print(os.path.join(base, hits[0][0]))
        sys.exit(0)
    line = cfg["lines"].get(".".join(spelling.split(".")[:2]))
    if offline or line is None:
        print("chtypes: no installed artifact for %s [CHTYPES_ARTIFACT_MISSING]" % spelling, file=sys.stderr)
        sys.exit(1)
    final = os.path.join(base, line["digest"].split(":")[1])
    lib = ("fake library " + line["version"]).encode() * 4096
    rec = {"schema": cfg["schema"], "platform": platform, "version": line["version"], "channel": None,
           "build": "20261006.000000", "library": "libchtypes.so",
           "library_sha256": hashlib.sha256(lib).hexdigest(), "library_bytes": len(lib),
           "digests": {"index": None, "manifest": line["digest"], "layer": line["digest"], "bundle": None,
                       "bundle_manifest": None},
           "signed_by": cfg["key_id"], "predicate": {"abi": cfg["abi"]}}
    os.makedirs(base, exist_ok=True)
    if plant == "concurrent:" + binding:
        try:
            os.mkdir(final)
        except FileExistsError:
            print("chtypes: %s is unusable as a cache: unwritable (EEXIST) [CHTYPES_CACHE_UNUSABLE]" % final, file=sys.stderr)
            sys.exit(9)
        tmp = final
    else:
        tmp = os.path.join(base, ".tmp-%d-%s" % (os.getpid(), os.urandom(6).hex()))
        os.mkdir(tmp)
    open(os.path.join(tmp, "libchtypes.so"), "wb").write(lib)
    open(os.path.join(tmp, "verified.json"), "w").write(json.dumps(rec))
    if tmp != final:
        try:
            os.rename(tmp, final)
        except OSError:
            shutil.rmtree(tmp)
    print(final)
else:
    print("chtypes: unknown command " + cmd, file=sys.stderr)
    sys.exit(2)
'''


class FakeRegistry:
    """The selftest's registry: fixed lines and digests, no network."""

    def __init__(self, served: dict[str, dict[str, str]]) -> None:
        self.served = served

    def lines(self) -> list[str]:
        return sorted(self.served, key=lambda t: tuple(int(x) for x in t.split(".")), reverse=True)

    def digest(self, line: str, platform: str) -> str:
        return self.served[line]["digest"]


def selftest_v2() -> None:
    """The ABI v2 matrix's own proof, against stand-in CLIs and no network: a
    clean run passes every cell, and each planted defect turns the run red in
    exactly the cells that hold it."""
    ch = channel_from_script()
    assert ch.subroot and "/" not in ch.subroot and ch.record_schema >= 1 and ch.base.startswith("https://"), ch
    try:
        channel_from_script(ROOT / "scripts" / "no-such-channel-script.sh")
        raise AssertionError("an unreadable channel script must refuse, never default")
    except RuntimeError:
        pass
    assert Registry("https://r.example/chtypes/v2-dev").api == "https://r.example/v2/chtypes/v2-dev"
    schema = record_schema_for(ch)
    assert schema["properties"]["schema"] == {"const": ch.record_schema}
    assert json.loads(VERIFIED_SCHEMA.read_text(encoding="utf-8"))["properties"]["schema"] == {"const": 1}, "the file is never edited"
    assert first_code("chtypes: x [CHTYPES_ARTIFACT_MISSING]; then CHTYPES_CACHE_UNUSABLE") == "CHTYPES_ARTIFACT_MISSING"
    assert installed_lines(subprocess.CompletedProcess([], 0, "installed 26.9.8.3 linux-amd64 /x/y\npublished 26.9 x\n", "")) == {
        ("26.9.8.3", "linux-amd64", os.path.realpath("/x/y"))
    }
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "r"
        assert tree_stat(root) is None
        root.mkdir()
        (root / "f").write_text("1")
        before = tree_stat(root)
        os.replace(root / "f", root / "g")
        (root / "f").write_text("1")
        os.remove(root / "g")
        assert tree_changes(before, tree_stat(root)), "a file replaced by an identical one is a change (its inode)"
        (root / "v2-dev").mkdir()
        assert "created ['v2-dev']" in tree_changes(before, tree_stat(root)), tree_changes(before, tree_stat(root))
        log = Path(d) / "clock.log"
        log.write_text(
            "loaded pid=7 exe=/usr/bin/bash\nrounded pid=7 exe=/usr/bin/bash\nloaded pid=7 exe=/usr/bin/node\nrounded pid=7 exe=/usr/bin/node\n"
            "loaded pid=8 exe=/usr/bin/bash\nloaded pid=9 exe=/x/cli-rust\nloaded pid=10 exe=/x/cli-go\n"
        )
        got = clock_reach(log, [7, 8, 9, 10, 11])
        assert got[7] == (True, True, "/usr/bin/node"), got
        assert got[8][0] is False, "a wrapper whose CLI never loaded the shim: the shim did not reach the CLI"
        assert got[9] == (True, False, "/x/cli-rust") and got[11] == (False, False, ""), got

    served = {
        "26.9": {"version": "26.9.8.3", "digest": "sha256:" + "9" * 64},
        "26.8": {"version": "26.8.15.10", "digest": "sha256:" + "8" * 64},
    }
    fake_channel = Channel(name="v2-dev", base="https://fake.invalid/chtypes/v2-dev", subroot="v2-dev", record_schema=2, key_id="0123456789abcdef", abi=2)

    def run(names: list[str], plant: str, **kw: Any) -> tuple[int, dict[str, str], str]:
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "fake-chtypes.py"
            fake.write_text(FAKE_V2_CLI)
            config = Path(d) / "config.json"
            config.write_text(json.dumps({"subroot": "v2-dev", "schema": 2, "key_id": fake_channel.key_id, "abi": 2, "lines": served}))
            clis = {b: [sys.executable, str(fake), b] for b in names}
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = main_run_v2(
                    clis, platform="linux-amd64", channel=fake_channel, registry=FakeRegistry(served), rounds=1, coarse_rounds=1,
                    extra_env={"FAKE_V2_CONFIG": str(config), "FAKE_V2_PLANT": plant}, work_base=Path(d), summary=False, **kw,
                )
            failed = dict(re.findall(r"^FAILED (.+? / (?:go|python|ts|rust|result)): (.*)$", buf_err.getvalue(), re.MULTILINE))
            return rc, failed, buf_out.getvalue() + buf_err.getvalue()

    rc, failed, log_text = run(list(BINDINGS), "")
    assert rc == 0 and not failed, f"positive control: four sound CLIs must pass every cell (rc {rc}):\n{log_text[-3000:]}"
    assert "go 8/8 installed" in log_text and "mixed x8 (go+python+ts+rust)" in log_text, log_text[-2000:]
    assert "coarse-clock leg: SKIPPED by name" in log_text, "without --coarse-clock the leg says so by name"
    # Each plant: the run exits nonzero, the named cell fails, and every
    # failing cell is one the defect can reach (a reader that reads nothing
    # also fails the final-cache verify of every concurrency set).
    plants: list[tuple[str, list[str], str, Callable[[str], bool]]] = [
        ("reader-rejects:ts", ["go", "ts"], "go writes / ts", lambda row: row.endswith(" / ts") or row.endswith(" / result")),
        ("concurrent:rust", ["go", "rust"], "rust x8 / result", lambda row: row in ("rust x8 / result", "mixed x8 (go+rust) / result")),
        ("ro-creates:go", ["go", "rust"], "read-only commands: absent root / go", lambda row: row.startswith("read-only commands: ") and row.endswith(" / go")),
    ]
    for plant, names, must, reachable in plants:
        rc, failed, log_text = run(names, plant)
        assert rc != 0, f"planted {plant}: the run must exit nonzero:\n{log_text[-3000:]}"
        assert must in failed, f"planted {plant}: {must!r} must fail, got {sorted(failed)}"
        assert all(reachable(row) for row in failed), f"planted {plant}: a cell the defect cannot reach failed: {sorted(failed)}"
        if plant.startswith("concurrent:"):
            assert "CHTYPES_CACHE_UNUSABLE" in failed[must], f"a concurrent failure names its exact code: {failed[must]}"
        if plant.startswith("ro-creates:"):
            assert "created" in failed[must], f"a read-only command that creates a directory names it: {failed[must]}"

    # The coarse-clock leg: LD_PRELOAD is Linux's, so its own proof runs there.
    if not sys.platform.startswith("linux"):
        print("cache-interop --selftest: the coarse-clock checks need Linux (LD_PRELOAD): SKIPPED here; CI runs them", file=sys.stderr)
        return
    with tempfile.TemporaryDirectory() as d:
        shim, why = build_shim(Path(d))
        assert shim is not None, f"the shim must build: {why}"
        good = shim_control(shim, Path(d))
        assert good == "ok", f"positive control: the shim must coarsen the clock: {good}"
        planted = shim_control(shim, Path(d), grain_ns=0)
        assert planted != "ok" and "not multiples" in planted, f"a shim that rounds nothing must fail the control: {planted}"
    rc, failed, log_text = run(["python"], "", coarse_clock=True)
    assert rc == 0 and "shim in the CLI 8/8, clock coarsened 8/8" in log_text, f"coarse leg, positive control:\n{log_text[-3000:]}"
    # Planted: the CLI is reached through `env -u LD_PRELOAD`, so the shim
    # loads into `env` and never into the CLI. The leg must call that INVALID.
    env_cli = shutil.which("env")
    assert env_cli is not None
    with tempfile.TemporaryDirectory() as d:
        fake = Path(d) / "fake-chtypes.py"
        fake.write_text(FAKE_V2_CLI)
        config = Path(d) / "config.json"
        config.write_text(json.dumps({"subroot": "v2-dev", "schema": 2, "key_id": fake_channel.key_id, "abi": 2, "lines": served}))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = main_run_v2(
                {"python": [env_cli, "-u", "LD_PRELOAD", sys.executable, str(fake), "python"]},
                platform="linux-amd64", channel=fake_channel, registry=FakeRegistry(served), rounds=1, coarse_rounds=1,
                extra_env={"FAKE_V2_CONFIG": str(config)}, work_base=Path(d), summary=False, coarse_clock=True,
            )
        assert rc != 0 and "INVALID the shim did not reach every CLI process" in buf.getvalue(), buf.getvalue()[-3000:]


def selftest() -> None:
    a = {"schema": 1, "build": "1", "digests": {"x": None}}
    b = {"digests": {"x": None}, "build": "1", "schema": 1}
    assert records_equal(a, b) == [], "records that differ only in key order must be equal"
    assert records_equal(a, {**b, "build": "2"}) == ["build"], "a differing member must be named"
    assert records_equal(a, {k: v for k, v in b.items() if k != "schema"}) == ["schema"], "a missing member must be named"
    table = render_table("w \\ r", ["go writes"], ["go", "rust"], {("go writes", "go"): "ok", ("go writes", "rust"): "FAIL x"})
    assert "FAIL x" in table and "go writes" in table, table
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "a").mkdir()
        (root / "a" / "f").write_text("1")
        before = tree_hashes(root)
        (root / "a" / "f").write_text("2")
        assert tree_hashes(root) != before, "tree_hashes must see a changed file"
        (root / "c").mkdir()
        (root / "c" / "verified.json").write_text("{}")
        try:
            only_record(root / "c")
            raise AssertionError("only_record must refuse a cache with no unpacked directory")
        except RuntimeError:
            pass
    assert within("26.3", "26.3.4.1") and within("26.3.4.1", "26.3.4.1"), "a build within its line must match"
    assert not within("26.3", "26.8.5.1"), "another line must not match"
    assert not within("26.3", "26.30.1.1"), "a line is a component prefix, never a string prefix"
    assert not within("26.3.4.1", "26.3.9.1"), "an exact request matches only that version"
    assert named_dir(subprocess.CompletedProcess([], 0, "warning\n/x/y\n\n", "")) == "/x/y"
    # The watcher must see each kind of state a concurrent reader can meet.
    with tempfile.TemporaryDirectory() as d:
        cache = Path(d)
        base = cache / "unpacked" / "sha256"
        name = "a" * 64

        def make(entry: Path, *, record: str | None, library: bool) -> None:
            entry.mkdir(parents=True)
            if library:
                (entry / "lib.so").write_bytes(b"12345")
            if record is not None:
                (entry / "verified.json").write_text(record)

        good = json.dumps({"library": "lib.so", "library_bytes": 5})
        w = Watcher(cache)
        w.scan()
        assert not w.events, w.events
        make(base / name, record=good, library=True)
        (base / ".staging-1").mkdir()
        w.scan()
        assert not w.events, f"a complete entry and a dot-named temp dir are not events: {w.events}"
        (base / name).rename(base / ".aside")
        make(base / name, record=good, library=True)
        w.scan()
        assert w.events["REPLACED"] == 1, f"a complete entry under a new inode is REPLACED: {w.events}"
        (base / name / "lib.so").unlink()
        w.scan()
        assert w.events["VANISHED"] == 1 and w.events["TORN-LIBRARY"] == 1, f"a record without its library: {w.events}"
        (base / "not-hex").mkdir()
        make(base / ("c" * 64), record=None, library=True)
        make(base / ("d" * 64), record="not json {", library=True)
        w.scan()
        assert w.events["UNRECORDED"] == 1 and w.events["TORN-RECORD"] == 1, f"unrecorded and torn records: {w.events}"
        assert entry_state(base / ".aside") == "complete"
    # Part 5's tree comparison must see an empty directory appear, and a
    # missing root stay None.
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "r"
        assert tree_state(root) is None
        root.mkdir()
        (root / "f").write_text("1")
        before = tree_state(root)
        (root / "blobs" / "sha256").mkdir(parents=True)
        assert tree_state(root) != before and tree_state(root)["blobs/sha256"] == "dir", "a new empty dir is a change"
    # Part 6's sudo line carries the fetch environment and nothing else.
    argv = as_user("nobody", {"CHTYPES_CACHE": "/c", "PATH": "/bin", "HOME": "/h", "SECRET": "x"}, ["cli", "verify"])
    assert argv[:6] == ["sudo", "-n", "-u", "nobody", "env", "-i"], argv
    assert "SECRET=x" not in argv and "CHTYPES_CACHE=/c" in argv and argv[-2:] == ["cli", "verify"], argv
    assert ZERO_X_HINT.format(root="/x").startswith("/x holds a 0.x registry (26.1/manifest.json)")
    assert "${XDG_CACHE_HOME:-~/.cache}/chtypes/v1" in ZERO_X_HINT.format(root="/x")
    # Part 7 reads every binding's answer the same way, and expects one answer.
    cp = subprocess.CompletedProcess
    strict_err = cp([], 9, "", "chtypes: CHTYPES_CACHE_UNUSABLE: /c/unpacked/sha256 is unusable as a cache: unreadable_root (EACCES)\n")
    got = observe("fetch", strict_err)
    assert got == expected_answer("fetch", "unpacked-000", True, Path("/c/e"), None, Path("/c/unpacked/sha256")), got
    missing = cp([], 1, "", "chtypes: no installed artifact for 26.8. /c could not be read (EACCES); treated as not installed. Set CHTYPES_CACHE_STRICT=1 to make this an error. [CHTYPES_ARTIFACT_MISSING]\n")
    got = observe("fetch", missing)
    assert got == expected_answer("fetch", "root-000", False, Path("/c/e"), None, Path("/c")), got
    assert got != expected_answer("fetch", "entry-000", False, Path("/c/e"), None, Path("/c/e")), "another path is another answer"
    corrupt = cp([], 1, "", "chtypes: CHTYPES_ARTIFACT_CORRUPT: JSON: missing field\n")
    assert observe("fetch", corrupt)["code"] == "CHTYPES_ARTIFACT_CORRUPT", "another code reads as that code"
    hint = cp([], 1, "", "x holds a 0.x registry; point CHTYPES_CACHE at an empty dir. Set CHTYPES_CACHE_STRICT=1 [CHTYPES_ARTIFACT_MISSING]\n")
    assert observe("fetch", hint)["code"] == "CHTYPES_ARTIFACT_MISSING", "an environment variable is not a code"
    with tempfile.TemporaryDirectory() as d:
        listed = cp([], 0, f"installed 26.8.15.10 linux-arm64 {d}\n", "")
        assert observe("list", listed) == expected_answer("list", "none", False, Path(d), None, None)
        silent = cp([], 0, "", "")
        assert observe("verify", silent) != expected_answer("verify", "root-000", False, Path(d), None, Path(d)), "a silent verify is not the answer"
    selftest_v2()
    print("cache-interop --selftest: OK")


def main() -> int:
    argv = sys.argv[1:]
    if argv == ["--selftest"]:
        selftest()
        return 0
    clis: dict[str, list[str]] = {}
    other_user: str | None = None
    system_dir: Path | None = None
    faults_as_self = False
    abi = 1
    coarse_clock = False
    platform: str | None = None
    counts: dict[str, int] = {}
    i = 0
    while i < len(argv):
        if argv[i] == "--cli" and i + 1 < len(argv) and "=" in argv[i + 1]:
            name, _, cmd = argv[i + 1].partition("=")
            if name not in BINDINGS:
                print(f"cache-interop: unknown binding {name!r}", file=sys.stderr)
                return 2
            clis[name] = shlex.split(cmd)
            i += 2
        elif argv[i] == "--other-user" and i + 1 < len(argv):
            other_user = argv[i + 1]
            i += 2
        elif argv[i] == "--system-dir" and i + 1 < len(argv):
            system_dir = Path(argv[i + 1])
            i += 2
        elif argv[i] == "--faults-as-self":
            faults_as_self = True
            i += 1
        elif argv[i] == "--abi" and i + 1 < len(argv) and argv[i + 1] in ("1", "2"):
            abi = int(argv[i + 1])
            i += 2
        elif argv[i] == "--coarse-clock":
            coarse_clock = True
            i += 1
        elif argv[i] == "--platform" and i + 1 < len(argv):
            platform = argv[i + 1]
            i += 2
        elif argv[i] in ("--processes", "--rounds", "--coarse-rounds") and i + 1 < len(argv) and argv[i + 1].isdigit() and int(argv[i + 1]) > 0:
            counts[argv[i][2:].replace("-", "_")] = int(argv[i + 1])
            i += 2
        else:
            print(f"cache-interop: unknown argument {argv[i]!r}", file=sys.stderr)
            return 2
    if not clis:
        print("usage: cache-interop.py [--abi 2] --cli <binding>=<command> ... | --selftest", file=sys.stderr)
        return 2
    if abi == 2:
        if system_dir is not None or faults_as_self:
            print("cache-interop: --system-dir and --faults-as-self belong to the 1.x rows, not --abi 2", file=sys.stderr)
            return 2
        if coarse_clock and not sys.platform.startswith("linux"):
            print("cache-interop: --coarse-clock needs Linux (LD_PRELOAD)", file=sys.stderr)
            return 2
        return main_run_v2(clis, other_user=other_user, coarse_clock=coarse_clock, platform=platform, **counts)
    if coarse_clock or platform is not None or counts:
        print("cache-interop: --coarse-clock, --platform, --processes and the round counts belong to --abi 2", file=sys.stderr)
        return 2
    if system_dir is not None:
        constants = json.loads((ROOT / "spec" / "fetch-v1" / "constants.json").read_text(encoding="utf-8"))
        if str(system_dir) not in constants["cache"]["system_dirs"]:
            print(f"cache-interop: --system-dir must be one of the default system dirs {constants['cache']['system_dirs']}", file=sys.stderr)
            return 2
        if not system_dir.is_dir() or any(system_dir.iterdir()) or not os.access(system_dir, os.W_OK):
            print(f"cache-interop: --system-dir {system_dir} must exist, be empty and be writable by this user", file=sys.stderr)
            return 2
    return main_run(clis, other_user, system_dir, faults_as_self)


if __name__ == "__main__":
    sys.exit(main())
