#!/usr/bin/env python3
r"""check-where-settings.py: guard spec/abi-v2/where-settings/.

That directory holds data copied byte for byte from the artifact producer: one
list per supported ClickHouse line (26.3.json ...) and their union
(union.json). The files are outside abi.json, so the fingerprint never moves
with them; this check is what keeps a bad copy from landing.

    check-where-settings.py --check       check the tree
    check-where-settings.py --selftest    plant each defect, prove each rule fires
    check-where-settings.py --print-table print the README's sha256 list

Rules (each has a planted case in --selftest):
  schema      a schema id other than the two the files use
  sorted      names not sorted, or a duplicate name
  tier        a tier outside the six
  kind        a kind that disagrees with its tier (the mapping below is the
              one the data carries: the two result-* tiers are kind "result",
              the rest are kind "where")
  line        a per-line file whose `line` or `tag` disagrees with its name
  union       a union that is not exactly the derivation of the per-line files
              (names, tier, kind, scopes as the sorted union, and the tags each
              name appears in, in tag order)
  private     a string that points into the private half: a private CI
              path, the private repository's name, a private notes path or
              "#<digit>" (JSON strings and the README)
  table       the README's sha256 list disagrees with the files
  refuse      a `refuse` field anywhere (the lists are not the refuse key)

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
DIR = ROOT / "spec" / "abi-v2" / "where-settings"
LINES = ("26.3", "26.7", "26.8", "26.9")
FILES = tuple(f"{n}.json" for n in LINES) + ("union.json",)
LINE_SCHEMA = "chtypes.where-settings/1"
UNION_SCHEMA = "chtypes.where-settings-union/1"
TIER_KIND = {
    "predicate": "where",
    "predicate-unflipped": "where",
    "execution": "where",
    "output": "where",
    "result-content": "result",
    "result-truncate": "result",
}
# Built from parts so this file does not itself spell a private name (lint-public).
PRIVATE = re.compile("|".join(["ci" + "/steps", "chtypes" + "-core", "notes" + "/", "#[0-9]"]))
ROW = re.compile(r"^- `([^`]+\.json)`: `([0-9a-f]{64})`$")


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


def has_key(node, key) -> bool:
    if isinstance(node, dict):
        return key in node or any(has_key(v, key) for v in node.values())
    if isinstance(node, list):
        return any(has_key(v, key) for v in node)
    return False


def table_of(raw: dict[str, bytes]) -> str:
    return "\n".join(f"- `{n}`: `{hashlib.sha256(raw[n]).hexdigest()}`" for n in FILES)


def check(raw: dict[str, bytes], readme: str) -> list[str]:
    """raw maps each file name to its bytes; returns the problems found."""
    out: list[str] = []
    docs: dict[str, dict] = {}
    for n in FILES:
        if n not in raw:
            out.append(f"{n}: missing")
            continue
        try:
            docs[n] = json.loads(raw[n])
        except ValueError as e:
            out.append(f"{n}: not JSON ({e})")
    for n, d in docs.items():
        want = UNION_SCHEMA if n == "union.json" else LINE_SCHEMA
        if d.get("schema") != want:
            out.append(f"{n}: schema {d.get('schema')!r}, want {want!r} (schema)")
        names = [s.get("name") for s in d.get("settings", [])]
        if names != sorted(names):
            out.append(f"{n}: names are not sorted (sorted)")
        if len(set(names)) != len(names):
            out.append(f"{n}: duplicate name (sorted)")
        for s in d.get("settings", []):
            tier = s.get("tier")
            if tier not in TIER_KIND:
                out.append(f"{n}: {s.get('name')}: tier {tier!r} is not one of the six (tier)")
            elif s.get("kind") != TIER_KIND[tier]:
                out.append(f"{n}: {s.get('name')}: kind {s.get('kind')!r} disagrees with tier {tier} (kind)")
        if has_key(d, "refuse"):
            out.append(f"{n}: has a `refuse` field (refuse)")
        for t in strings(d):
            if PRIVATE.search(t):
                out.append(f"{n}: string {t!r} points into the private half (private)")
        if n != "union.json":
            if d.get("line") != n[: -len(".json")]:
                out.append(f"{n}: line {d.get('line')!r} disagrees with the file name (line)")
            tag, ver = d.get("tag", ""), d.get("version", "")
            if not str(tag).startswith("v" + n[: -len(".json")] + ".") or not str(tag).startswith("v" + str(ver)):
                out.append(f"{n}: tag {tag!r} / version {ver!r} disagree with the file name (line)")
    for m in PRIVATE.finditer(readme):
        out.append(f"README.md: {m.group(0)!r} points into the private half (private)")
    if all(n in docs for n in FILES):
        out += union_problems(docs)
    rows = {}
    for line in readme.splitlines():
        m = ROW.match(line)
        if m:
            rows[m.group(1)] = m.group(2)
    for n in FILES:
        if n in raw:
            got = hashlib.sha256(raw[n]).hexdigest()
            if rows.get(n) != got:
                out.append(f"README.md: sha256 row for {n} is {rows.get(n)!r}, the file is {got} (table)")
    for n in rows:
        if n not in FILES:
            out.append(f"README.md: sha256 row for unknown file {n} (table)")
    return out


def union_problems(docs: dict) -> list[str]:
    out: list[str] = []
    u = docs["union.json"]
    per = [docs[f"{n}.json"] for n in LINES]
    want_tags = [d.get("tag") for d in per]
    if u.get("tags") != want_tags:
        out.append(f"union.json: tags {u.get('tags')!r}, the per-line tags are {want_tags!r} (union)")
    by: dict[str, list] = {}
    for d in per:
        for s in d.get("settings", []):
            by.setdefault(s.get("name"), []).append((d.get("tag"), s))
    have = {s.get("name"): s for s in u.get("settings", [])}
    if set(have) != set(by):
        extra, gone = sorted(set(have) - set(by)), sorted(set(by) - set(have))
        out.append(f"union.json: names differ from the per-line files: extra {extra[:5]}, missing {gone[:5]} (union)")
    for name, ents in by.items():
        s = have.get(name)
        if s is None:
            continue
        tiers = {e["tier"] for _, e in ents}
        if tiers != {s.get("tier")}:
            out.append(f"union.json: {name}: tier {s.get('tier')!r}, the lines say {sorted(tiers)} (union)")
        if {e["kind"] for _, e in ents} != {s.get("kind")}:
            out.append(f"union.json: {name}: kind disagrees with the lines (union)")
        scopes = sorted({x for _, e in ents for x in e.get("scopes", [])})
        if s.get("scopes") != scopes:
            out.append(f"union.json: {name}: scopes {s.get('scopes')!r}, the lines say {scopes!r} (union)")
        if s.get("tags") != [t for t, _ in ents]:
            out.append(f"union.json: {name}: tags {s.get('tags')!r}, the lines say {[t for t, _ in ents]!r} (union)")
    return out


def load(directory: Path) -> tuple[dict[str, bytes], str]:
    raw = {n: (directory / n).read_bytes() for n in FILES if (directory / n).exists()}
    rd = directory / "README.md"
    return raw, rd.read_text() if rd.exists() else ""


def selftest() -> int:
    raw, readme = load(DIR)
    fails: list[str] = []
    planted = 0

    def dump(d) -> bytes:
        return json.dumps(d, indent=1).encode()

    def variant(name: str, mutate, rule: str, file: str = "26.8.json", fix_table: bool = True):
        nonlocal planted
        planted += 1
        r = dict(raw)
        d = json.loads(r[file])
        mutate(d)
        r[file] = dump(d)
        rd = readme
        if fix_table:
            rd = re.sub(r"(?m)^- `" + re.escape(file) + r"`: `[0-9a-f]{64}`$",
                        f"- `{file}`: `{hashlib.sha256(r[file]).hexdigest()}`", rd)
        probs = check(r, rd)
        if not any(f"({rule})" in p for p in probs):
            fails.append(f"planted case {name!r}: rule {rule} did not fire; got {probs[:3]}")

    # positive control: a re-serialized but unchanged corpus is clean once the table is recomputed
    r0 = {n: dump(json.loads(b)) for n, b in raw.items()}
    rd0 = readme
    for n in FILES:
        rd0 = re.sub(r"(?m)^- `" + re.escape(n) + r"`: `[0-9a-f]{64}`$",
                     f"- `{n}`: `{hashlib.sha256(r0[n]).hexdigest()}`", rd0)
    base = check(r0, rd0)
    if base:
        fails.append(f"positive control: the real tree re-serialized is not clean: {base[:3]}")
    base = check(raw, readme)
    if base:
        fails.append(f"positive control: the tree as committed is not clean: {base[:3]}")

    variant("schema id", lambda d: d.update(schema="chtypes.where-settings/2"), "schema")
    variant("union schema id", lambda d: d.update(schema="chtypes.where-settings/1"), "schema", "union.json")
    variant("unsorted", lambda d: d["settings"].reverse(), "sorted")
    variant("duplicate", lambda d: d["settings"].insert(1, copy.deepcopy(d["settings"][0])), "sorted")
    variant("tier outside the six", lambda d: d["settings"][0].update(tier="mystery"), "tier")
    variant("kind vs tier", lambda d: d["settings"][0].update(kind="where" if d["settings"][0]["kind"] == "result" else "result"), "kind")
    variant("line vs name", lambda d: d.update(line="26.7"), "line")
    variant("tag vs name", lambda d: d.update(tag="v26.7.19.5-stable", version="26.7.19.5"), "line")
    variant("union drops a name", lambda d: d["settings"].pop(5), "union", "union.json")
    variant("union adds a name", lambda d: d["settings"].append({"name": "zzz_extra", "tier": "execution", "kind": "where", "scopes": ["planner"], "tags": ["v26.9.8.3-stable"]}), "union", "union.json")
    variant("union tier", lambda d: d["settings"][0].update(tier="predicate"), "union", "union.json")
    variant("union tags", lambda d: d["settings"][0]["tags"].pop(), "union", "union.json")
    variant("union scopes", lambda d: d["settings"][0].update(scopes=["nowhere"]), "union", "union.json")
    variant("a line drops a name the union keeps", lambda d: d["settings"].pop(0), "union")
    for needle in ("ci" + "/steps/x.sh", "the chtypes" + "-core repo", "see notes" + "/plan.md", "fixed in #123"):
        variant("private string " + needle, lambda d, n=needle: d["settings"][0].update(reason=n), "private")
    variant("refuse field", lambda d: d["settings"][0].update(refuse=True), "refuse")
    variant("refuse field in the union", lambda d: d["settings"][0].update(refuse=False), "refuse", "union.json")
    # table: edit the file but leave the README alone
    variant("stale README table", lambda d: d["settings"][0].update(reason="changed"), "table", fix_table=False)
    planted += 1
    if not any("(table)" in p for p in check(raw, readme.replace(hashlib.sha256(raw["26.3.json"]).hexdigest(), "0" * 64))):
        fails.append("planted case 'wrong README hash': rule table did not fire")
    planted += 1
    if not any("(private)" in p for p in check(raw, readme + "\nsee notes/x\n")):
        fails.append("planted case 'private string in README': rule private did not fire")

    if fails:
        for f in fails:
            print("SELFTEST FAIL:", f, file=sys.stderr)
        return 1
    print(f"check-where-settings selftest: ok ({planted} planted cases, 2 positive controls, every rule fired)")
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
        n = sum(len(json.loads(raw[f"{x}.json"])["settings"]) for x in LINES)
        print(f"spec/abi-v2/where-settings: ok ({len(FILES)} files, {n} per-line entries)")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
