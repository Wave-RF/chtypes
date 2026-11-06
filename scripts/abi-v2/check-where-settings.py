#!/usr/bin/env python3
r"""check-where-settings.py: guard spec/abi-v2/where-settings/.

That directory holds data from the artifact producer: one list per supported
ClickHouse line, copied byte for byte (26.3.json ...), and their union
(union.json), derived from those lists by derive_union below. lines.json names
the supported lines; nothing else here does, so a line can neither be added nor
dropped without this check seeing it. The files are outside abi.json, so the
fingerprint never moves with them; this check is what keeps a bad copy from
landing. scripts/abi-v2/regen-where-settings.py writes the directory.

    check-where-settings.py --check       check the tree
    check-where-settings.py --selftest    plant each defect, prove each rule fires
    check-where-settings.py --print-table print the README's sha256 list

The directory also holds documented-pathological.tsv: the settings values under
which a server refuses a filter's query outright while the library answers its
rows. It is copied byte for byte too, and checked by the rules marked (tsv).

Rules (each has a planted case in --selftest, which asserts the rule's name and
a non-zero exit status):
  lines       lines.json missing, not JSON, with another schema, or with a
              `lines` member that is empty, not a list, not `major.minor`
              spellings, repeated, or not in numeric order
  lines-file  a line lines.json names that has no list
  stray       any other file here: a list of a line lines.json does not name
              fails, it is not ignored
  schema      a schema id other than the two the files use
  sorted      names not sorted, or a duplicate name
  tier        a tier outside the six
  kind        a kind that disagrees with its tier (the mapping below is the
              one the data carries: the two result-* tiers are kind "result",
              the rest are kind "where")
  line        a per-line file whose `line` or `tag` disagrees with its name
  union       a union that is not byte for byte the derivation of the per-line
              files (names, tier, kind, scopes as the sorted union, and the tags
              each name appears in, in lines.json's order), or that cannot be
              derived because the lines disagree on a name's tier or kind
  private     a string that points into the private half: a private CI
              path, the private repository's name, a private notes path or
              "#<digit>" (JSON strings and the README)
  table       the README's sha256 list disagrees with the files
  refuse      a `refuse` field anywhere (the lists are not the refuse key)
  tsv-header  documented-pathological.tsv without its column header, or with a
              different one
  tsv-column  a data row that does not have exactly seven columns, or an empty
              column, or a file with no data row
  tsv-line    a row whose line is not in lines.json
  tsv-layer   a row whose layer is not profile, schema or filter
  tsv-code    a row whose server answer is not `e:` and an integer
  tsv-dup     a repeated cell (setting, value, line, layer)
  private     (also) any string of the TSV that points into the private half

Python standard library only.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "spec" / "abi-v2" / "where-settings"
LINES_FILE = "lines.json"
LINES_SCHEMA = "chtypes.where-settings-lines/1"
LINE_SPELLING = re.compile(r"[0-9]+\.[0-9]+")
UNION = "union.json"
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
ROW = re.compile(r"^- `([^`]+\.(?:json|tsv))`: `([0-9a-f]{64})`$")
TSV = "documented-pathological.tsv"
TSV_HEADER = ("setting", "value", "line", "layer", "server", "rule", "ruling")
TSV_LAYERS = ("profile", "schema", "filter")
SERVER = re.compile(r"^e:[0-9]+$")


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


def line_key(line: str) -> tuple[int, ...]:
    return tuple(int(p) for p in line.split("."))


def lines_of(raw: dict[str, bytes]) -> tuple[list[str], list[str]]:
    """The supported lines lines.json names, and the problems found reading it.
    The list is empty whenever a problem makes it unusable."""
    if LINES_FILE not in raw:
        return [], [f"{LINES_FILE}: missing (lines)"]
    try:
        doc = json.loads(raw[LINES_FILE])
    except ValueError as e:
        return [], [f"{LINES_FILE}: not JSON ({e}) (lines)"]
    if not isinstance(doc, dict):
        return [], [f"{LINES_FILE}: not a JSON object (lines)"]
    out: list[str] = []
    if doc.get("schema") != LINES_SCHEMA:
        out.append(f"{LINES_FILE}: schema {doc.get('schema')!r}, want {LINES_SCHEMA!r} (lines)")
    lines = doc.get("lines")
    if not isinstance(lines, list) or not lines:
        return [], out + [f"{LINES_FILE}: `lines` is empty or not a list (lines)"]
    if not all(isinstance(x, str) and LINE_SPELLING.fullmatch(x) for x in lines):
        return [], out + [f"{LINES_FILE}: `lines` holds something other than major.minor (lines)"]
    if len(set(lines)) != len(lines):
        return [], out + [f"{LINES_FILE}: `lines` repeats a line (lines)"]
    if lines != sorted(lines, key=line_key):
        return [], out + [f"{LINES_FILE}: `lines` is not in numeric order (lines)"]
    return lines, out


def data_names(raw: dict[str, bytes], lines: list[str]) -> list[str]:
    """Every data file, in the README's order: the lists, the union, the TSV,
    lines.json."""
    return [f"{n}.json" for n in lines] + [UNION, TSV, LINES_FILE]


def table_of(raw: dict[str, bytes]) -> str:
    lines, _ = lines_of(raw)
    return "\n".join(
        f"- `{n}`: `{hashlib.sha256(raw[n]).hexdigest()}`" for n in data_names(raw, lines) if n in raw
    )


def derive_union(per_line: list[dict]) -> bytes:
    """union.json's exact bytes from the per-line lists, in lines.json's order.
    Run over the producer's own lists it reproduces the producer's union.json
    byte for byte (regen-where-settings.py --control). A name the lines give two
    tiers or two kinds raises ValueError: the data has none, and this derivation
    does not invent an order to settle one."""
    by: dict[str, list] = {}
    for d in per_line:
        for s in d["settings"]:
            by.setdefault(s["name"], []).append((d["tag"], s))
    generators = {d["generator"] for d in per_line}
    if len(generators) != 1:
        raise ValueError(f"the lists name {len(generators)} generators, want 1")
    settings = []
    for name in sorted(by):
        ents = by[name]
        tiers, kinds = {e["tier"] for _, e in ents}, {e["kind"] for _, e in ents}
        if len(tiers) != 1 or len(kinds) != 1:
            raise ValueError(f"{name}: the lines say tiers {sorted(tiers)}, kinds {sorted(kinds)}")
        settings.append(
            {
                "name": name,
                "tier": tiers.pop(),
                "kind": kinds.pop(),
                "scopes": sorted({x for _, e in ents for x in e["scopes"]}),
                "tags": [t for t, _ in ents],
            }
        )
    doc = {
        "schema": UNION_SCHEMA,
        "tags": [d["tag"] for d in per_line],
        "generator": generators.pop(),
        "settings": settings,
    }
    return (json.dumps(doc, indent=1) + "\n").encode()


def check(raw: dict[str, bytes], readme: str) -> list[str]:
    """raw maps each file name to its bytes; returns the problems found."""
    lines, out = lines_of(raw)
    files = [f"{n}.json" for n in lines] + [UNION]
    data_files = data_names(raw, lines)
    for n in sorted(raw):
        if n not in data_files:
            if lines or not n.endswith(".json"):
                out.append(f"{n}: a file lines.json does not account for (stray)")
    docs: dict[str, dict] = {}
    for n in files:
        if n not in raw:
            rule = "union" if n == UNION else "lines-file"
            out.append(f"{n}: missing, lines.json names it or the union needs it ({rule})")
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
    if TSV not in raw:
        out.append(f"{TSV}: missing")
    else:
        out += tsv_problems(raw[TSV], lines)
    for m in PRIVATE.finditer(readme):
        out.append(f"README.md: {m.group(0)!r} points into the private half (private)")
    if lines and all(n in docs for n in files):
        out += union_problems(docs, lines, raw[UNION])
    rows = {}
    for line in readme.splitlines():
        m = ROW.match(line)
        if m:
            rows[m.group(1)] = m.group(2)
    for n in data_files:
        if n in raw:
            got = hashlib.sha256(raw[n]).hexdigest()
            if rows.get(n) != got:
                out.append(f"README.md: sha256 row for {n} is {rows.get(n)!r}, the file is {got} (table)")
    for n in rows:
        if n not in data_files:
            out.append(f"README.md: sha256 row for unknown file {n} (table)")
    return out


def tsv_problems(data: bytes, supported: list[str]) -> list[str]:
    """Rules for documented-pathological.tsv; supported are lines.json's lines."""
    out: list[str] = []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        return [f"{TSV}: not UTF-8 ({e}) (tsv-header)"]
    for t in text.splitlines():
        if PRIVATE.search(t):
            out.append(f"{TSV}: line {t!r} points into the private half (private)")
    lines = text.splitlines()
    comments = [x for x in lines if x.startswith("#")]
    rows = [x for x in lines if x.strip() and not x.startswith("#")]
    want = "# " + "\t".join(TSV_HEADER)
    if not comments or comments[-1] != want:
        out.append(f"{TSV}: the last comment line is not the column header {want!r} (tsv-header)")
    if not rows:
        out.append(f"{TSV}: no data row (tsv-column)")
    top = set(supported)
    seen: set = set()
    for r in rows:
        cols = r.split("\t")
        if len(cols) != len(TSV_HEADER) or any(not c.strip() for c in cols):
            out.append(f"{TSV}: row {r!r} does not have {len(TSV_HEADER)} non-empty columns (tsv-column)")
            continue
        setting, value, line, layer, server = cols[:5]
        if line not in top:
            out.append(f"{TSV}: {setting}: line {line!r} is not one of {sorted(top)} (tsv-line)")
        if layer not in TSV_LAYERS:
            out.append(f"{TSV}: {setting}: layer {layer!r} is not one of {TSV_LAYERS} (tsv-layer)")
        if not SERVER.match(server):
            out.append(f"{TSV}: {setting}: server {server!r} is not e:<integer> (tsv-code)")
        cell = (setting, value, line, layer)
        if cell in seen:
            out.append(f"{TSV}: {setting}: repeated cell {cell!r} (tsv-dup)")
        seen.add(cell)
    return out


