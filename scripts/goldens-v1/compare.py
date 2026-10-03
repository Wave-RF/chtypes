#!/usr/bin/env python3
"""compare.py: the one comparator for the v1 goldens, shared by all four bindings.

    scripts/goldens-v1/compare.py --selftest [--schema-validate]
    scripts/goldens-v1/compare.py --goldens G --report R [--report R2 ...]
                                  [--kinds wire,...] [--require-bindings go,python,ts,rust]
                                  [--schema-validate]

A binding's runner executes the goldens document's cases and RECORDS what the
library returned in a report (spec/goldens/v1/report.schema.json). It never
compares. This script reads the goldens document and one report per binding and
renders every verdict, so the comparison logic lives in exactly one place.

THE RULES ARE THE ARTIFACT PRODUCER'S. The section "the producer's rules" below
(decode, pointers, drop, check_one, strict_equal, judge) is the producer's
reference evaluator carried over with only its module framing and selftest
removed; the schema's $defs.expect and $defs.check (spec/goldens/v1/schema.json)
are their statement. The byte rule: comparison happens on DECODED trees, never
on JSON text. Every JSON string becomes its UTF-8 bytes, every F_b64 member
becomes member F with the decoded bytes (value_b64 stays value_b64, decoded),
run_varying members are dropped from both sides, then deep equality: array
order significant, type-strict. A malformed document is a failure, never a pass.

REPORT-LEVEL RULES (this repository's, layered on the producer's):

  * a case of the goldens document that the report does not name is a FAIL,
    never a skip; an id the document does not name, or a duplicate id, is a FAIL;
  * `skipped` is allowed ONLY when the report's platform is not among the case's
    `platforms`; a skip anywhere else is a FAIL, and so is RUNNING a case the
    document excludes on that platform (its expectation is unproven there);
  * a case's `at` and `status` must equal the expectation's; for a non-OK case
    the error is compared as a decoded tree (ch_code, and the name, message and
    column bytes) after dropping run_varying members, which on a non-OK status
    address the error as /error/<member> (schema.json, expect.run_varying);
  * for an OK case the document bytes are parsed as JSON and judged by the
    producer's rules, `decoded_ok` must be true (the binding's public decoder
    accepted the document), and the export bytes must equal export_b64, or be
    absent when the expectation carries none;
  * the report must name the goldens document's own clickhouse_version, build,
    revision and sha256 (of the file given), carry its abi_fingerprint, and run
    on one of its generated.checked_on platforms; another build or revision is
    a whole-report failure, never a per-case one;
  * the whole run FAILS if any case fails, if any report ran zero cases (after
    --kinds), if a --require-bindings binding has no report, or if two reports
    name the same binding.

Exit status: 0 only when every report passes. Python standard library only; the
optional --schema-validate additionally validates the goldens document and each
report against their schemas and needs `jsonschema` (it refuses to run without).
"""
import argparse
import base64
import copy
import hashlib
import json
import os
import sys

BINDINGS = ("go", "python", "ts", "rust")
KINDS = ("schema", "row", "batch", "filter", "wire")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SPEC_DIR = os.path.join(ROOT, "spec", "goldens", "v1")
FIXTURE_DIR = os.path.join(ROOT, "tests", "fixtures", "goldens-v1")


# --- the producer's rules ------------------------------------------------------------


class Malformed(Exception):
    pass


def b64(s):
    return base64.b64decode(s, validate=True)


def decode(v):
    """The byte-rule normal form of a JSON value."""
    if isinstance(v, str):
        return v.encode("utf-8")
    if isinstance(v, list):
        return [decode(x) for x in v]
    if not isinstance(v, dict):
        return v
    out = {}
    for k, x in v.items():
        if k.endswith("_b64") and k != "value_b64":
            base = k[:-4]
            if base in v:
                raise Malformed("both %s and %s are present" % (base, k))
            if not isinstance(x, str):
                raise Malformed("%s is not a base64 string" % k)
            out[base] = b64(x)
        elif k == "value_b64":
            if not isinstance(x, str):
                raise Malformed("value_b64 is not a base64 string")
            out[k] = b64(x)
        else:
            out[k] = decode(x)
    return out


