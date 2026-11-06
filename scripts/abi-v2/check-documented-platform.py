#!/usr/bin/env python3
r"""check-documented-platform.py: guard spec/abi-v2/documented-platform/.

That directory holds one data file copied byte for byte from the artifact
producer: the exact cells where a platform's server build refuses what the
library answers (documented-platform.tsv). The file is outside abi.json, so
the fingerprint never moves with it; this check is what keeps a bad copy from
landing.

    check-documented-platform.py --check        check the tree
    check-documented-platform.py --selftest     plant each defect, prove each rule fires
    check-documented-platform.py --print-table  print the README's sha256 line

Every mode but --selftest ends with one `documented-platform: verdict:` line on
stderr, naming its exit status, and --selftest drives each through this file's real
entry point in a subprocess, on a planted tree.

Rules (each has a planted case in --selftest, which asserts the rule's name and a
non-zero exit status):
  header    the last comment line is not the column header
  column    a data row without exactly seven non-empty columns, or no data row
  platform  a platform other than linux-amd64, linux-arm64 or darwin-arm64
  line      a line lines.json does not name (spec/abi-v2/where-settings/)
  server    a server answer that is not `server e:<integer>`
  dup       a repeated cell (platform, line, class, kind, server)
  private   a string that points into the private half: a private CI path,
            the private repository's name, a private notes path or
            "#<digit>" (the TSV and the README)
  table     the README's sha256 line disagrees with the file

Python standard library only.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "spec" / "abi-v2" / "documented-platform"
NAME = "documented-platform.tsv"
HEADER = ("platform", "line", "class", "kind", "server", "rule", "ruling")
PLATFORMS = ("linux-amd64", "linux-arm64", "darwin-arm64")
LINES_JSON = ROOT / "spec" / "abi-v2" / "where-settings" / "lines.json"
SERVER = re.compile(r"^server e:[0-9]+$")
# Built from parts so this file does not itself spell a private name (lint-public).
PRIVATE = re.compile("|".join(["ci" + "/steps", "chtypes" + "-core", "chtypes" + "-infra", "notes" + "/", "#[0-9]"]))
ROW = re.compile(r"^- `documented-platform\.tsv`: `([0-9a-f]{64})`$")


def read_lines() -> tuple[str, ...]:
    """The lines the where-settings files use: spec/abi-v2/where-settings/lines.json,
    the one place they are named. Read when a mode needs them, never at import, so
    a lines.json that is missing or unreadable ends in a verdict line like any other
    failure. Raises OSError or ValueError."""

    def pairs(items: list) -> dict:
        out: dict = {}
        for key, value in items:
            if key in out:
                raise ValueError(f"the member {key!r} appears twice")
            out[key] = value
        return out

    doc = json.loads(LINES_JSON.read_bytes(), object_pairs_hook=pairs)
    lines = doc.get("lines") if isinstance(doc, dict) else None
    if not isinstance(lines, list) or not lines or not all(isinstance(x, str) for x in lines):
        raise ValueError("`lines` is not a list of lines")
    return tuple(lines)


def table_of(raw: bytes) -> str:
    return f"- `{NAME}`: `{hashlib.sha256(raw).hexdigest()}`"


def check(raw: bytes, readme: str, lines_named: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        return [f"{NAME}: not UTF-8 ({e}) (header)"]
    lines = text.splitlines()
    for t in lines:
        if PRIVATE.search(t):
            out.append(f"{NAME}: line {t!r} points into the private half (private)")
    comments = [x for x in lines if x.startswith("#")]
    rows = [x for x in lines if x.strip() and not x.startswith("#")]
    want = "# " + "\t".join(HEADER)
    if not comments or comments[-1] != want:
        out.append(f"{NAME}: the last comment line is not the column header {want!r} (header)")
    if not rows:
        out.append(f"{NAME}: no data row (column)")
    seen: set = set()
    for r in rows:
        cols = r.split("\t")
        if len(cols) != len(HEADER) or any(not c.strip() for c in cols):
            out.append(f"{NAME}: row {r!r} does not have {len(HEADER)} non-empty columns (column)")
            continue
        platform, line, klass, kind, server = cols[:5]
        if platform not in PLATFORMS:
            out.append(f"{NAME}: platform {platform!r} is not one of {PLATFORMS} (platform)")
        if line not in lines_named:
            out.append(f"{NAME}: line {line!r} is not one of {lines_named} (line)")
        if not SERVER.match(server):
            out.append(f"{NAME}: server {server!r} is not `server e:<integer>` (server)")
        cell = (platform, line, klass, kind, server)
        if cell in seen:
            out.append(f"{NAME}: repeated cell {cell!r} (dup)")
        seen.add(cell)
    for m in PRIVATE.finditer(readme):
        out.append(f"README.md: {m.group(0)!r} points into the private half (private)")
    got = hashlib.sha256(raw).hexdigest()
    have = [m.group(1) for m in (ROW.match(x) for x in readme.splitlines()) if m]
    if have != [got]:
        out.append(f"README.md: sha256 rows {have!r}, the file is {got} (table)")
    return out


def load(directory: Path) -> tuple[bytes, str]:
    f, rd = directory / NAME, directory / "README.md"
    return (f.read_bytes() if f.exists() else b""), (rd.read_text(encoding="utf-8") if rd.exists() else "")


def verdict(raw: bytes, readme: str, lines_named: tuple[str, ...]) -> tuple[int, list[str]]:
    """The exit status --check answers, and the problems behind it."""
    probs = check(raw, readme, lines_named)
    return (1 if probs else 0), probs


def verdict_line(status: int, word: str) -> str:
    return f"documented-platform: verdict: {word} (exit {status})"


def selftest() -> int:
    raw, readme = load(DIR)
    fails: list[str] = []
    planted = 0
    text = raw.decode()
    try:
        named = read_lines()
    except (OSError, ValueError) as e:
        print(f"SELFTEST FAIL: lines.json is unusable, nothing to plant on: {e}", file=sys.stderr)
        sys.exit(1)

    def first_row(t: str) -> str:
        return next(x for x in t.splitlines() if x.strip() and not x.startswith("#"))

    def swap(t: str, col: int, new: str) -> str:
        r = first_row(t).split("\t")
        r[col] = new
        return t.replace(first_row(t), "\t".join(r), 1)

    def variant(name: str, mutate, rule: str):
        nonlocal planted
        planted += 1
        r = mutate(text).encode()
        if r == raw:
            fails.append(f"planted case {name!r}: the plant changed nothing")
        rd = re.sub(r"(?m)^- `documented-platform\.tsv`: `[0-9a-f]{64}`$", table_of(r), readme)
        rc, probs = verdict(r, rd, named)
        if rc == 0:
            fails.append(f"planted case {name!r}: exit status 0")
        if not any(f"({rule})" in p for p in probs):
            fails.append(f"planted case {name!r}: rule {rule} did not fire; got {probs[:3]}")

    base = check(raw, readme, named)
    if base:
        fails.append(f"positive control: the tree as committed is not clean: {base[:3]}")

    variant("header renamed", lambda t: t.replace("# platform\tline", "# plat\tline"), "header")
    variant("header removed", lambda t: "\n".join(x for x in t.splitlines() if not x.startswith("# platform\t")) + "\n", "header")
    variant("a column dropped", lambda t: t.replace(first_row(t), first_row(t).rsplit("\t", 1)[0], 1), "column")
    variant("a column added", lambda t: t.replace(first_row(t), first_row(t) + "\textra", 1), "column")
    variant("an empty column", lambda t: swap(t, 5, ""), "column")
    variant("no data row", lambda t: "\n".join(x for x in t.splitlines() if x.startswith("#")) + "\n", "column")
    variant("a platform outside the three", lambda t: swap(t, 0, "windows-amd64"), "platform")
    variant("a line outside the lists", lambda t: swap(t, 1, "25.1"), "line")
    variant("a server answer without the prefix", lambda t: swap(t, 4, "e:306"), "server")
    variant("a server answer that is not an integer", lambda t: swap(t, 4, "server e:deep"), "server")
    variant("a repeated cell", lambda t: t.rstrip("\n") + "\n" + first_row(t) + "\n", "dup")
    for needle in ("ci" + "/steps/x.sh", "the chtypes" + "-core repo", "see notes" + "/plan.md", "fixed in #123"):
        variant("private string " + needle, lambda t, n=needle: swap(t, 6, n), "private")
    def judged(name: str, r: bytes, rd: str, rule: str):
        nonlocal planted
        planted += 1
        if (r, rd) == (raw, readme):
            fails.append(f"planted case {name!r}: the plant changed nothing")
        rc, probs = verdict(r, rd, named)
        if rc == 0:
            fails.append(f"planted case {name!r}: exit status 0")
        if not any(f"({rule})" in p for p in probs):
            fails.append(f"planted case {name!r}: rule {rule} did not fire; got {probs[:3]}")

    judged("stale README table", raw + b"# x\n", readme, "table")
    judged("wrong README hash", raw, readme.replace(hashlib.sha256(raw).hexdigest(), "0" * 64), "table")
    judged("private string in README", raw, readme + "\nsee notes/x\n", "private")

    cli = cli_selftest(raw, readme, fails)

    # A selftest that saw a failure ends the process itself: it is the one mode whose
    # status no selftest can read, and CI's whole trust in it is that status.
    if fails:
        for f in fails:
            print("SELFTEST FAIL:", f, file=sys.stderr)
        sys.exit(1)
    print(f"check-documented-platform selftest: ok ({planted} planted cases, 1 positive control, every rule fired with a non-zero status; {cli} command-line runs, each process's status equal to its verdict line)")
    return 0


def cli_selftest(raw: bytes, readme: str, fails: list[str]) -> int:
    """This file's real entry point in a subprocess, on a planted copy of the tree,
    run from that copy so ROOT and LINES are derived the way a real run derives
    them. What is asserted is the process's own status: nothing above reads it."""
    verdict_re = re.compile(r"^documented-platform: verdict: .*\(exit ([0-9]+)\)$", re.M)
    runs = 0
    row = next(x for x in raw.decode().splitlines() if x.strip() and not x.startswith("#"))
    cols = row.split("\t")

    with tempfile.TemporaryDirectory(prefix="documented-platform-cli-") as tmp:

        def tree(name: str, lines: list[str], data: bytes = raw, fresh_table: bool = True) -> Path:
            """A copy with its own lines.json and, by default, its table recomputed."""
            root = Path(tmp) / name
            where = root / DIR.relative_to(ROOT)
            where.mkdir(parents=True)
            (where / NAME).write_bytes(data)
            rd = re.sub(r"(?m)^- `documented-platform\.tsv`: `[0-9a-f]{64}`$", table_of(data), readme) if fresh_table else readme
            (where / "README.md").write_text(rd, encoding="utf-8")
            settings = root / "spec" / "abi-v2" / "where-settings"
            settings.mkdir(parents=True)
            (settings / "lines.json").write_text(json.dumps({"lines": lines}), encoding="utf-8")
            script = root / "scripts" / "abi-v2" / "check-documented-platform.py"
            script.parent.mkdir(parents=True)
            shutil.copyfile(__file__, script)
            return root

        def cli(what: str, root: Path, args: list[str], want: int, expect: str = "") -> subprocess.CompletedProcess:
            nonlocal runs
            runs += 1
            script = root / "scripts" / "abi-v2" / "check-documented-platform.py"
            r = subprocess.run(
                [sys.executable, "-I", str(script), *args], capture_output=True, text=True, cwd=tmp, timeout=120
            )
            said = verdict_re.findall(r.stderr)
            if r.returncode != want or said != [str(want)]:
                fails.append(f"command line {what!r}: status {r.returncode}, verdict lines {said}, want {want}")
            if expect not in r.stdout + r.stderr:
                fails.append(f"command line {what!r}: {expect!r} was not said")
            return r

        on = [cols[1]]
        clean = tree("clean", on)
        cli("--check on a clean tree", clean, ["--check"], 0, "spec/abi-v2/documented-platform: ok")
        r = cli("--print-table on a clean tree", clean, ["--print-table"], 0)
        if r.stdout.strip() != table_of(raw):
            fails.append("command line --print-table: stdout is not the table")
        cli("no mode", clean, [], 2)
        cli("--check with a stale table", tree("stale", on, raw + b"# x\n", fresh_table=False), ["--check"], 1, "(table)")

        # The lines this script accepts are the tree's lines.json, however that moves:
        # a retired line is refused, and a line added there is accepted with no edit here.
        retired = tree("retired", ["26.8"] if cols[1] != "26.8" else ["26.3"])
        cli("--check, a row on a line lines.json no longer names", retired, ["--check"], 1, "(line)")
        added = tree("added", ["88.8"], raw.replace(f"\t{cols[1]}\t".encode(), b"\t88.8\t"))
        if (added / DIR.relative_to(ROOT) / NAME).read_bytes() == raw:
            fails.append("command line: the moved row changed nothing")
        cli("--check, a row on a line lines.json just added", added, ["--check"], 0)

        # lines.json is read inside the run: a missing or unreadable one is a verdict line
        # (and --print-table needs none).
        bad = tree("nolines", on)
        (bad / "spec" / "abi-v2" / "where-settings" / "lines.json").unlink()
        cli("--check, no lines.json", bad, ["--check"], 1, "lines.json could not be read")
        cli("--print-table, no lines.json", bad, ["--print-table"], 0)
        for what, body in (("not JSON", "nope"), ("a lines that is a string", '{"lines": "26.8"}'), ("a repeated lines", '{"lines": [], "lines": ["' + cols[1] + '"]}')):
            bad = tree("badlines-" + what.split()[-1], on)
            (bad / "spec" / "abi-v2" / "where-settings" / "lines.json").write_text(body, encoding="utf-8")
            cli(f"--check, lines.json {what}", bad, ["--check"], 1, "lines.json could not be read")

        empty = tree("empty", on, b"")
        (empty / DIR.relative_to(ROOT) / NAME).unlink()
        cli("--check with no file", empty, ["--check"], 1, "(column)")
        r = cli("--print-table with no file", empty, ["--print-table"], 1)
        if r.stdout:
            fails.append("command line --print-table with no file: printed a table")
    return runs