def union_problems(docs: dict, lines: list[str], union_bytes: bytes) -> list[str]:
    out: list[str] = []
    u = docs[UNION]
    per = [docs[f"{n}.json"] for n in lines]
    try:
        if union_bytes != derive_union(per):
            out.append("union.json: not byte for byte the derivation of the per-line files (union)")
    except (KeyError, TypeError, AttributeError, ValueError) as e:
        out.append(f"union.json: cannot be derived from the per-line files: {e!r} (union)")
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
    """Every file of the directory but the README, whatever its name: reading only
    the names lines.json gives would make a stray list invisible. A dot file
    (a local .DS_Store) is not data. A subdirectory reads as an empty file, so it
    is stray too."""
    raw = {}
    for p in sorted(directory.iterdir()):
        if p.name != "README.md" and not p.name.startswith("."):
            raw[p.name] = p.read_bytes() if p.is_file() else b""
    rd = directory / "README.md"
    return raw, rd.read_text() if rd.exists() else ""


def verdict(raw: dict[str, bytes], readme: str) -> tuple[int, list[str]]:
    """The exit status --check answers, and the problems behind it."""
    probs = check(raw, readme)
    return (1 if probs else 0), probs


def selftest() -> int:
    raw, readme = load(DIR)
    fails: list[str] = []
    planted = 0
    lines, problems = lines_of(raw)
    if problems or len(lines) < 2:
        print("SELFTEST FAIL: lines.json is unusable, nothing to plant on:", problems, file=sys.stderr)
        return 1
    line_file = f"{lines[0]}.json"
    other_file = f"{lines[1]}.json"
    tag = json.loads(raw[line_file])["tag"]

    def dump(d) -> bytes:
        return (json.dumps(d, indent=1) + "\n").encode()

    def judged(name: str, r: dict[str, bytes], rd: str, rule: str):
        """A planted case counts only if the rule fired AND the status is non-zero."""
        rc, probs = verdict(r, rd)
        if rc == 0:
            fails.append(f"planted case {name!r}: exit status 0")
        if not any(f"({rule})" in p for p in probs):
            fails.append(f"planted case {name!r}: rule {rule} did not fire; got {probs[:3]}")

    def variant(name: str, mutate, rule: str, file: str = line_file, fix_table: bool = True):
        nonlocal planted
        planted += 1
        r = dict(raw)
        d = json.loads(r[file])
        mutate(d)
        r[file] = dump(d)
        if r[file] == raw[file]:
            fails.append(f"planted case {name!r}: the plant changed nothing")
        rd = readme
        if fix_table:
            rd = re.sub(r"(?m)^- `" + re.escape(file) + r"`: `[0-9a-f]{64}`$",
                        f"- `{file}`: `{hashlib.sha256(r[file]).hexdigest()}`", rd)
        judged(name, r, rd, rule)

    # positive control: a re-serialized but unchanged corpus is clean once the table is recomputed
    r0 = {n: (b if n == TSV else dump(json.loads(b))) for n, b in raw.items()}
    rd0 = readme
    for n in data_names(raw, lines):
        rd0 = re.sub(r"(?m)^- `" + re.escape(n) + r"`: `[0-9a-f]{64}`$",
                     f"- `{n}`: `{hashlib.sha256(r0[n]).hexdigest()}`", rd0)
    base = check(r0, rd0)
    if base:
        fails.append(f"positive control: the real tree re-serialized is not clean: {base[:3]}")
    base = check(raw, readme)
    if base:
        fails.append(f"positive control: the tree as committed is not clean: {base[:3]}")

    variant("schema id", lambda d: d.update(schema="chtypes.where-settings/2"), "schema")
    variant("union schema id", lambda d: d.update(schema="chtypes.where-settings/1"), "schema", UNION)
    variant("unsorted", lambda d: d["settings"].reverse(), "sorted")
    variant("duplicate", lambda d: d["settings"].insert(1, copy.deepcopy(d["settings"][0])), "sorted")
    variant("tier outside the six", lambda d: d["settings"][0].update(tier="mystery"), "tier")
    variant("kind vs tier", lambda d: d["settings"][0].update(kind="where" if d["settings"][0]["kind"] == "result" else "result"), "kind")
    variant("line vs name", lambda d: d.update(line="25.1"), "line")
    variant("tag vs name", lambda d: d.update(tag="v25.1.1.1-stable", version="25.1.1.1"), "line")
    variant("union drops a name", lambda d: d["settings"].pop(5), "union", UNION)
    variant("union adds a name", lambda d: d["settings"].append({"name": "zzz_extra", "tier": "execution", "kind": "where", "scopes": ["planner"], "tags": [tag]}), "union", UNION)
    variant("union tier", lambda d: d["settings"][0].update(tier="predicate"), "union", UNION)
    variant("union tags", lambda d: d["settings"][0]["tags"].pop(), "union", UNION)
    variant("union scopes", lambda d: d["settings"][0].update(scopes=["nowhere"]), "union", UNION)
    variant("union keeps a tag no list has", lambda d: d["tags"].insert(1, "v25.1.1.1-stable"), "union", UNION)
    variant("a line drops a name the union keeps", lambda d: d["settings"].pop(0), "union")

    def serialized_otherwise(name: str, data: bytes):
        nonlocal planted
        planted += 1
        if data == raw[UNION]:
            fails.append(f"planted case {name!r}: the plant changed nothing")
        rd = readme.replace(hashlib.sha256(raw[UNION]).hexdigest(), hashlib.sha256(data).hexdigest())
        judged(name, {**raw, UNION: data}, rd, "union")

    union_doc = json.loads(raw[UNION])
    serialized_otherwise("union value-equal but not the producer's bytes", (json.dumps(union_doc, indent=2) + "\n").encode())
    serialized_otherwise("union without its last newline", raw[UNION].rstrip(b"\n"))

    # lines.json itself, each with its README row recomputed so only (lines) can be at stake
    def lines_variant(name: str, data: bytes | None, rule: str = "lines"):
        nonlocal planted
        planted += 1
        r = dict(raw)
        if data is None:
            del r[LINES_FILE]
        else:
            r[LINES_FILE] = data
            if data == raw[LINES_FILE]:
                fails.append(f"planted case {name!r}: the plant changed nothing")
        judged(name, r, readme, rule)

    doc = json.loads(raw[LINES_FILE])
    lines_variant("lines.json missing", None)
    lines_variant("lines.json not JSON", b"nope\n")
    lines_variant("lines.json not an object", b"[]\n")
    lines_variant("lines.json another schema", dump({**doc, "schema": "chtypes.where-settings-lines/2"}))
    lines_variant("lines.json empty", dump({**doc, "lines": []}))
    lines_variant("lines.json without lines", dump({k: v for k, v in doc.items() if k != "lines"}))
    lines_variant("lines.json lines not a list", dump({**doc, "lines": lines[0]}))
    lines_variant("lines.json a spelling that is no line", dump({**doc, "lines": ["26"] + lines}))
    lines_variant("lines.json repeats a line", dump({**doc, "lines": [lines[0]] + lines}))
    lines_variant("lines.json not in numeric order", dump({**doc, "lines": list(reversed(lines))}))
    # A line dropped from lines.json with its list left behind: the list is stray,
    # the TSV's rows on it fall outside, and nothing reads as fine.
    lines_variant("a line dropped, its list left behind", dump({**doc, "lines": lines[1:]}), "stray")

    # The same rules through load(), on a real directory: a rule load() cannot
    # feed is dead however well check() passes above.
    with tempfile.TemporaryDirectory(prefix="where-settings-selftest-") as tmp:

        def on_disk(name: str, change, rule: str):
            nonlocal planted
            planted += 1
            d = Path(tmp) / str(planted)
            d.mkdir()
            for n, b in raw.items():
                (d / n).write_bytes(b)
            (d / "README.md").write_text(readme)
            base_rc, base_probs = verdict(*load(d))
            if base_rc != 0:
                fails.append(f"planted case {name!r}: its unplanted copy is not clean: {base_probs[:3]}")
            before = sorted(p.name for p in d.iterdir())
            change(d)
            r, rd = load(d)
            if sorted(p.name for p in d.iterdir()) == before and r == raw:
                fails.append(f"planted case {name!r}: the plant changed nothing")
            judged(name, r, rd, rule)

        on_disk("a line with no list", lambda d: (d / other_file).unlink(), "lines-file")
        on_disk("a list with no line", lambda d: (d / "25.1.json").write_bytes(raw[other_file]), "stray")
        on_disk("a stray file of another kind", lambda d: (d / "extra.txt").write_bytes(b"x\n"), "stray")
        on_disk("a stray directory", lambda d: (d / "extra").mkdir(), "stray")
        on_disk("lines.json empty on disk", lambda d: (d / LINES_FILE).write_bytes(dump({**doc, "lines": []})), "lines")
        on_disk("lines.json deleted on disk", lambda d: (d / LINES_FILE).unlink(), "lines")
        on_disk("the union deleted on disk", lambda d: (d / UNION).unlink(), "union")

    # A name the lines disagree on is not derived (the derivation invents no order).
    planted += 1
    a, b = json.loads(raw[line_file]), json.loads(raw[other_file])
    shared = next(s["name"] for s in a["settings"] if any(x["name"] == s["name"] for x in b["settings"]))
    for s in b["settings"]:
        if s["name"] == shared:
            s["tier"] = "execution" if s["tier"] != "execution" else "output"
    try:
        derive_union([a, b])
        fails.append("planted case 'lines disagree on a tier': derive_union answered")
    except ValueError:
        pass
    for needle in ("ci" + "/steps/x.sh", "the chtypes" + "-core repo", "see notes" + "/plan.md", "fixed in #123"):
        variant("private string " + needle, lambda d, n=needle: d["settings"][0].update(reason=n), "private")
    variant("refuse field", lambda d: d["settings"][0].update(refuse=True), "refuse")
    variant("refuse field in the union", lambda d: d["settings"][0].update(refuse=False), "refuse", UNION)
    # table: edit the file but leave the README alone
    variant("stale README table", lambda d: d["settings"][0].update(reason="changed"), "table", fix_table=False)
    planted += 1
    judged("wrong README hash", raw, readme.replace(hashlib.sha256(raw[line_file]).hexdigest(), "0" * 64), "table")
    planted += 1
    judged("private string in README", raw, readme + "\nsee notes/x\n", "private")

    # the TSV: each rule, on a table recomputed for the planted bytes so only the rule under test can fire
    def tsv_variant(name: str, mutate, rule: str):
        nonlocal planted
        planted += 1
        r = dict(raw)
        r[TSV] = mutate(raw[TSV].decode()).encode()
        if r[TSV] == raw[TSV]:
            fails.append(f"planted case {name!r}: the plant changed nothing")
        rd = re.sub(r"(?m)^- `" + re.escape(TSV) + r"`: `[0-9a-f]{64}`$",
                    f"- `{TSV}`: `{hashlib.sha256(r[TSV]).hexdigest()}`", readme)
        judged(name, r, rd, rule)

    def first_row(t: str) -> str:
        return next(x for x in t.splitlines() if x.strip() and not x.startswith("#"))

    def swap(t: str, col: int, new: str) -> str:
        r = first_row(t).split("\t")
        r[col] = new
        return t.replace(first_row(t), "\t".join(r), 1)

    tsv_variant("header renamed", lambda t: t.replace("# setting\tvalue", "# name\tvalue"), "tsv-header")
    tsv_variant("header removed", lambda t: "\n".join(x for x in t.splitlines() if not x.startswith("# setting\t")) + "\n", "tsv-header")
    tsv_variant("a column dropped", lambda t: t.replace(first_row(t), first_row(t).rsplit("\t", 1)[0], 1), "tsv-column")
    tsv_variant("a column added", lambda t: t.replace(first_row(t), first_row(t) + "\textra", 1), "tsv-column")
    tsv_variant("an empty column", lambda t: swap(t, 5, ""), "tsv-column")
    tsv_variant("no data row", lambda t: "\n".join(x for x in t.splitlines() if x.startswith("#")) + "\n", "tsv-column")
    tsv_variant("a line outside the lists", lambda t: swap(t, 2, "25.1"), "tsv-line")
    tsv_variant("a layer outside the three", lambda t: swap(t, 3, "mystery"), "tsv-layer")
    tsv_variant("a code that is not e:<n>", lambda t: swap(t, 4, "e:thirty-six"), "tsv-code")
    tsv_variant("a code without e:", lambda t: swap(t, 4, "36"), "tsv-code")
    tsv_variant("a repeated cell", lambda t: t.rstrip("\n") + "\n" + first_row(t) + "\n", "tsv-dup")
    for needle in ("ci" + "/steps/x.sh", "the chtypes" + "-core repo", "see notes" + "/plan.md", "fixed in #123"):
        tsv_variant("private string " + needle, lambda t, n=needle: swap(t, 6, n), "private")
    planted += 1
    judged("stale README table for the TSV", {**raw, TSV: raw[TSV] + b"# x\n"}, readme, "table")
    planted += 1
    judged("README lacks the TSV row", raw, re.sub(r"(?m)^- `" + re.escape(TSV) + r"`.*$", "", readme), "table")
    planted += 1
    rc, probs = verdict({k: v for k, v in raw.items() if k != TSV}, readme)
    if rc == 0 or not any(f"{TSV}: missing" in p for p in probs):
        fails.append("planted case 'TSV missing': not reported")

    if fails:
        for f in fails:
            print("SELFTEST FAIL:", f, file=sys.stderr)
        return 1
    print(f"check-where-settings selftest: ok ({planted} planted cases, 2 positive controls, every rule fired with a non-zero status)")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--selftest"]:
        return selftest()
    raw, readme = load(DIR)
    if argv == ["--print-table"]:
        print(table_of(raw))
        return 0
    if argv == ["--check"]:
        rc, probs = verdict(raw, readme)
        for p in probs:
            print("FAIL:", p, file=sys.stderr)
        if rc:
            return rc
        lines, _ = lines_of(raw)
        n = sum(len(json.loads(raw[f"{x}.json"])["settings"]) for x in lines)
        t = sum(1 for x in raw[TSV].decode().splitlines() if x.strip() and not x.startswith("#"))
        print(f"spec/abi-v2/where-settings: ok ({len(lines)} lines {lines}, {n} per-line entries, {t} pathological rows)")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