def segments(path):
    if path == "":
        return []
    if not path.startswith("/"):
        raise Malformed("pointer %r does not start with '/'" % path)
    return [s.replace("~1", "/").replace("~0", "~") for s in path[1:].split("/")]


def step(nodes, seg):
    out = []
    for n in nodes:
        if seg == "*":
            if not isinstance(n, list):
                raise Malformed("'*' over a non-array")
            out.extend(n)
        elif seg.startswith("@"):
            if not isinstance(n, list):
                raise Malformed("selector %r over a non-array" % seg)
            want = b64(seg[1:])
            hits = [e for e in n if isinstance(e, dict) and e.get("name") == want]
            if len(hits) != 1:
                raise Malformed("selector %r matched %d elements" % (seg, len(hits)))
            out.append(hits[0])
        elif isinstance(n, list):
            if not seg.isdigit() or int(seg) >= len(n):
                raise Malformed("index %r out of range (%d)" % (seg, len(n)))
            out.append(n[int(seg)])
        elif isinstance(n, dict):
            if seg not in n:
                raise Malformed("no member %r" % seg)
            out.append(n[seg])
        else:
            raise Malformed("cannot step %r into a scalar" % seg)
    return out


def walk(tree, segs):
    nodes = [tree]
    for s in segs:
        nodes = step(nodes, s)
    return nodes


def drop(tree, path):
    """Remove the member a run_varying pointer names (absent is fine: a NULL has no value_b64)."""
    segs = segments(path)
    if not segs:
        raise Malformed("run_varying cannot name the whole document")
    for parent in walk(tree, segs[:-1]):
        if not isinstance(parent, dict):
            raise Malformed("run_varying %s does not end at an object member" % path)
        parent.pop(segs[-1], None)


def check_one(doc, c):
    segs = segments(c["path"])
    if "set_bytes_b64" in c:
        got = {x for x in walk(doc, segs[:-1]) for x in [x.get(segs[-1]) if isinstance(x, dict) else None] if x is not None}
        want = {b64(x) for x in c["set_bytes_b64"]}
        return None if got == want else "set %r != %r" % (sorted(got), sorted(want))
    if "absent" in c:
        parents = walk(doc, segs[:-1])
        if len(parents) != 1 or not isinstance(parents[0], dict):
            raise Malformed("%s does not name one object member" % c["path"])
        return None if segs[-1] not in parents[0] else "present, expected absent"
    nodes = walk(doc, segs)
    if len(nodes) != 1:
        raise Malformed("%s resolved to %d nodes" % (c["path"], len(nodes)))
    n = nodes[0]
    if "bytes_b64" in c:
        return None if n == b64(c["bytes_b64"]) else "bytes %r != %r" % (n, b64(c["bytes_b64"]))
    if "length" in c:
        if not isinstance(n, list):
            raise Malformed("length of a non-array")
        return None if len(n) == c["length"] else "length %d != %d" % (len(n), c["length"])
    if "equals" in c:
        want = decode(c["equals"])
        return None if n == want and type(n) is type(want) else "%r != %r" % (n, want)
    raise Malformed("check names no operation")


def strict_equal(a, b):
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(strict_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(strict_equal(x, y) for x, y in zip(a, b))
    return a == b


def first_difference(a, b, at=""):
    if type(a) is not type(b):
        return "%s: %s vs %s" % (at or "/", type(a).__name__, type(b).__name__)
    if isinstance(a, dict):
        for k in sorted(a.keys() | b.keys()):
            if k not in a or k not in b:
                return "%s/%s: only in %s" % (at, k, "expected" if k in a else "actual")
            d = first_difference(a[k], b[k], "%s/%s" % (at, k))
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return "%s: %d vs %d elements" % (at or "/", len(a), len(b))
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_difference(x, y, "%s/%d" % (at, i))
            if d:
                return d
        return None
    return None if a == b else "%s: %r vs %r" % (at or "/", a, b)


def judge(expect, actual_doc):
    """[reason] for a case whose call answered CHS_OK with `actual_doc`; [] means it passed."""
    try:
        act = decode(actual_doc)
        if "document" in expect:
            exp = decode(copy.deepcopy(expect["document"]))
            for p in expect["run_varying"]:
                drop(exp, p)
                drop(act, p)
            return [] if strict_equal(exp, act) else ["document differs: " + (first_difference(exp, act) or "?")]
        fails = []
        for c in expect["compare"]:
            try:
                why = check_one(act, c)
            except (Malformed, ValueError) as e:
                why = "malformed: %s" % e
            if why:
                fails.append("%s: %s" % (c["path"], why))
        return fails
    except (Malformed, ValueError) as e:
        return ["malformed: %s" % e]


# --- the report-level rules ------------------------------------------------------------

PASS, FAIL, SKIP = "pass", "fail", "skipped"


def parse_json_bytes(raw):
    """Strict JSON from bytes: UTF-8, no duplicate member names (a duplicate would hide a value)."""

    def pairs(items):
        d = {}
        for k, v in items:
            if k in d:
                raise ValueError("duplicate member %r" % k)
            d[k] = v
        return d

    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)


