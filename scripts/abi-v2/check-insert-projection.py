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
"equal", {"exception": "<name>"} or {"violates": "<rule>"}; and, for a body the
post-WHERE steps refused, batch_of_t_rows: {rows, batch, export_b64}, where rows
are the indexes of the body's rows the insert keeps (its t rows) and batch, with
its export, is chs_preview_batch's document for the same inputs over those
records alone (the filter attached, as for the whole body).

Why that document. When the batch is not accepted every verdict in it is `d`,
so the batch alone cannot say which rows the WHERE cut, and no other call
answers the WHERE for a row a post-WHERE step refuses (chs_filter_eval_body
mirrors the batch's refusal). The batch of the t rows is the library's own
answer for the insert the docs describe, the insert of the passing rows: it
must be accepted with every row t, it must not hold the row the whole body was
refused at, and the insert must equal it. A pair whose refusal came from a row
the WHERE keeps has that row among its t rows, so its batch of the t rows is the
same refusal and the pair fails. What this cannot see is a t row left out of
rows: that the rows outside the list are cut is the insert's own claim, which
only a server's INSERT ... SELECT ... WHERE over the same body can confirm.

Rules, checked in this order (the first that fails is the answer):
  e_row_rejects    a batch with an `e` verdict (outcome accepted): the insert is
                   rejected with the FIRST e row's code and err, refused_at row,
                   rows_passed, rows_cut and rows_undecided 0, rows_read the
                   batch's, and no output
  export_decline_is_call_decline
                   a batch that is accepted with `export_declined` R (reason id
                   I): the insert is `unsupported`, reason I, code -2, refused_at
                   `body_step`, err the batch's export_declined text, no
                   export_columns, no output, and rows_passed, rows_cut and
                   rows_undecided 0 under a filter (those members, and err,
                   are then not compared with the batch's)
  post_where_cut   a case with batch_of_t_rows, or a filtered batch that is
                   `rejected` with an insert that is `accepted`: the case carries
                   batch_of_t_rows; the batch is `rejected` at `row` or
                   `body_step`, at a refused_row outside rows, and every row in
                   rows is `accepted` in it; the t rows' batch reads exactly
                   those records (one row each, the same input_span lengths,
                   none skipped), is `accepted`, every verdict `t`; the insert's
                   outcome, code, reason, refused_at, export_columns and
                   partition_count are the t rows' batch's, its rows_read,
                   rows_skipped, unsupported_settings and framing the whole
                   batch's, rows_passed the number of t rows, rows_cut the rest
                   of the rows read and not skipped, rows_undecided at most
                   rows_cut, err the t rows' batch's, and the output the t rows'
                   batch's export exactly (generator_draw still applies); the
                   rules below are not applied to such a case
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

RULES = ("e_row_rejects", "export_decline_is_call_decline", "post_where_cut", "shared_members", "counts", "err_prefix",
         "err_suffix", "output_presence", "non_t_row", "line_output", "native_output")
EXCEPTIONS = ("generator_draw",)

# docs.md, chs_preview_insert, "The projection rule", first bullet.
SHARED = ("outcome", "code", "reason", "refused_at", "rows_read", "rows_skipped", "unsupported_settings", "framing",
          "partition_count", "export_columns", "rows_passed", "rows_cut")
# Members a declined output replaces: the insert's own values, by export_decline_is_call_decline.
DECLINE_REPLACES = ("outcome", "code", "reason", "refused_at", "export_columns", "rows_passed", "rows_cut")
COUNTS = ("rows_passed", "rows_cut", "rows_undecided")
INSERT_NEVER = ("export_declined", "export_declined_b64", "export_declined_reason")
# post_where_cut: the members the t rows' insert decides, and those the whole body's read decides.
PWC = "batch_of_t_rows"
PWC_FROM_T_ROWS = ("outcome", "code", "reason", "refused_at", "export_columns", "partition_count")
PWC_FROM_BODY = ("rows_read", "rows_skipped", "unsupported_settings", "framing")
POST_WHERE_SITES = ("row", "body_step")
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
    errs = abimodel.validate(case["insert"], insert_schema)
    if errs:
        bad(f"insert is not a valid insert_result document: {errs[:3]}")
    ran = _batch_shape(case["batch"], _b64(case, "batch_export_b64"), batch_schema, bad, "batch")
    if PWC in case:
        _pwc_shape(case, batch_schema, bad)
    if case["output_format"] == "NONE" and (ran or "export_columns" in case["insert"]):
        bad("NONE: an export member is present")
    e = case["expect"]
    ok = e == "equal" or (isinstance(e, dict) and len(e) == 1 and (
        e.get("exception") in EXCEPTIONS or e.get("violates") in RULES))
    if not ok:
        bad(f"expect {e!r} is not equal, a known exception or a known rule")


def _pwc_shape(case: dict, batch_schema, bad) -> None:
    """batch_of_t_rows: {rows, batch, export_b64}, its batch a consistent batch document over those rows."""
    ev = case[PWC]
    if not isinstance(ev, dict) or set(ev) != {"rows", "batch", "export_b64"}:
        bad(f"{PWC} is not {{rows, batch, export_b64}}")
    idx = ev["rows"]
    if not (isinstance(idx, list) and all(isinstance(i, int) and not isinstance(i, bool) for i in idx)):
        bad(f"{PWC}.rows is not a list of row indexes")
    if idx != sorted(set(idx)) or any(i < 0 or i >= len(case["batch"]["rows"]) for i in idx):
        bad(f"{PWC}.rows is not increasing indexes into the batch's rows")
    if "rows_passed" not in case["batch"]:
        bad(f"{PWC} on a batch with no filter")
    if "rows_passed" not in ev["batch"]:
        bad(f"{PWC}.batch carries no filter")
    _batch_shape(ev["batch"], None if ev["export_b64"] is None else base64.b64decode(ev["export_b64"]),
                 batch_schema, bad, f"{PWC}.batch")


def _batch_shape(batch: dict, exp, batch_schema, bad, what: str) -> bool:
    """A batch document's schema and its consistency with its export; returns whether the export ran."""
    errs = abimodel.validate(batch, batch_schema)
    if errs:
        bad(f"{what} is not a valid batch document: {errs[:3]}")
    ran = "row_spans" in batch or "row_positions" in batch
    if ran != (exp is not None):
        bad(f"{what}'s export is present exactly when it carries a row map")
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
    return ran


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
                and _bytes(ins, "err") == want_err and ins.get("refused_at") == "row"
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

    # post_where_cut: a filtered batch the body steps refused, and an insert that did not refuse with it
    if PWC in case or (filtered and batch["outcome"] == "rejected" and ins["outcome"] == "accepted"):
        got = _post_where_cut(case, out)
        if got is None:
            return "violates", "post_where_cut"
        return got

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


def _post_where_cut(case: dict, out):
    """The insert of a body whose post-WHERE refusal only cut rows raised, against batch_of_t_rows, the batch
    over the rows the insert keeps. Returns a verdict when the pair holds, None when it does not."""
    batch, ins, ev = case["batch"], case["insert"], case.get(PWC)
    if ev is None:  # an accepted insert over a refused body, with no t rows' batch to answer for it
        return None
    rows, idx, sub = batch["rows"], ev["rows"], ev["batch"]
    sexp = None if ev["export_b64"] is None else base64.b64decode(ev["export_b64"])
    # the whole body: refused by a post-WHERE step, at no row the insert keeps
    if batch["outcome"] != "rejected" or batch.get("refused_at") not in POST_WHERE_SITES:
        return None
    if batch.get("refused_at") == "row" and batch.get("refused_row") in idx:
        return None
    if any(rows[i]["outcome"] != "accepted" for i in idx):
        return None
    # the t rows' batch: those records, read alone, accepted, every one t
    if len(sub["rows"]) != len(idx) or sub["rows_read"] != len(idx) or sub["rows_skipped"] != 0:
        return None
    if any(r["input_span"]["len"] != rows[i]["input_span"]["len"] for r, i in zip(sub["rows"], idx)):
        return None
    if sub["outcome"] != "accepted" or any(r.get("verdict") != "t" for r in sub["rows"]):
        return None
    if any(k in sub for k in INSERT_NEVER):
        return None
    # the insert: the t rows' insert, over the whole body's read
    if any(k in ins for k in INSERT_NEVER):
        return None
    if any(_member(sub, m) != _member(ins, m) for m in PWC_FROM_T_ROWS):
        return None
    if any(_member(batch, m) != _member(ins, m) for m in PWC_FROM_BODY):
        return None
    cut = batch["rows_read"] - batch["rows_skipped"] - len(idx)
    if not (ins.get("rows_passed") == len(idx) and ins.get("rows_cut") == cut
            and isinstance(ins.get("rows_undecided"), int) and 0 <= ins["rows_undecided"] <= cut):
        return None
    if _bytes(ins, "err") != _bytes(sub, "err"):
        return None
    if (sexp is None) != (out is None):
        return None
    if out is None:
        return "equal", None
    if any(c.get("src") == "default_generated" for r in sub["rows"] for c in r.get("cols", [])):
        return "exception", "generator_draw"
    # every row of the t rows' batch is t, so its whole export is the t rows' output, in either format
    return ("equal", None) if out == sexp else None


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
    pwc = next((c for c in cases if PWC in c and c["expect"] == "equal"), None)
    if pwc is None:
        fails.append(f"no equal case with {PWC}")
        pwc = base
    for what, start, mutate in (
        ("an invalid batch document", base, lambda c: c["batch"].pop("rows")),
        ("an invalid insert document", base, lambda c: c["insert"].update(rows_read=-1)),
        ("a span outside the export", base, lambda c: c["batch"]["row_spans"][0].update(len=1)),
        ("an unknown rule", base, lambda c: c.update(expect={"violates": "nope"})),
        (f"{PWC} with no rows member", pwc, lambda c: c[PWC].pop("rows")),
        (f"{PWC} rows out of order", pwc, lambda c: c[PWC].update(rows=c[PWC]["rows"][::-1])),
        (f"{PWC} rows past the body", pwc, lambda c: c[PWC]["rows"].append(len(c["batch"]["rows"]))),
        (f"{PWC} with an invalid batch", pwc, lambda c: c[PWC]["batch"].pop("rows")),
        (f"{PWC} with its export missing", pwc, lambda c: c[PWC].update(export_b64=None)),
    ):
        c = copy.deepcopy(start)
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
