#!/usr/bin/env python3
r"""check-insert-projection.py: the projection rule of chs_preview_insert, over
the shared fixture tests/fixtures/abi-v2/insert-projection.json.

spec/abi-v2/docs.md (chs_preview_insert) states that an insert_result document
is a projection of the batch document for the same inputs. The fixture holds
cases, each a batch document with its export bytes and an insert_result with
its output bytes; every binding's projection-gate unit tests read the same file
and mirror the rule names below.

    check-insert-projection.py             check the fixture's cases
    check-insert-projection.py --selftest  check them, then prove every rule fires

A case: id, output_format (a chs_format name, or NONE), batch, batch_export_b64
(null when no export ran), insert, insert_rows_b64 (null when no output), expect:
"equal", {"exception": "<name>"} or {"violates": "<rule>"}.

Rules, checked in this order (the first that fails is the answer):
  e_row_rejects    a batch with an `e` verdict (outcome accepted): the insert is
                   rejected with the FIRST e row's code and err, refused_at
                   `where` (the INSERT's WHERE threw on a row; never `row`,
                   which is the body's parse), rows_passed, rows_cut and
                   rows_undecided 0, rows_read the batch's, and no output
  export_decline_is_call_decline
                   a batch that is accepted with `export_declined` R (reason id
                   I): the insert is `unsupported`, reason I, code -2, refused_at
                   `body_step`, err the batch's export_declined text, no
                   export_columns, no output, and rows_passed, rows_cut and
                   rows_undecided 0 under a filter (those members, and err,
                   are then not compared with the batch's)
  shared_members   every member both documents may carry is equal, absent where
                   absent (outcome, code, reason, refused_at, export_columns,
                   rows_passed and rows_cut are skipped under a declined output); an insert never carries
                   export_declined or export_declined_reason
  counts           rows_passed = #t, rows_cut = #f + #d, rows_undecided = #d
                   when the insert is accepted, and all three 0 on any other
                   outcome (the batch's rows_passed and rows_cut are 0 and 0 on
                   a batch that is not accepted); all three absent with no filter
  err_prefix       the batch's err is a prefix of the insert's
  err_suffix       the remainder is empty or exactly ClickHouse's row-reader
                   suffix, `: (at row N)` and a line feed
  output_presence  an output exists exactly when the batch export ran
  non_t_row        a line format's output holds more lines than there are t rows
                   (a row that is not t was output)
  line_output      a line format's output is the concatenation, in body order,
                   of the batch's row_spans of the t rows
  native_output    a Native output is byte-identical to the batch's Native export

Exception (a case that is equal by the docs' own carve-out):
  generator_draw   a row has a column sourced `default_generated`: the two calls
                   may draw different values, so the output bytes are not
                   compared; every other member still is

A fixture whose documents break abi.json's schemas, or whose batch is
inconsistent with itself (a span outside the export, positions that do not number
the t rows), is a FixtureError, never a rule violation.

Python standard library only (plus the repository's own validator).
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "abi-v1"))
import model as abimodel  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "abi-v2" / "insert-projection.json"
SCHEMA = "chtypes.abi-v2.insert-projection-fixture/1"

RULES = ("e_row_rejects", "export_decline_is_call_decline", "shared_members", "counts", "err_prefix", "err_suffix",
         "output_presence", "non_t_row", "line_output", "native_output")
EXCEPTIONS = ("generator_draw",)

# docs.md, chs_preview_insert, "The projection rule", first bullet.
SHARED = ("outcome", "code", "reason", "refused_at", "rows_read", "rows_skipped", "unsupported_settings", "framing",
          "partition_count", "export_columns", "rows_passed", "rows_cut")
# Members a declined output replaces: the insert's own values, by export_decline_is_call_decline.
DECLINE_REPLACES = ("outcome", "code", "reason", "refused_at", "export_columns", "rows_passed", "rows_cut")
COUNTS = ("rows_passed", "rows_cut", "rows_undecided")
INSERT_NEVER = ("export_declined", "export_declined_b64", "export_declined_reason")
SUFFIX = re.compile(rb"^: \(at row [0-9]+\)\n$")
_ABSENT = object()


class FixtureError(Exception):
    pass


def _bytes(doc: dict, name: str):
    """A byte-string member (name or name_b64), as bytes; _ABSENT when neither is present."""
    if name in doc:
        return doc[name].encode("utf-8")
    if name + "_b64" in doc:
        return base64.b64decode(doc[name + "_b64"])
    return _ABSENT


def _member(doc: dict, name: str):
    return doc.get(name, _ABSENT)


def _b64(case: dict, key: str):
    v = case.get(key)
    return None if v is None else base64.b64decode(v)


def _varuint(b: bytes, i: int = 0):
    n = shift = 0
    while True:
        c = b[i]
        n |= (c & 0x7F) << shift
        i += 1
        if c < 0x80:
            return n, i
        shift += 7


def _schemas():
    m = abimodel.load(ROOT, 2)
    return m.documents["batch"].schema, m.documents["insert_result"].schema


def shape(case: dict, batch_schema, insert_schema) -> None:
    cid = case.get("id", "?")

    def bad(msg):
        raise FixtureError(f"{cid}: {msg}")

    for k in ("id", "output_format", "batch", "batch_export_b64", "insert", "insert_rows_b64", "expect"):
        if k not in case:
            bad(f"missing {k}")
    errs = abimodel.validate(case["batch"], batch_schema)
    if errs:
        bad(f"batch is not a valid batch document: {errs[:3]}")
    errs = abimodel.validate(case["insert"], insert_schema)
    if errs:
        bad(f"insert is not a valid insert_result document: {errs[:3]}")
    batch = case["batch"]
    exp = _b64(case, "batch_export_b64")
    ran = "row_spans" in batch or "row_positions" in batch
    if ran != (exp is not None):
        bad("batch_export_b64 is present exactly when the batch carries a row map")
    verdicts = [r.get("verdict") for r in batch["rows"]]
    if verdicts and ("rows_passed" in batch) != all(v is not None for v in verdicts):
        bad("row verdicts are present exactly when the batch carries a filter's counts")
    if "row_spans" in batch:
        spans = batch["row_spans"]
        if len(spans) != len(batch["rows"]):
            bad("row_spans is not index-aligned with rows")
        end = 0
        for i, (s, v) in enumerate(zip(spans, verdicts)):
            if (s is not None) != (v in (None, "t")):
                bad(f"row_spans[{i}] is present exactly for an exported row (no filter, or verdict t)")
            if s is not None:
                if s["off"] != end:
                    bad(f"row_spans[{i}] does not follow the previous span")
                end = s["off"] + s["len"]
        if end != len(exp):
            bad("row_spans do not cover the export")
    if "row_positions" in batch:
        pos = batch["row_positions"]
        want = [None if v not in (None, "t") else 0 for v in verdicts]
        k = 0
        for i, w in enumerate(want):
            if w is not None:
                want[i] = k
                k += 1
        if pos != want:
            bad("row_positions do not number the exported rows 0, 1, 2 in body order")
        if k == 0 and exp != b"":
            bad("a Native export with no row exported is an empty buffer")
        if k:
            ncols, i = _varuint(exp)
            nrows, i = _varuint(exp, i)
            if nrows != k:
                bad("the Native block's row count is not the number of exported rows")
    if case["output_format"] == "NONE" and (ran or "export_columns" in case["insert"]):
        bad("NONE: an export member is present")
    e = case["expect"]
    ok = e == "equal" or (isinstance(e, dict) and len(e) == 1 and (
        e.get("exception") in EXCEPTIONS or e.get("violates") in RULES))
    if not ok:
        bad(f"expect {e!r} is not equal, a known exception or a known rule")


def evaluate(case: dict):
    """Returns ("equal", None), ("exception", name) or ("violates", rule)."""
    batch, ins = case["batch"], case["insert"]
    fmt = case["output_format"]
    rows = batch["rows"]
    verdicts = [r.get("verdict") for r in rows]
    out = _b64(case, "insert_rows_b64")
    exp = _b64(case, "batch_export_b64")
    filtered = "rows_passed" in batch

    # e_row_rejects
    if batch["outcome"] == "accepted" and "e" in verdicts:
        first = rows[verdicts.index("e")]
        want_err = _bytes(first, "verdict_err")
        if not (ins["outcome"] == "rejected" and ins["code"] == first["verdict_code"]
                and _bytes(ins, "err") == want_err and ins.get("refused_at") == "where"
                and ins.get("rows_passed") == 0 and ins.get("rows_cut") == 0 and ins.get("rows_undecided") == 0
                and ins["rows_read"] == batch["rows_read"] and out is None):
            return "violates", "e_row_rejects"
        return "equal", None

    # export_decline_is_call_decline
    declined = batch["outcome"] == "accepted" and any(k in batch for k in ("export_declined", "export_declined_b64"))
    if declined:
        if not (ins["outcome"] == "unsupported" and ins.get("reason") == batch.get("export_declined_reason")
                and ins["code"] == -2 and ins.get("refused_at") == "body_step"
                and "export_columns" not in ins and out is None
                and _bytes(ins, "err") == _bytes(batch, "export_declined")
                and (not filtered or all(ins.get(k) == 0 for k in COUNTS))):
            return "violates", "export_decline_is_call_decline"

    # shared_members
    if any(k in ins for k in INSERT_NEVER):
        return "violates", "shared_members"
    for m in SHARED:
        if declined and m in DECLINE_REPLACES:
            continue
        if _member(batch, m) != _member(ins, m):
            return "violates", "shared_members"

    # counts: #t, #f + #d and #d on an accepted call, 0 on any other outcome
    t, f, d = verdicts.count("t"), verdicts.count("f"), verdicts.count("d")
    if filtered:
        if batch["outcome"] == "accepted":
            good = batch["rows_passed"] == t and batch["rows_cut"] == f + d
        else:
            good = batch["rows_passed"] == 0 and batch["rows_cut"] == 0
        want = (t, f + d, d) if ins["outcome"] == "accepted" else (0, 0, 0)
        if not (good and tuple(ins.get(k) for k in COUNTS) == want):
            return "violates", "counts"
    elif any(k in ins for k in COUNTS):
        return "violates", "counts"

    # err: the batch's, plus a remainder that is empty or ClickHouse's row-reader suffix
    # (a declined output's err is the decline text, checked above)
    if not declined:
        be, ie = _bytes(batch, "err"), _bytes(ins, "err")
        if not ie.startswith(be):
            return "violates", "err_prefix"
        rest = ie[len(be):]
        if rest and not SUFFIX.fullmatch(rest):
            return "violates", "err_suffix"

    # output
    if (exp is None) != (out is None):
        return "violates", "output_presence"
    if out is None:
        return "equal", None
    if any(c.get("src") == "default_generated" for r in rows for c in r.get("cols", [])):
        return "exception", "generator_draw"
    if fmt == "Native":
        return ("equal", None) if out == exp else ("violates", "native_output")
    spans = batch["row_spans"]
    t_lines = [exp[s["off"]:s["off"] + s["len"]] for s, v in zip(spans, verdicts) if v in (None, "t")]
    # the spans of non-t rows are null, so a stray row is seen as a line beyond the t rows
    if len(out.splitlines(keepends=True)) > len(t_lines):
        return "violates", "non_t_row"
    return ("equal", None) if out == b"".join(t_lines) else ("violates", "line_output")


def verdict_of(case: dict):
    return evaluate(case)


def expected(case: dict):
    e = case["expect"]
    if e == "equal":
        return ("equal", None)
    if "exception" in e:
        return ("exception", e["exception"])
    return ("violates", e["violates"])


def check(doc: dict) -> list[str]:
    problems: list[str] = []
    if doc.get("schema") != SCHEMA:
        return [f"schema {doc.get('schema')!r}, want {SCHEMA!r}"]
    bs, isch = _schemas()
    ids = [c.get("id") for c in doc["cases"]]
    if len(set(ids)) != len(ids):
        problems.append("duplicate case id")
    for c in doc["cases"]:
        try:
            shape(c, bs, isch)
            got = evaluate(c)
        except FixtureError as e:
            problems.append(str(e))
            continue
        if got != expected(c):
            problems.append(f"{c['id']}: got {got}, expected {expected(c)}")
    return problems


def selftest() -> int:
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fails = check(doc)
    cases = doc["cases"]
    # coverage: an equal case, the exception, and a planted red for every rule
    for rule in RULES:
        if not any(c["expect"] == {"violates": rule} for c in cases):
            fails.append(f"no planted red for rule {rule}")
    for x in EXCEPTIONS:
        if not any(c["expect"] == {"exception": x} for c in cases):
            fails.append(f"no case for exception {x}")
    for fmt in ("JSONCompactEachRow", "CSV", "Native", "NONE"):
        if not any(c["output_format"] == fmt and c["expect"] == "equal" for c in cases):
            fails.append(f"no equal case for {fmt}")
    # a malformed fixture is a FixtureError, not a verdict
    bs, isch = _schemas()
    base = next(c for c in cases if c["expect"] == "equal")
    for what, mutate in {
        "an invalid batch document": lambda c: c["batch"].pop("rows"),
        "an invalid insert document": lambda c: c["insert"].update(rows_read=-1),
        "a span outside the export": lambda c: c["batch"]["row_spans"][0].update(len=1),
        "an unknown rule": lambda c: c.update(expect={"violates": "nope"}),
    }.items():
        c = copy.deepcopy(base)
        mutate(c)
        try:
            shape(c, bs, isch)
            fails.append(f"shape: {what} was accepted")
        except FixtureError:
            pass
    if fails:
        for f in fails:
            print(f"check-insert-projection selftest FAILED: {f}", file=sys.stderr)
        return 1
    reds = sum(1 for c in cases if isinstance(c["expect"], dict) and "violates" in c["expect"])
    print(f"check-insert-projection selftest: ok ({len(cases)} cases, {reds} planted reds, every rule fired)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="the insert projection rule over the shared fixture")
    ap.add_argument("--selftest", action="store_true", help="prove every rule fires on the fixture's planted cases")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    problems = check(json.loads(FIXTURE.read_text(encoding="utf-8")))
    for p in problems:
        print(p, file=sys.stderr)
    if problems:
        return 1
    print("check-insert-projection: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