def judge_error(exp, got):
    """(verdict, [reasons]) for a non-OK case's error. Like a document, the error is compared as a decoded
    tree ({ch_code, ch_name, message, column}, the last three bytes), and run_varying pointers address its
    members as /error/<member> (schema.json $defs.expect.run_varying): a message quoting a per-run name is dropped."""
    if not isinstance(got, dict):
        return FAIL, ["no error recorded for a %s case" % exp["status"]]
    try:
        for f in ("ch_code", "ch_name_b64", "message_b64", "column_b64"):
            if f not in got:
                return FAIL, ["error.%s missing" % f]
        if type(got["ch_code"]) is not int:
            return FAIL, ["error.ch_code %r is not an integer" % (got["ch_code"],)]
        want_t, got_t = {"error": decode(copy.deepcopy(exp["error"]))}, {"error": decode(copy.deepcopy(got))}
        for p in exp["run_varying"]:
            drop(want_t, p)
            drop(got_t, p)
    except (Malformed, ValueError) as e:
        return FAIL, ["malformed: %s" % e]
    if strict_equal(want_t, got_t):
        return PASS, []
    return FAIL, ["error differs: " + (first_difference(want_t, got_t) or "?")]


def judge_case(case, rc, platform):
    """(verdict, [reasons]) for one goldens case against its report entry (None when absent)."""
    if rc is None:
        return FAIL, ["absent from the report (a missing case is a failure, never a skip)"]
    allowed = platform in case["platforms"]
    if rc.get("result") == "skipped":
        if allowed:
            return FAIL, ["skipped on %s, which the case's platforms allow (%s)" % (platform, rc.get("skip_reason", "no reason"))]
        if not rc.get("skip_reason"):
            return FAIL, ["skipped with no skip_reason"]
        return SKIP, ["platform %s is not in the case's platforms" % platform]
    if rc.get("result") != "ran":
        return FAIL, ["result is %r, expected ran or skipped" % (rc.get("result"),)]
    if not allowed:
        return FAIL, ["ran on %s, which the case's platforms exclude; it must be skipped" % platform]
    exp = case["expect"]
    why = []
    if rc.get("at") != exp["at"]:
        why.append("stopped at %r, expected %r" % (rc.get("at"), exp["at"]))
    if rc.get("status") != exp["status"]:
        why.append("status %r, expected %r" % (rc.get("status"), exp["status"]))
    if why:
        return FAIL, why
    try:
        if exp["status"] != "CHS_OK":
            return judge_error(exp, rc.get("error"))
        if rc.get("decoded_ok") is not True:
            why.append("decoded_ok is %r: the binding's public decoder must accept the document" % (rc.get("decoded_ok"),))
        if "document_b64" not in rc:
            return FAIL, why + ["no document_b64 recorded for an OK case"]
        try:
            doc = parse_json_bytes(b64(rc["document_b64"]))
        except (ValueError, UnicodeDecodeError) as e:
            return FAIL, why + ["document_b64 is not a JSON document: %s" % e]
        why += judge(exp, doc)
        want_export = b64(exp["export_b64"]) if "export_b64" in exp else None
        got_export = b64(rc["export_b64"]) if "export_b64" in rc else None
        if want_export is None and got_export:
            why.append("export bytes recorded but the expectation has none")
        elif want_export is not None and got_export != want_export:
            why.append("export bytes %r, expected %r" % (got_export, want_export))
    except (Malformed, ValueError) as e:
        why.append("malformed: %s" % e)
    return (FAIL, why) if why else (PASS, [])


