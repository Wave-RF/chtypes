#!/usr/bin/env python3
r"""check-declines.py: guard spec/abi-v2/declines/.

That directory holds one data file copied byte for byte from the artifact
producer: every reason the library declines or refuses a filter, per supported
ClickHouse line (declines.json). The file is outside abi.json, so the
fingerprint never moves with it; this check is what keeps a bad copy from
landing.

    check-declines.py --check        check the tree
    check-declines.py --selftest     plant each defect, prove each rule fires
    check-declines.py --print-table  print the README's sha256 line

Rules (each has a planted case in --selftest):
  schema      a schema id other than chtypes.filter-declines/1
  unique      a duplicate id (the data is grouped by class, not sorted by id,
              so order is not checked; uniqueness is)
  class       a class outside the six the data uses
  field       an entry missing a field, or a code without `chs` and `ch`
  line        a `lines` value outside the file's top-level `lines`, or empty
  private     a string that points into the private half: a private CI
              path, the private repository's name, a private notes path or
              "#<digit>" (JSON strings and the README)
  table       the README's sha256 line disagrees with the file

Python standard library only.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "spec" / "abi-v2" / "declines"
NAME = "declines.json"
SCHEMA = "chtypes.filter-declines/1"
# Derived from the copied file (declines.json at the producer's commit) and pinned.
CLASSES = frozenset({"decline", "server-check", "create-decline", "call-decline", "row-decline", "batch-precedence"})
FIELDS = ("id", "class", "trigger", "code", "lines", "paths", "retirement", "description")
# Built from parts so this file does not itself spell a private name (lint-public).
PRIVATE = re.compile("|".join(["ci" + "/steps", "chtypes" + "-core", "notes" + "/", "#[0-9]"]))
ROW = re.compile(r"^- `declines\.json`: `([0-9a-f]{64})`$")


def strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from strings(v)


def table_of(raw: bytes) -> str:
    return f"- `{NAME}`: `{hashlib.sha256(raw).hexdigest()}`"


def check(raw: bytes, readme: str) -> list[str]:
    """raw is the file's bytes; returns the problems found."""
    out: list[str] = []
    try:
        d = json.loads(raw)
    except ValueError as e:
        return [f"{NAME}: not JSON ({e})"]
    if d.get("schema") != SCHEMA:
        out.append(f"{NAME}: schema {d.get('schema')!r}, want {SCHEMA!r} (schema)")
    top = d.get("lines", [])
    entries = d.get("declines", [])
    ids = [e.get("id") for e in entries]
    if len(set(ids)) != len(ids):
        out.append(f"{NAME}: duplicate id (unique)")
    for e in entries:
        who = e.get("id")
        if e.get("class") not in CLASSES:
            out.append(f"{NAME}: {who}: class {e.get('class')!r} is not one of the six (class)")
        for f in FIELDS:
            if f not in e:
                out.append(f"{NAME}: {who}: missing field {f} (field)")
        code = e.get("code")
        if not isinstance(code, dict) or "chs" not in code or "ch" not in code:
            out.append(f"{NAME}: {who}: code lacks chs or ch (field)")
        ls = e.get("lines")
        if not isinstance(ls, list) or not ls:
            out.append(f"{NAME}: {who}: lines is empty or not a list (line)")
        else:
            for x in ls:
                if x not in top:
                    out.append(f"{NAME}: {who}: line {x!r} is not in the file's lines {top!r} (line)")
    for t in strings(d):
        if PRIVATE.search(t):
            out.append(f"{NAME}: string {t!r} points into the private half (private)")
    for m in PRIVATE.finditer(readme):
        out.append(f"README.md: {m.group(0)!r} points into the private half (private)")
    rows = [m.group(1) for m in (ROW.match(x) for x in readme.splitlines()) if m]
    got = hashlib.sha256(raw).hexdigest()
    if rows != [got]:
        out.append(f"README.md: sha256 rows {rows!r}, the file is {got} (table)")
    return out


def load(directory: Path) -> tuple[bytes, str]:
    f, rd = directory / NAME, directory / "README.md"
    return (f.read_bytes() if f.exists() else b""), (rd.read_text() if rd.exists() else "")


def selftest() -> int:
    raw, readme = load(DIR)
    fails: list[str] = []
    planted = 0

    def dump(d) -> bytes:
        return json.dumps(d, indent=1).encode()

    def variant(name: str, mutate, rule: str, fix_table: bool = True):
        nonlocal planted
        planted += 1
        d = json.loads(raw)
        mutate(d)
        r = dump(d)
        rd = readme
        if fix_table:
            rd = re.sub(r"(?m)^- `declines\.json`: `[0-9a-f]{64}`$", table_of(r), rd)
        probs = check(r, rd)
        if not any(f"({rule})" in p for p in probs):
            fails.append(f"planted case {name!r}: rule {rule} did not fire; got {probs[:3]}")

    # positive controls: the tree as committed, and the same data re-serialized with its table recomputed
    base = check(raw, readme)
    if base:
        fails.append(f"positive control: the tree as committed is not clean: {base[:3]}")
    r0 = dump(json.loads(raw))
    base = check(r0, re.sub(r"(?m)^- `declines\.json`: `[0-9a-f]{64}`$", table_of(r0), readme))
    if base:
        fails.append(f"positive control: the real data re-serialized is not clean: {base[:3]}")
    # the pinned class set is the set the data uses
    if {e["class"] for e in json.loads(raw)["declines"]} != CLASSES:
        fails.append("positive control: the pinned class set differs from the classes the data uses")

    variant("schema id", lambda d: d.update(schema="chtypes.filter-declines/2"), "schema")
    variant("duplicate id", lambda d: d["declines"].insert(1, copy.deepcopy(d["declines"][0])), "unique")
    variant("class outside the six", lambda d: d["declines"][0].update({"class": "mystery"}), "class")
    for f in ("id", "class", "trigger", "code", "lines", "paths", "retirement", "description"):
        variant("missing " + f, lambda d, f=f: d["declines"][3].pop(f), "field")
    variant("code without ch", lambda d: d["declines"][0]["code"].pop("ch"), "field")
    variant("line outside the file's lines", lambda d: d["declines"][0]["lines"].append("25.1"), "line")
    variant("empty lines", lambda d: d["declines"][0].update(lines=[]), "line")
    for needle in ("ci" + "/steps/x.sh", "the chtypes" + "-core repo", "see notes" + "/plan.md", "fixed in #123"):
        variant("private string " + needle, lambda d, n=needle: d["declines"][0].update(description=n), "private")
    variant("stale README table", lambda d: d["declines"][0].update(description="changed"), "table", fix_table=False)
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
    print(f"check-declines selftest: ok ({planted} planted cases, 3 positive controls, every rule fired)")
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
        print(f"spec/abi-v2/declines: ok ({len(json.loads(raw)['declines'])} entries)")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