def main(argv: list[str]) -> int:
    if argv == ["--selftest"]:
        return selftest()
    raw, readme = load(DIR)
    if argv == ["--print-table"]:
        if not (DIR / NAME).is_file():
            print(f"FAIL: {NAME}: missing", file=sys.stderr)
            print(verdict_line(1, "no table"), file=sys.stderr)
            return 1
        print(table_of(raw))
        print(verdict_line(0, "ok"), file=sys.stderr)
        return 0
    if argv == ["--check"]:
        try:
            named = read_lines()
        except (OSError, ValueError) as e:
            print(f"FAIL: {LINES_JSON.name} could not be read ({type(e).__name__}) (line)", file=sys.stderr)
            print(verdict_line(1, "lines.json unreadable"), file=sys.stderr)
            return 1
        rc, probs = verdict(raw, readme, named)
        for p in probs:
            print("FAIL:", p, file=sys.stderr)
        if rc == 0:
            n = sum(1 for x in raw.decode().splitlines() if x.strip() and not x.startswith("#"))
            print(f"spec/abi-v2/documented-platform: ok ({n} rows)")
        print(verdict_line(rc, f"problems found: {len(probs)}" if rc else "ok"), file=sys.stderr)
        return rc
    print(__doc__, file=sys.stderr)
    print(verdict_line(2, "usage"), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