def sha256_hex(raw):
    return hashlib.sha256(raw).hexdigest()


def judge_report(gold, gold_sha, report, kinds):
    """Judge one report. Returns (problems, rows): problems are whole-report failures,
    rows are (case_id, kind, verdict, reasons) for every in-scope case."""
    problems = []
    g = report.get("goldens") or {}
    for k in ("clickhouse_version", "build", "revision"):
        if g.get(k) != gold.get(k) or type(g.get(k)) is not type(gold.get(k)):
            problems.append("report is for %s %r, the goldens document has %r" % (k, g.get(k), gold.get(k)))
    if g.get("sha256") != gold_sha:
        problems.append("report names goldens sha256 %r, the document given hashes to %s" % (g.get("sha256"), gold_sha))
    art = report.get("artifact") or {}
    if art.get("abi_fingerprint") != gold["abi_fingerprint"]:
        problems.append("artifact abi_fingerprint %r, the document's is %s" % (art.get("abi_fingerprint"), gold["abi_fingerprint"]))
    platform = report.get("platform")
    if platform not in gold["generated"]["checked_on"]:
        problems.append("platform %r is not one the document was checked on (%s)" % (platform, ", ".join(gold["generated"]["checked_on"])))
    by_id, dupes = {}, set()
    for rc in report.get("cases") or []:
        if rc.get("id") in by_id:
            dupes.add(rc.get("id"))
        by_id[rc.get("id")] = rc
    for d in sorted(dupes):
        problems.append("case %r appears more than once in the report" % d)
    known = set()
    rows = []
    for setup in gold["setups"]:
        for case in setup["cases"]:
            known.add(case["id"])
            if kinds and case["kind"] not in kinds:
                continue
            rc = by_id.get(case["id"])
            if rc is not None and rc.get("setup") != setup["id"]:
                rows.append((case["id"], case["kind"], FAIL, ["ran under setup %r, the case belongs to %r" % (rc.get("setup"), setup["id"])]))
                continue
            v, why = judge_case(case, rc, platform)
            if case["id"] in dupes:
                v, why = FAIL, why + ["duplicate id in the report"]
            rows.append((case["id"], case["kind"], v, why))
    for i in sorted(str(x) for x in by_id if x not in known):
        problems.append("report names case %r, which the goldens document does not" % i)
    ran = sum(1 for r in rows if r[2] != SKIP)
    if ran == 0:
        problems.append("zero cases ran (%d in scope%s)" % (len(rows), ", kinds " + ",".join(sorted(kinds)) if kinds else ""))
    return problems, rows


def evaluate(gold, gold_sha, reports, kinds, require):
    """The whole run. Returns (ok, lines, table) where table is {binding: {kind: [pass, fail, skip]}}."""
    ok, lines, table = True, [], {}
    seen = {}
    for rep in reports:
        b = rep.get("binding")
        if b in seen:
            ok = False
            lines.append("FAIL  two reports name binding %r" % b)
        seen[b] = rep
    for b in require:
        if b not in seen:
            ok = False
            lines.append("FAIL  no report for required binding %r" % b)
    for b, rep in seen.items():
        problems, rows = judge_report(gold, gold_sha, rep, kinds)
        t = table.setdefault(b, {})
        for cid, kind, v, why in rows:
            t.setdefault(kind, [0, 0, 0])[(PASS, FAIL, SKIP).index(v)] += 1
            if v == FAIL:
                ok = False
                lines.append("FAIL  %s  %s: %s" % (b, cid, "; ".join(why)))
            elif v == SKIP:
                lines.append("skip  %s  %s: %s" % (b, cid, why[0]))
        for p in problems:
            ok = False
            lines.append("FAIL  %s  report: %s" % (b, p))
    return ok, lines, table


