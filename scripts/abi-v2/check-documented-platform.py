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

Rules (each has a planted case in --selftest):
  header    the last comment line is not the column header
  column    a data row without exactly seven non-empty columns, or no data row
  platform  a platform other than linux-amd64, linux-arm64 or darwin-arm64
  line      a line that is not one the where-settings files use
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
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "spec" / "abi-v2" / "documented-platform"
NAME = "documented-platform.tsv"
HEADER = ("platform", "line", "class", "kind", "server", "rule", "ruling")
PLATFORMS = ("linux-amd64", "linux-arm64", "darwin-arm64")
# The lines the where-settings files use (check-where-settings.py LINES).
LINES = ("26.3", "26.7", "26.8", "26.9")
SERVER = re.compile(r"^server e:[0-9]+$")
# Built from parts so this file does not itself spell a private name (lint-public).
PRIVATE = re.compile("|".join(["ci" + "/steps", "chtypes" + "-core", "chtypes" + "-infra", "notes" + "/", "#[0-9]"]))
ROW = re.compile(r"^- `documented-platform\.tsv`: `([0-9a-f]{64})`$")


def table_of(raw: bytes) -> str:
    return f"- `{NAME}`: `{hashlib.sha256(raw).hexdigest()}`"


def check(raw: bytes, readme: str) -> list[str]:
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
        if line not in LINES:
            out.append(f"{NAME}: line {line!r} is not one of {LINES} (line)")
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
    return (f.read_bytes() if f.exists() else b""), (rd.read_text() if rd.exists() else "")


def selftest() -> int:
    raw, readme = load(DIR)
    fails: list[str] = []
    planted = 0
    text = raw.decode()

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
        rd = re.sub(r"(?m)^- `documented-platform\.tsv`: `[0-9a-f]{64}`$", table_of(r), readme)
        probs = check(r, rd)
        if not any(f"({rule})" in p for p in probs):
            fails.append(f"planted case {name!r}: rule {rule} did not fire; got {probs[:3]}")

    base = check(raw, readme)
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
    planted += 1
    if not any("(table)" in p for p in check(raw + b"# x\n", readme)):
        fails.append("planted case 'stale README table': rule table did not fire")
    planted += 1
    if not any("(table)" in p for p in check(raw, readme.replace(hashlib.sha256(raw).hexdigest(), "0" * 64))):
        fails.append("planted case 'wrong README hash': rule table did not fire")
    planted += 1
    if not any("(private)" in p for p in check(raw, readme + "\nsee notes/x\n")):
        fails.append("planted case 'private string in README': rule private did not fire")

    if fails:
        for f in fails:
            print("SELFTEST FAIL:", f, file=sys.stderr)
        return 1
    print(f"check-documented-platform selftest: ok ({planted} planted cases, 1 positive control, every rule fired)")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--selftest"]:
        return selftest()
    raw, readme = load(DIR)
    if argv == ["--print-table"]:
        print(table_of(raw))
        return 0
    if argv == ["--check"]:
        probs = check(raw, readme)
        for p in probs:
            print("FAIL:", p, file=sys.stderr)
        if probs:
            return 1
        n = sum(1 for x in raw.decode().splitlines() if x.strip() and not x.startswith("#"))
        print(f"spec/abi-v2/documented-platform: ok ({n} rows)")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
