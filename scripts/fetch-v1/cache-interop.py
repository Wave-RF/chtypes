#!/usr/bin/env python3
r"""scripts/fetch-v1/cache-interop.py — a cache one binding writes, every binding reads
(docs/guides/fetch-v1.md §1, "The verified.json record").

    scripts/fetch-v1/cache-interop.py --cli go=<cmd> --cli python=<cmd> --cli ts=<cmd> --cli rust=<cmd>
                                      [--other-user <name>]
    scripts/fetch-v1/cache-interop.py --selftest

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

Every process runs at umask 022, set here. Prints the 4x4 table, the 0.x
rows, one table per line, the concurrency sets, the read-only rows and the
cross-uid table, writes the same to $GITHUB_STEP_SUMMARY when set, and exits
nonzero if any cell fails. Without --other-user the cross-uid block is skipped
LOUDLY, by name.
"""

from __future__ import annotations

import hashlib
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
from collections import Counter
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
# then wait for the barrier file, so all of them start together.
BARRIER = (
    "import os, sys, time\n"
    "open(sys.argv[1], 'w').close()\n"
    "deadline = time.monotonic() + 120\n"
    "while not os.path.exists(sys.argv[2]):\n"
    "    if time.monotonic() > deadline:\n"
    "        sys.exit(97)\n"
    "    time.sleep(0.0005)\n"
    "os.execvp(sys.argv[3], sys.argv[3:])\n"
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def concurrent_round(h: Harness, members: list[str], cache: Path) -> tuple[int, list[str], Counter[str]]:
    """One round: len(members) processes released together, each fetching
    SPELLING into `cache`. Returns how many installed a verified library, the
    failures, and the watcher's events (plus any leftover)."""
    ready = cache.parent / f"{cache.name}.ready"
    barrier = cache.parent / f"{cache.name}.go"
    ready.mkdir()
    procs = []
    for i, binding in enumerate(members):
        argv = [*h.clis[binding], "fetch", SPELLING, "--platform", PLATFORM, "--cache", str(cache)]
        procs.append((binding, subprocess.Popen(
            [sys.executable, "-c", BARRIER, str(ready / str(i)), str(barrier), *argv],
            env=h.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )))
    deadline = time.monotonic() + 120
    while len(os.listdir(ready)) < len(members) and time.monotonic() < deadline:
        time.sleep(0.01)
    watcher = Watcher(cache)
    watcher.start()
    barrier.touch()
    results = []
    for binding, p in procs:
        try:
            out, err = p.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
        results.append((binding, subprocess.CompletedProcess(p.args, p.returncode, out, err)))
    watcher.stop.set()
    watcher.join()
    events = watcher.events
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


def main_run(clis: dict[str, list[str]], other_user: str | None = None) -> int:
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
        if len(cells) != expected_cells:
            print(f"cache-interop: {len(cells)} cells, expected {expected_cells}", file=sys.stderr)
            return 1
        return 1 if bad or failures else 0
    finally:
        server.stdin and server.stdin.close()
        server.terminate()


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
    print("cache-interop --selftest: OK")


def main() -> int:
    argv = sys.argv[1:]
    if argv == ["--selftest"]:
        selftest()
        return 0
    clis: dict[str, list[str]] = {}
    other_user: str | None = None
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
        else:
            print(f"cache-interop: unknown argument {argv[i]!r}", file=sys.stderr)
            return 2
    if not clis:
        print("usage: cache-interop.py --cli <binding>=<command> ... | --selftest", file=sys.stderr)
        return 2
    return main_run(clis, other_user)


if __name__ == "__main__":
    sys.exit(main())