def render_table(table, kinds):
    cols = [k for k in KINDS if (not kinds or k in kinds)]
    out = ["| binding | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for b in sorted(table, key=lambda x: (BINDINGS.index(x) if x in BINDINGS else 99, str(x))):
        cells = []
        for k in cols:
            p, f, s = table[b].get(k, [0, 0, 0])
            cells.append("%d pass, %d fail, %d skipped" % (p, f, s) if (p or f or s) else "none")
        out.append("| %s | %s |" % (b, " | ".join(cells)))
    return "\n".join(out)


def schema_validate(instance, schema_name):
    try:
        import jsonschema
    except ImportError:
        sys.exit("--schema-validate needs the `jsonschema` package (uv run --with jsonschema)")
    with open(os.path.join(SPEC_DIR, schema_name), encoding="utf-8") as f:
        schema = json.load(f)
    v = jsonschema.Draft202012Validator(schema)
    return ["%s: %s" % ("/".join(str(p) for p in e.absolute_path) or "(root)", e.message[:200]) for e in v.iter_errors(instance)]


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--selftest", action="store_true", help="prove the rules and every report-level failure mode on the synthetic fixtures")
    ap.add_argument("--goldens")
    ap.add_argument("--report", action="append", default=[])
    ap.add_argument("--kinds", default="", help="comma list of kinds to judge, default all")
    ap.add_argument("--require-bindings", default="", help="comma list; each must have a report")
    ap.add_argument("--schema-validate", action="store_true", help="also validate the goldens document and every report against their schemas (needs jsonschema)")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest(a.schema_validate)
    if not a.goldens or not a.report:
        ap.error("--goldens and at least one --report are required")
    kinds = {k for k in a.kinds.split(",") if k}
    if kinds - set(KINDS):
        ap.error("unknown kinds: %s" % ",".join(sorted(kinds - set(KINDS))))
    require = [b for b in a.require_bindings.split(",") if b]
    if set(require) - set(BINDINGS):
        ap.error("unknown bindings: %s" % ",".join(sorted(set(require) - set(BINDINGS))))
    with open(a.goldens, "rb") as f:
        raw = f.read()
    gold = json.loads(raw)
    reports = []
    for p in a.report:
        with open(p, encoding="utf-8") as f:
            reports.append(json.load(f))
    if a.schema_validate:
        errs = ["goldens: " + e for e in schema_validate(gold, "schema.json")]
        for p, r in zip(a.report, reports):
            errs += ["%s: %s" % (p, e) for e in schema_validate(r, "report.schema.json")]
        if errs:
            print("\n".join("SCHEMA  " + e for e in errs))
            return 1
        print("schema: the goldens document and %d report(s) validate" % len(reports))
    ok, lines, table = evaluate(gold, sha256_hex(raw), reports, kinds, require)
    print("\n".join(lines))
    print(render_table(table, kinds))
    print("goldens %s build %s revision %s: %s" % (gold["clickhouse_version"], gold["build"], gold["revision"], "PASS" if ok else "FAIL"))
    return 0 if ok else 1


# --- self-test -------------------------------------------------------------------------


def selftest(with_schema):
    enc = lambda s: base64.b64encode(s if isinstance(s, bytes) else s.encode()).decode()
    with open(os.path.join(FIXTURE_DIR, "synthetic-goldens.json"), "rb") as f:
        raw = f.read()
    gold = json.loads(raw)
    sha = sha256_hex(raw)
    with open(os.path.join(FIXTURE_DIR, "synthetic-report.json"), encoding="utf-8") as f:
        base_report = json.load(f)
    cases = {c["id"]: c for s in gold["setups"] for c in s["cases"]}
    results = []

    def expect(label, fails, want_pass):
        ok = (not fails) == want_pass
        results.append(ok)
        print("%s  %s%s" % ("ok  " if ok else "FAIL", label, "" if ok else "  -> %r" % fails))

    # 1. the producer's rules, on this fixture's cases (synthetic values, the producer's case shapes)
    for cid, c in cases.items():
        if "document" in c["expect"]:
            expect("%s: its own expected document passes" % cid, judge(c["expect"], c["expect"]["document"]), True)
    row = cases["json-overflow-wrap"]["expect"]
    d = copy.deepcopy(row["document"])
    d["cols"][0]["stored_b64"] = enc(d["cols"][0].pop("stored"))
    expect("the same bytes as F_b64 instead of F pass (bytes, not JSON text)", judge(row, d), True)
    d = copy.deepcopy(row["document"]); d["cols"][0]["stored"] = "300"
    expect("a changed stored rendering fails", judge(row, d), False)
    d = copy.deepcopy(row["document"]); d["transformed"][0]["reason"] = "value_changed"
    expect("a changed transform reason fails", judge(row, d), False)
    d = copy.deepcopy(row["document"]); d["transformed"][0]["lossy"] = 1
    expect("lossy 1 is not lossy true (type-strict)", judge(row, d), False)
    raw_row = cases["row-tsv-nonutf8-string-bytes"]["expect"]
    d = copy.deepcopy(raw_row["document"]); d["cols"][0]["value_b64"] = enc("cafe")
    expect("different value bytes fail", judge(raw_row, d), False)
    d = copy.deepcopy(raw_row["document"]); d["cols"][0]["stored"] = "x"
    expect("F and F_b64 both present is malformed, not a pass", judge(raw_row, d), False)
    d = copy.deepcopy(row["document"]); d["extra"] = True
    expect("an extra member fails a whole-document compare", judge(row, d), False)
    batch = cases["json-two-rows"]["expect"]
    d = copy.deepcopy(batch["document"]); d["rows"].reverse()
    expect("array order is significant", judge(batch, d), False)
    wire = cases["wire-tsv-array-null-element"]["expect"]
    d = copy.deepcopy(wire["document"]); d["transformed"] = []
    expect("a wire case without its transform fails", judge(wire, d), False)
    d = copy.deepcopy(wire["document"]); d["cols"][0]["wire"] = "[NULL]"
    expect("a wire case with a different wire rendering fails", judge(wire, d), False)
    rnd = cases["row-json-default-rand"]["expect"]
    actual = {"outcome": "accepted", "code": 0, "err": "", "cols": [
        {"name": "n", "src": "input", "stored": "1"}, {"name": "r", "src": "default_generated", "stored": "2718281828"}],
        "transformed": [{"column": "r", "stored": "2718281828", "reason": "default_filled", "lossy": False}]}
    expect("a generator case passes on any drawn value", judge(rnd, actual), True)
    actual["cols"][1]["src"] = "default"
    expect("a generator case fails when value_src differs", judge(rnd, actual), False)
    raw_name = b"\xff\xfe?"
    sel = enc(raw_name)
    assert "/" in sel, sel
    doc = {"cols": [{"name_b64": sel, "null": True}]}
    esc = "@" + sel.replace("~", "~0").replace("/", "~1")
    expect("a '/'-bearing base64 selector, escaped, finds a non-UTF-8 name", judge({"compare": [{"path": "/cols/%s/null" % esc, "equals": True}], "run_varying": []}, doc), True)
    expect("the same selector unescaped does not resolve", judge({"compare": [{"path": "/cols/@%s/null" % sel, "equals": True}], "run_varying": []}, doc), False)
    vary = {"document": {"cols": [{"name": "r", "stored": "1"}]}, "run_varying": ["/cols/@%s/stored" % enc("r")]}
    expect("run_varying drops the member on both sides", judge(vary, {"cols": [{"name": "r", "stored": "99"}]}), True)
    expect("run_varying drops only what it names", judge(vary, {"cols": [{"name": "q", "stored": "1"}]}), False)

    # 1b. run_varying on a non-OK status addresses /error/<member>
    ev = cases["schema-default-unknown-column"]["expect"]
    other = {"ch_code": 47, "ch_name_b64": enc("UNKNOWN_IDENTIFIER"), "message_b64": enc("Unknown identifier tmp_zz99 in DEFAULT"), "column_b64": enc("n")}
    expect("an error whose run_varying message differs passes", judge_error(ev, other)[1], True)
    expect("an error whose ch_code differs fails even with message dropped", judge_error(ev, dict(other, ch_code=48))[1], False)
    expect("an error whose column differs fails even with message dropped", judge_error(ev, dict(other, column_b64=enc("m")))[1], False)
    strict = dict(ev, run_varying=[])
    expect("without run_varying the same message difference fails", judge_error(strict, other)[1], False)
    expect("a run_varying pointer that is not under /error is malformed, not a pass", judge_error(dict(ev, run_varying=["/message"]), other)[1], False)

    # 2. the report-level rules, on in-memory mutations of the synthetic report
    def run(report, kinds=(), require=()):
        ok, lines, _ = evaluate(gold, sha, [report], set(kinds), list(require))
        return [] if ok else lines or ["failed"]

    def mut(fn):
        r = copy.deepcopy(base_report)
        fn(r)
        return r

    def case_of(r, cid):
        return next(c for c in r["cases"] if c["id"] == cid)

    def set_doc(r, cid, fn):
        c = case_of(r, cid)
        d = json.loads(base64.b64decode(c["document_b64"]))
        fn(d)
        c["document_b64"] = enc(json.dumps(d))

    expect("the synthetic report passes whole", run(base_report), True)
    expect("a --kinds subset passes and judges only that kind", run(base_report, kinds=["wire"]), True)
    expect("the report's own document bytes differ from the expected JSON text and still pass (no re-serialization)",
           [] if case_of(base_report, "filter-simple")["document_b64"] != enc(json.dumps(cases["filter-simple"]["expect"]["document"])) else ["same text"], True)
    expect("a document whose real change hides in the decoded tree fails", run(mut(lambda r: set_doc(r, "wire-tsv-array-null-element", lambda d: d["cols"][0].update(wire="[NULL]")))), False)
    expect("a missing case is a FAIL, not a skip", run(mut(lambda r: r["cases"].remove(case_of(r, "filter-simple")))), False)
    def all_skipped(r):
        for c in r["cases"]:
            for k in ("at", "status", "error", "document_b64", "export_b64", "decoded_ok"):
                c.pop(k, None)
            c.update(result="skipped", skip_reason="x")
    expect("a zero-run report fails", run(mut(all_skipped)), False)
    expect("an empty case list fails", run(mut(lambda r: r.update(cases=[]))), False)
    def skip_allowed(r):
        c = case_of(r, "filter-simple"); c.update(result="skipped", skip_reason="not wanted")
        [c.pop(k, None) for k in ("at", "status", "document_b64", "decoded_ok")]
    expect("a skip on a platform the case allows fails", run(mut(skip_allowed)), False)
    expect("the skip the platforms exclude passes (and is counted skipped)", [] if case_of(base_report, "row-not-on-linux-amd64")["result"] == "skipped" and run(base_report) == [] else ["x"], True)
    def ran_excluded(r):
        c = case_of(r, "row-not-on-linux-amd64")
        c.pop("skip_reason")
        c.update(result="ran", at="call", status="CHS_OK", decoded_ok=True,
                 document_b64=enc(json.dumps(cases["row-not-on-linux-amd64"]["expect"]["document"])))
    expect("running a case the document excludes on this platform fails", run(mut(ran_excluded)), False)
    expect("a skip with no reason fails", run(mut(lambda r: case_of(r, "row-not-on-linux-amd64").pop("skip_reason"))), False)
    expect("decoded_ok false on an OK case fails", run(mut(lambda r: case_of(r, "json-overflow-wrap").update(decoded_ok=False))), False)
    expect("decoded_ok null on an OK case fails", run(mut(lambda r: case_of(r, "json-overflow-wrap").update(decoded_ok=None))), False)
    expect("a report for another build fails", run(mut(lambda r: r["goldens"].update(build="20990102.000000"))), False)
    expect("a report for another revision fails", run(mut(lambda r: r["goldens"].update(revision=2))), False)
    expect("a report for another version fails", run(mut(lambda r: r["goldens"].update(clickhouse_version="99.1.2.4"))), False)
    expect("a report hashing another document fails", run(mut(lambda r: r["goldens"].update(sha256="0" * 64))), False)
    expect("a report from another ABI fingerprint fails", run(mut(lambda r: r["artifact"].update(abi_fingerprint="sha256:" + "0" * 64))), False)
    expect("a platform the document was not checked on fails", run(mut(lambda r: r.update(platform="linux/riscv64"))), False)
    expect("a wrong status fails", run(mut(lambda r: case_of(r, "schema-unknown-type").update(status="CHS_DECLINED"))), False)
    expect("a wrong stop step fails", run(mut(lambda r: case_of(r, "schema-unknown-type").update(at="call"))), False)
    expect("a wrong error code fails", run(mut(lambda r: case_of(r, "schema-unknown-type")["error"].update(ch_code=51))), False)
    expect("a wrong error message fails", run(mut(lambda r: case_of(r, "schema-unknown-type")["error"].update(message_b64=enc("other")))), False)
    expect("a missing error on a rejected case fails", run(mut(lambda r: case_of(r, "schema-unknown-type").pop("error"))), False)
    expect("a wrong export fails", run(mut(lambda r: case_of(r, "json-two-rows").update(export_b64=enc("x")))), False)
    expect("a missing export fails", run(mut(lambda r: case_of(r, "json-two-rows").pop("export_b64"))), False)
    expect("an unexpected export fails", run(mut(lambda r: case_of(r, "filter-simple").update(export_b64=enc("x")))), False)
    expect("a document that is not JSON fails", run(mut(lambda r: case_of(r, "filter-simple").update(document_b64=enc("not json")))), False)
    expect("a document with a duplicate member fails", run(mut(lambda r: case_of(r, "filter-simple").update(document_b64=enc('{"outcome":"ok","outcome":"ok"}')))), False)
    expect("an OK case without document_b64 fails", run(mut(lambda r: case_of(r, "filter-simple").pop("document_b64"))), False)
    expect("a case the document does not name fails", run(mut(lambda r: r["cases"].append({"id": "stray", "setup": "utc", "result": "skipped", "skip_reason": "x"}))), False)
    expect("a duplicate case id fails", run(mut(lambda r: r["cases"].append(copy.deepcopy(case_of(r, "filter-simple"))))), False)
    expect("a case under another setup fails", run(mut(lambda r: case_of(r, "filter-simple").update(setup="other"))), False)
    expect("a required binding with no report fails", run(base_report, require=["go", "python"]), False)
    expect("two reports for one binding fail", [] if evaluate(gold, sha, [base_report, base_report], set(), [])[0] else ["failed"], False)
    expect("the required binding with its report passes", run(base_report, require=["go"]), True)
    expect("a --kinds subset whose cases are all absent from the report fails", run(mut(lambda r: r["cases"].remove(case_of(r, "json-two-rows"))), kinds=["batch"]), False)
    # the table
    _, _, table = evaluate(gold, sha, [base_report], set(), [])
    expect("the table counts pass, fail and skipped per binding and kind", [] if table["go"]["row"] == [4, 0, 1] and table["go"]["wire"] == [2, 0, 0] else [repr(table)], True)

    # 3. the schemas, when jsonschema is available and asked for (a skip here is printed, never silent)
    if with_schema:
        gerr = schema_validate(gold, "schema.json")
        expect("the synthetic goldens document validates against schema.json", gerr, True)
        expect("the synthetic report validates against report.schema.json", schema_validate(base_report, "report.schema.json"), True)
        expect("a report with a bad status word is refused by report.schema.json", schema_validate(mut(lambda r: case_of(r, "filter-simple").update(status="OK")), "report.schema.json"), False)
        expect("a skipped entry carrying a document is refused by report.schema.json", schema_validate(mut(lambda r: case_of(r, "row-not-on-linux-amd64").update(document_b64="")), "report.schema.json"), False)
        expect("a rejected case with no error is refused by report.schema.json", schema_validate(mut(lambda r: case_of(r, "schema-unknown-type").pop("error")), "report.schema.json"), False)
        g = copy.deepcopy(gold); g["setups"][0]["cases"][0]["kind"] = "bogus"
        expect("a goldens document with an unknown kind is refused by schema.json", schema_validate(g, "schema.json"), False)
        g = copy.deepcopy(gold); [c for c in g["setups"][0]["cases"] if c["id"] == "row-not-on-linux-amd64"][0].pop("platforms_reason")
        expect("a narrowed case with no platforms_reason is refused by schema.json", schema_validate(g, "schema.json"), False)
    else:
        print("note  schema cases NOT run (no --schema-validate); CI runs them")
    print("%d/%d selftest cases behaved as expected" % (sum(results), len(results)))
    if not results:
        return 1
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
