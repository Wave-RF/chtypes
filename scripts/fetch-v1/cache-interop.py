#!/usr/bin/env python3
r"""scripts/fetch-v1/cache-interop.py — a cache one binding writes, every binding reads
(docs/guides/fetch-v1.md §1, "The verified.json record").

    scripts/fetch-v1/cache-interop.py --cli go=<cmd> --cli python=<cmd> --cli ts=<cmd> --cli rust=<cmd>
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

Prints the 4x4 table and the 0.x rows, writes the same to
$GITHUB_STEP_SUMMARY when set, and exits nonzero if any cell fails.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "fetch-v1"
BINDINGS = ("go", "python", "ts", "rust")
PLATFORM = "linux-arm64"
CASE = "line-ok"
SPELLING = "26.8"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): sha256_file(p) for p in sorted(root.rglob("*")) if p.is_file()}


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

    def run(self, binding: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self.clis[binding], *args], env=self.env, capture_output=True, text=True, timeout=300, check=False
        )

    def fetch(self, binding: str, spelling: str, cache: Path, *, offline: bool) -> subprocess.CompletedProcess[str]:
        args = ["fetch", spelling, "--platform", PLATFORM, "--cache", str(cache)]
        if offline:
            args.append("--offline")
        return self.run(binding, *args)


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


def main_run(clis: dict[str, list[str]]) -> int:
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
        work = Path(tempfile.mkdtemp(prefix="cache-interop-"))
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

        row_labels = [f"{w} writes" for w in names]
        table = render_table("writer \\ reader", row_labels, names, cells)
        up_rows = sorted({r for (r, _c) in cells if not r.endswith(" writes")})
        up_table = render_table("0.x upgrade \\ reader", up_rows, names, cells)
        out = f"{table}\n\n{up_table}\n"
        print(out)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write("### v1-cache-interop\n\n```\n" + out + "```\n")
        bad = [(r, c, v) for (r, c), v in cells.items() if not v.startswith("ok")]
        for r, c, v in bad:
            print(f"FAILED {r} / {c}: {v}", file=sys.stderr)
        expected_cells = len(names) * len(names) + 3 * len(names)
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
    print("cache-interop --selftest: OK")


def main() -> int:
    argv = sys.argv[1:]
    if argv == ["--selftest"]:
        selftest()
        return 0
    clis: dict[str, list[str]] = {}
    i = 0
    while i < len(argv):
        if argv[i] == "--cli" and i + 1 < len(argv) and "=" in argv[i + 1]:
            name, _, cmd = argv[i + 1].partition("=")
            if name not in BINDINGS:
                print(f"cache-interop: unknown binding {name!r}", file=sys.stderr)
                return 2
            clis[name] = shlex.split(cmd)
            i += 2
        else:
            print(f"cache-interop: unknown argument {argv[i]!r}", file=sys.stderr)
            return 2
    if not clis:
        print("usage: cache-interop.py --cli <binding>=<command> ... | --selftest", file=sys.stderr)
        return 2
    return main_run(clis)


if __name__ == "__main__":
    sys.exit(main())
