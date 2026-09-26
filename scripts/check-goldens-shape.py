#!/usr/bin/env python3
"""check-goldens-shape.py — the served golden set's SHAPE, never its contents
or its hash (chtypes#188).

WHY THIS EXISTS. The golden set is served, not tracked (sdk-goldens.json,
published beside the artifacts): a republish changes what all four bindings
assert against, with no commit and no review on this side. A question about
whether uninitialized bytes could reach a recorded expectation turned on one
property of the CURRENTLY SERVED document: every `err_code` is an integer,
every `reason` is a closed enum, and no field anywhere carries free text — a
server message has nowhere to land. That property was measured once, by hand,
on the bytes served that night. It was never a check, and a claim that is
never checked is exactly this repository's own recorded failure mode (a claim
in a document is not a check — chtypes#185's own lesson, one level up: this
time the "document" is JSON served by someone else, not markdown written
here).

This is an ALLOW-LIST WALK, not a contents check. It never asks whether a
case's `outcome` is the RIGHT outcome, or whether a `reason` is the reason a
real server would give — scripts/check-divergences.py and the golden test
suites themselves already own that question, against loaded artifacts. This
script asks only: does every key that appears, anywhere under `cases`, belong
to a small allow-list of keys this repository has actually reviewed, and does
every string-typed leaf that names a CLOSED vocabulary (an outcome, a verdict,
a Transform reason, a `chs_format` name) belong to that vocabulary? A YES
answer to both is the whole property: nowhere for a server message to hide. A
grown field is not "the library regressed" — it is "the served document now
carries something this repository has not looked at", and the failure message
says exactly that, never the former.

THE VOCABULARIES ARE READ FROM THEIR OWN SOURCE, NEVER HAND-COPIED HERE. A
second list that happens to match today's `Outcome`/`Reason`/`chs_format`
members is exactly the trap this repository's own house rule warns about (a
test that hand-sets a derived value cannot catch a derivation bug): the day
someone adds a sixth Transform reason to python/src/chtypes/results.py and
forgets this file, a hand-copied list would keep silently agreeing with the
old vocabulary forever. So every vocabulary below is parsed, at run time, out
of the same source the bindings themselves are generated from or agree with:

  chs_format names     ts/src/format.ts's `export const Format = { ... }`
  Outcome / FilterOutcome / Reason
                        python/src/chtypes/results.py's own classes
  filter verdicts       docs/guides/filters.md's "Four verdicts" table

`--selftest` proves this empirically, not just structurally: it mutates a
TEMP COPY of results.py to add a reason spelling no real source names, and
shows the checker accepts it once (and only once) it is pointed at that copy
— the one shape of proof a hardcoded list could not fake (see the "pin" test
below).

THE ALLOW-LIST ITSELF (measured against the served document on 2026-09-26;
schema 1, 46 cases):

  top level        schema (int), cases (list), generated (object — metadata,
                    NOT walked; only its type is checked. A republish's own
                    prose, timestamps and refusal notes live there and are
                    none of this script's business)
  case              id, ddl, body, format, filter (all str; format must name
                    a real chs_format), expect (object)
  expect            outcome (str, a known Outcome/FilterOutcome spelling —
                    `ok` included, for a filter case), err_code (int),
                    compile_error_code (int), rows (list), verdicts (list of
                    str, a known filter-verdict spelling)
  row               outcome (str, a known Outcome spelling), err_code (int),
                    values (object str -> str: ClickHouse's own value
                    renderings — this is DATA, not a hazard, and is allowed
                    unrestricted), nulls (list of str), transformed (list of
                    {column: str, reason: str} — reason must be a known
                    Transform reason, exactly these two keys, nothing else),
                    substituted (object str -> str), computed (object str ->
                    str)

Any key outside those lists, anywhere under `cases`, is a problem — and a key
whose OWN NAME looks like it exists to carry a message (`err`, `err_msg`,
`message`, `msg`, `error`, and near spellings) gets a problem that says so by
name, on top of already not being on the allow-list: that name pattern is
exactly the shape a server-message field would take on arrival, and it is
worth surfacing distinctly rather than folding into "unrecognized key" noise.

NON-BLOCKING BY DESIGN, same reasoning and the same job as
scripts/check-divergences.py: a producer republish that changes the SHAPE is
a cross-repo event this repository cannot prevent, and correctly growing the
document (a new optional field the bindings then learn to read) is not this
repository's fault to gate red on. This script's own exit code still means
something (CI reads it into a warning, not a gate) — it is not "always exit
0".

DELIBERATELY NOT CHECKED HERE: the served set's CONTENTS or its hash. The
golden set is meant to move as ClickHouse lines are added and generation
reruns; gating on its bytes would be gating on someone else's correct
republish, which is precisely the mistake chtypes#199 already made once
(a hash churning on a `generated` timestamp) and the divergence check's own
header already declines to repeat.

LOUD REFUSAL, NEVER A SILENT PASS: with no registry and no --goldens
override, or with a golden file that does not exist, or with zero cases in
it, this refuses rather than report a pass that checked nothing (the standing
house rule: scripts/check-divergences.py, check-suite.sh and
check-standalone.sh all refuse a zero-run the same way).

    scripts/check-goldens-shape.py [--registry DIR] [--goldens FILE]
    scripts/check-goldens-shape.py --selftest
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_RESULTS_PY = ROOT / "python" / "src" / "chtypes" / "results.py"
DEFAULT_FORMAT_TS = ROOT / "ts" / "src" / "format.ts"
DEFAULT_FILTERS_MD = ROOT / "docs" / "guides" / "filters.md"

TOP_ALLOWED = {"schema", "cases", "generated"}
CASE_ALLOWED = {"id", "ddl", "body", "format", "filter", "expect"}
EXPECT_ALLOWED = {"outcome", "err_code", "compile_error_code", "rows", "verdicts"}
ROW_ALLOWED = {"outcome", "err_code", "values", "nulls", "transformed", "substituted", "computed"}
TRANSFORM_ALLOWED = {"column", "reason"}

# A field whose own NAME suggests it exists to carry a server message or other
# free text — checked wherever this script enumerates an object's keys under
# `cases` (never inside the `values`/`substituted`/`computed` maps, whose KEYS
# are column names from the caller's own DDL, i.e. data, not schema).
MESSAGE_LIKE_KEYS = {"err", "err_msg", "errmsg", "message", "msg", "error", "err_message", "error_message"}


def die(msg: str) -> None:
    print(f"check-goldens-shape: {msg}", file=sys.stderr)
    sys.exit(1)


def note(msg: str) -> None:
    print(f"    {msg}", file=sys.stderr)


def say(msg: str) -> None:
    print(f"\033[1m==> {msg}\033[0m", file=sys.stderr)


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def is_int(value: object) -> bool:
    """True JSON integers only — `bool` is a subclass of `int` in Python, and
    a golden document has no business carrying `true`/`false` where an
    `err_code` belongs."""
    return isinstance(value, int) and not isinstance(value, bool)


def fail(path: str, reason: str) -> str:
    return (
        f"the served golden set has grown a field (or value) this repository never reviewed: {path} — "
        f"review whether it can carry free text before extending the allow-list here ({reason})"
    )


# ========================================================= vocabulary loaders ===
#
# Every vocabulary below is PARSED, not hand-typed, from the source it must
# agree with. Each loader is intentionally narrow (one class body, one JS
# object literal, one markdown table) so a source file that moved or was
# renamed fails loudly here rather than silently returning an empty set that
# then rejects every real spelling.

_MEMBER_RE = re.compile(r'^ {4}([A-Z][A-Z0-9_]*)\s*(?::\s*Final)?\s*=\s*"([^"]*)"\s*$')


def _read_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        die(f"could not read {path} to derive a vocabulary from it: {exc}")
        raise AssertionError("unreachable")  # for the type checker; die() never returns


def _class_body(text: str, source_label: str, class_name: str) -> str:
    """The lines of `class {class_name}` (`(...):` or `:`), up to but not
    including the next line that starts at column 0 — i.e. the next
    top-level statement. Deliberately line-based rather than an AST parse:
    this file reads OTHER repositories' source as data, and a line scan is
    the same trust boundary scripts/check-divergences.py already draws
    around docs/limitations.md's headings."""
    pattern = re.compile(rf"^class {re.escape(class_name)}\b")
    lines = text.splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if pattern.match(line)), None)
    if start is None:
        die(f"could not find 'class {class_name}' in {source_label} — its vocabulary source may have moved")
    body: list[str] = []
    for line in lines[start:]:
        if line and not line[0].isspace():
            break
        body.append(line)
    return "\n".join(body)


def load_enum_values(path: Path, class_name: str) -> frozenset[str]:
    """The string values of every `NAME = "value"` / `NAME: Final = "value"`
    member directly inside `class {class_name}`, one indent level deep — the
    shape both the StrEnum classes (Outcome, FilterOutcome) and the plain
    Reason class share in python/src/chtypes/results.py. A `@classmethod`
    body sits two indents deeper and never matches."""
    label = rel(path)
    body = _class_body(_read_source(path), label, class_name)
    values = {m.group(2) for line in body.splitlines() if (m := _MEMBER_RE.match(line))}
    if not values:
        die(f"found 'class {class_name}' in {label} but parsed no string-valued members from it — its shape may have changed")
    return frozenset(values)


_FORMAT_BLOCK_RE = re.compile(r"export const Format = \{(.*?)\}\s*as const;", re.DOTALL)
_FORMAT_MEMBER_RE = re.compile(r"^ {2}([A-Za-z][A-Za-z0-9_]*):\s*-?\d+,?\s*$")


def load_format_names(path: Path) -> frozenset[str]:
    """Every `chs_format` name from ts/src/format.ts's own
    `export const Format = { ... } as const;` object literal — the same
    constant TypeScript callers import, so a format this repository has never
    heard of cannot pass this check by accident."""
    label = rel(path)
    text = _read_source(path)
    match = _FORMAT_BLOCK_RE.search(text)
    if not match:
        die(f"could not find 'export const Format = {{ ... }} as const;' in {label} — the format vocabulary source may have moved")
    names = {m.group(1) for line in match.group(1).splitlines() if (m := _FORMAT_MEMBER_RE.match(line))}
    if not names:
        die(f"found the Format object literal in {label} but parsed no member names from it — its shape may have changed")
    return frozenset(names)


_VERDICT_HEADING = "## Four verdicts"
_VERDICT_ROW_RE = re.compile(r"^\|\s*`([a-z]+)`\s*\|")


def load_verdict_values(path: Path) -> frozenset[str]:
    """The spelled-out filter-verdict vocabulary (`true`/`false`/`error`/
    `decline`) from docs/guides/filters.md's own "Four verdicts" table — the
    golden set spells these words out (python/tests/test_golden.py's own
    VERDICTS mapping goes the other way, from the wire character), so the
    doc's table, not the wire-character StrEnum, is the source that actually
    matches what a golden document contains."""
    label = rel(path)
    lines = _read_source(path).splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if line.startswith(_VERDICT_HEADING)), None)
    if start is None:
        die(f"could not find a {_VERDICT_HEADING!r} heading in {label} — the verdict vocabulary source may have moved")
    values: set[str] = set()
    for line in lines[start:]:
        if line.startswith("## "):
            break
        if m := _VERDICT_ROW_RE.match(line):
            values.add(m.group(1))
    if not values:
        die(f"found {_VERDICT_HEADING!r} in {label} but parsed no backtick-quoted verdict names from its table — its shape may have changed")
    return frozenset(values)


@dataclass(frozen=True)
class Vocab:
    formats: frozenset[str]
    case_outcomes: frozenset[str]
    row_outcomes: frozenset[str]
    verdicts: frozenset[str]
    reasons: frozenset[str]


def load_vocab(results_py: Path, format_ts: Path, filters_md: Path) -> Vocab:
    outcomes = load_enum_values(results_py, "Outcome")
    filter_outcomes = load_enum_values(results_py, "FilterOutcome")
    return Vocab(
        formats=load_format_names(format_ts),
        # A case-level `expect.outcome` is a batch outcome OR a filter-call
        # outcome ("ok" is FilterOutcome-only) — the union of both is exactly
        # "the documented outcome spellings incl. `ok` for filter cases".
        case_outcomes=outcomes | filter_outcomes,
        # A ROW's own outcome is always the batch/row Outcome vocabulary —
        # "ok" is a call-level filter verdict and never appears on a row.
        row_outcomes=outcomes,
        verdicts=load_verdict_values(filters_md),
        reasons=load_enum_values(results_py, "Reason"),
    )


# ================================================================ the walk ===


def check_object_keys(obj: dict[str, Any], allowed: set[str], path: str, problems: list[str]) -> None:
    for key in obj:
        full = f"{path}.{key}"
        if key in MESSAGE_LIKE_KEYS:
            problems.append(fail(full, f"the key's own name ({key!r}) is exactly the shape a server-message field would take"))
        elif key not in allowed:
            problems.append(fail(full, f"unrecognized key {key!r} — known keys here: {sorted(allowed)}"))


def check_str_map(value: object, path: str, problems: list[str]) -> None:
    """An object whose keys are column names (data) and whose values are
    ClickHouse's own text renderings — checked for TYPE only, never for key
    names, exactly like `values` (`substituted`/`computed` share the shape)."""
    if not isinstance(value, dict):
        problems.append(fail(path, f"expected an object of string -> string, got {type(value).__name__}"))
        return
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            problems.append(fail(f"{path}.{k}", "expected a string value (a column rendering is always text)"))


def check_transform_entry(entry: object, path: str, vocab: Vocab, problems: list[str]) -> None:
    if not isinstance(entry, dict):
        problems.append(fail(path, f"expected an object, got {type(entry).__name__}"))
        return
    check_object_keys(entry, TRANSFORM_ALLOWED, path, problems)
    missing = TRANSFORM_ALLOWED - entry.keys()
    if missing:
        problems.append(fail(path, f"missing {sorted(missing)} — a Transform entry is exactly {{column, reason}}, nothing more, nothing less"))
    if "column" in entry and not isinstance(entry["column"], str):
        problems.append(fail(f"{path}.column", "expected a string"))
    if "reason" in entry:
        reason = entry["reason"]
        if not isinstance(reason, str):
            problems.append(fail(f"{path}.reason", "expected a string"))
        elif reason not in vocab.reasons:
            problems.append(fail(f"{path}.reason", f"unrecognized Transform reason {reason!r} — known: {sorted(vocab.reasons)}"))


def check_row(row: object, path: str, vocab: Vocab, problems: list[str]) -> None:
    if not isinstance(row, dict):
        problems.append(fail(path, f"expected an object, got {type(row).__name__}"))
        return
    check_object_keys(row, ROW_ALLOWED, path, problems)
    if "outcome" in row:
        outcome = row["outcome"]
        if not isinstance(outcome, str):
            problems.append(fail(f"{path}.outcome", "expected a string"))
        elif outcome not in vocab.row_outcomes:
            problems.append(fail(f"{path}.outcome", f"unrecognized row outcome {outcome!r} — known: {sorted(vocab.row_outcomes)}"))
    if "err_code" in row and not is_int(row["err_code"]):
        problems.append(fail(f"{path}.err_code", "expected an integer"))
    if "values" in row:
        check_str_map(row["values"], f"{path}.values", problems)
    if "nulls" in row:
        nulls = row["nulls"]
        if not isinstance(nulls, list) or not all(isinstance(x, str) for x in nulls):
            problems.append(fail(f"{path}.nulls", "expected a list of column-name strings"))
    if "transformed" in row:
        transformed = row["transformed"]
        if not isinstance(transformed, list):
            problems.append(fail(f"{path}.transformed", "expected a list"))
        else:
            for i, entry in enumerate(transformed):
                check_transform_entry(entry, f"{path}.transformed[{i}]", vocab, problems)
    if "substituted" in row:
        check_str_map(row["substituted"], f"{path}.substituted", problems)
    if "computed" in row:
        check_str_map(row["computed"], f"{path}.computed", problems)


def check_expect(expect: object, path: str, vocab: Vocab, problems: list[str]) -> None:
    if not isinstance(expect, dict):
        problems.append(fail(path, f"expected an object, got {type(expect).__name__}"))
        return
    check_object_keys(expect, EXPECT_ALLOWED, path, problems)
    if "outcome" in expect:
        outcome = expect["outcome"]
        if not isinstance(outcome, str):
            problems.append(fail(f"{path}.outcome", "expected a string"))
        elif outcome not in vocab.case_outcomes:
            problems.append(fail(f"{path}.outcome", f"unrecognized outcome {outcome!r} — known: {sorted(vocab.case_outcomes)}"))
    if "err_code" in expect and not is_int(expect["err_code"]):
        problems.append(fail(f"{path}.err_code", "expected an integer"))
    if "compile_error_code" in expect and not is_int(expect["compile_error_code"]):
        problems.append(fail(f"{path}.compile_error_code", "expected an integer"))
    if "rows" in expect:
        rows = expect["rows"]
        if not isinstance(rows, list):
            problems.append(fail(f"{path}.rows", "expected a list"))
        else:
            for i, row in enumerate(rows):
                check_row(row, f"{path}.rows[{i}]", vocab, problems)
    if "verdicts" in expect:
        verdicts = expect["verdicts"]
        if not isinstance(verdicts, list):
            problems.append(fail(f"{path}.verdicts", "expected a list"))
        else:
            for i, verdict in enumerate(verdicts):
                if not isinstance(verdict, str):
                    problems.append(fail(f"{path}.verdicts[{i}]", "expected a string"))
                elif verdict not in vocab.verdicts:
                    problems.append(fail(f"{path}.verdicts[{i}]", f"unrecognized verdict {verdict!r} — known: {sorted(vocab.verdicts)}"))


def check_case(case: object, path: str, vocab: Vocab, problems: list[str]) -> None:
    if not isinstance(case, dict):
        problems.append(fail(path, f"expected an object, got {type(case).__name__}"))
        return
    check_object_keys(case, CASE_ALLOWED, path, problems)
    for key in ("id", "ddl", "body", "filter"):
        if key in case and not isinstance(case[key], str):
            problems.append(fail(f"{path}.{key}", "expected a string"))
    if "format" in case:
        fmt = case["format"]
        if not isinstance(fmt, str):
            problems.append(fail(f"{path}.format", "expected a string"))
        elif fmt not in vocab.formats:
            problems.append(fail(f"{path}.format", f"unrecognized chs_format name {fmt!r} — known: {sorted(vocab.formats)}"))
    if "expect" in case:
        check_expect(case["expect"], f"{path}.expect", vocab, problems)
    else:
        problems.append(fail(f"{path}.expect", "missing — every case must carry an 'expect' object"))


def check_shape(doc: object, vocab: Vocab) -> list[str]:
    problems: list[str] = []
    if not isinstance(doc, dict):
        return [fail("$", f"expected the document root to be an object, got {type(doc).__name__}")]
    check_object_keys(doc, TOP_ALLOWED, "$", problems)
    if "schema" in doc and not is_int(doc["schema"]):
        problems.append(fail("$.schema", "expected an integer"))
    if "generated" in doc and not isinstance(doc["generated"], dict):
        problems.append(fail("$.generated", "expected an object (its CONTENTS are metadata and are deliberately not walked)"))
    cases = doc.get("cases")
    if not isinstance(cases, list):
        problems.append(fail("$.cases", "expected a list"))
        return problems
    for i, case in enumerate(cases):
        check_case(case, f"$.cases[{i}]", vocab, problems)
    return problems


# =================================================================== main ===


def load_doc(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        die(f"could not read {path}: {exc}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        die(f"{path} is not valid JSON: {exc}")


def run(registry_dir: str | None, goldens_override: str | None, results_py: Path, format_ts: Path, filters_md: Path) -> int:
    if not goldens_override and not registry_dir:
        die(
            "no golden set named — neither --goldens / $CHTYPES_GOLDENS nor --registry / $CHTYPES_REGISTRY "
            "was given, and this check needs the served sdk-goldens.json; refusing to report a pass that "
            "checked nothing"
        )
    goldens_path = Path(goldens_override) if goldens_override else Path(registry_dir) / "sdk-goldens.json"  # type: ignore[arg-type]
    if not goldens_path.is_file():
        die(
            f"{goldens_path} does not exist — scripts/fetch.sh --release-file sdk-goldens.json "
            f"--dest {registry_dir or '<registry>'} installs it; this script never fetches for itself"
        )
    doc = load_doc(goldens_path)
    cases = doc.get("cases") if isinstance(doc, dict) else None
    if not cases:
        die(f"{goldens_path} names no cases — a zero-case document proves nothing about the served shape, so this refuses rather than pass")

    vocab = load_vocab(results_py, format_ts, filters_md)
    say(f"checking the shape of {len(cases)} case(s) in {goldens_path}")
    note(f"chs_format names from {rel(format_ts)}: {sorted(vocab.formats)}")
    note(f"Outcome/FilterOutcome spellings from {rel(results_py)}: {sorted(vocab.case_outcomes)}")
    note(f"filter verdicts from {rel(filters_md)}: {sorted(vocab.verdicts)}")
    note(f"Transform reasons from {rel(results_py)}: {sorted(vocab.reasons)}")

    problems = check_shape(doc, vocab)
    for p in problems:
        print(p, file=sys.stderr)
    if problems:
        print(
            f"\ncheck-goldens-shape: {len(problems)} problem(s) — the served golden set carries a field or "
            "value this repository's allow-list has not reviewed. This is not a claim the library regressed.",
            file=sys.stderr,
        )
        return 1
    print(f"check-goldens-shape: ok — all {len(cases)} case(s) in {goldens_path} match the reviewed allow-list; no field carries free text")
    return 0


# ================================================================ selftest ===


def _synthetic_doc() -> dict[str, Any]:
    """Mirrors the shapes actually measured in the served document on
    2026-09-26: a batch case with a transform/null/computed row, a filter
    case (whose case-level outcome is `ok`), and a compile-error case."""
    return {
        "schema": 1,
        "generated": {
            "exact": {"25.8": "25.8.33.6-lts"},
            "note": "free text is fine here — 'generated' is metadata, never walked",
        },
        "cases": [
            {
                "id": "batch-case",
                "ddl": "x UInt8, y UInt8, s String",
                "format": "JSONEachRow",
                "body": '{"x":1}\n',
                "expect": {
                    "outcome": "accepted",
                    "err_code": 0,
                    "rows": [
                        {
                            "outcome": "accepted",
                            "values": {"x": "1", "y": "7", "s": '""'},
                            "nulls": [],
                            "transformed": [{"column": "y", "reason": "default_filled"}],
                            "substituted": {},
                            "computed": {"m": "6"},
                        }
                    ],
                },
            },
            {
                "id": "filter-case",
                "ddl": "x UInt8",
                "format": "JSONEachRow",
                "body": '{"x":1}\n{"x":5}\n',
                "filter": "x > 1",
                "expect": {"outcome": "ok", "verdicts": ["false", "true"]},
            },
            {
                "id": "compile-error-case",
                "ddl": "x Nope",
                "format": "JSONEachRow",
                "body": "",
                "expect": {"compile_error_code": 62},
            },
        ],
    }


def _mutate(doc: dict[str, Any], fn: Any) -> dict[str, Any]:
    d = copy.deepcopy(doc)
    fn(d)
    return d


def selftest() -> int:
    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    vocab = load_vocab(DEFAULT_RESULTS_PY, DEFAULT_FORMAT_TS, DEFAULT_FILTERS_MD)
    check(len(vocab.formats) >= 10, f"suspiciously few chs_format names parsed from {rel(DEFAULT_FORMAT_TS)}: {sorted(vocab.formats)}")
    check("JSONEachRow" in vocab.formats and "TSVWithNames" in vocab.formats, f"expected format names missing: {sorted(vocab.formats)}")
    check(
        {"accepted", "rejected", "accepted_poisoned", "unsupported", "skipped", "ok"} <= vocab.case_outcomes,
        f"case outcome vocabulary is missing an expected spelling: {sorted(vocab.case_outcomes)}",
    )
    check("ok" not in vocab.row_outcomes, "row outcome vocabulary should NOT include 'ok' — that is a call-level filter outcome, never a row's own")
    check({"true", "false", "error", "decline"} <= vocab.verdicts, f"verdict vocabulary is incomplete: {sorted(vocab.verdicts)}")
    check(
        {"overflow_wrap", "duplicate_key_dropped", "reformat", "default_filled", "zero_filled"} <= vocab.reasons,
        f"Transform reason vocabulary is incomplete: {sorted(vocab.reasons)}",
    )

    # ---- the real, currently-served shape must pass outright.
    doc = _synthetic_doc()
    problems = check_shape(doc, vocab)
    check(problems == [], f"a synthetic doc mirroring the real served shape was reported as having problem(s): {problems}")

    # ---- an unknown case key must fail, naming the path.
    mutated = _mutate(doc, lambda d: d["cases"][0].__setitem__("settings", {}))
    problems = check_shape(mutated, vocab)
    check(any("$.cases[0].settings" in p for p in problems), f"an unknown case key ('settings') was not caught by path: {problems}")

    # ---- an unknown row key must fail, naming the path.
    mutated = _mutate(doc, lambda d: d["cases"][0]["expect"]["rows"][0].__setitem__("weird_field", 1))
    problems = check_shape(mutated, vocab)
    check(any("rows[0].weird_field" in p for p in problems), f"an unknown row key was not caught by path: {problems}")

    # ---- an err_msg field must fail, BY NAME, distinctly from a plain
    # unrecognized key — this is the central hazard the issue exists to catch.
    mutated = _mutate(doc, lambda d: d["cases"][0]["expect"]["rows"][0].__setitem__("err_msg", "Code: 27. DB::Exception: ..."))
    problems = check_shape(mutated, vocab)
    check(
        any("rows[0].err_msg" in p and "server-message" in p for p in problems),
        f"an 'err_msg' field was not caught by its own name: {problems}",
    )

    # ---- a non-int err_code must fail, naming the path.
    mutated = _mutate(doc, lambda d: d["cases"][0]["expect"].__setitem__("err_code", "27"))
    problems = check_shape(mutated, vocab)
    check(any("expect.err_code" in p and "integer" in p for p in problems), f"a non-int err_code was not caught: {problems}")

    # ---- a bool must not sneak past the int check (bool is an int subclass).
    mutated = _mutate(doc, lambda d: d["cases"][0]["expect"].__setitem__("err_code", True))
    problems = check_shape(mutated, vocab)
    check(any("expect.err_code" in p and "integer" in p for p in problems), f"a bool err_code was wrongly accepted as an integer: {problems}")

    # ---- an unknown Transform reason must fail, naming the path and value.
    mutated = _mutate(doc, lambda d: d["cases"][0]["expect"]["rows"][0]["transformed"][0].__setitem__("reason", "quantum_flux"))
    problems = check_shape(mutated, vocab)
    check(
        any("transformed[0].reason" in p and "quantum_flux" in p for p in problems),
        f"an unknown Transform reason was not caught: {problems}",
    )

    # ---- an unknown case-level outcome must fail, naming the path and value.
    mutated = _mutate(doc, lambda d: d["cases"][0]["expect"].__setitem__("outcome", "reticulated"))
    problems = check_shape(mutated, vocab)
    check(
        any("expect.outcome" in p and "reticulated" in p for p in problems),
        f"an unknown case outcome was not caught: {problems}",
    )

    # ---- a Transform entry missing 'reason' (or carrying an extra key) is
    # also a problem — "exactly column and reason", per the allow-list.
    mutated = _mutate(doc, lambda d: d["cases"][0]["expect"]["rows"][0]["transformed"][0].pop("reason"))
    problems = check_shape(mutated, vocab)
    check(any("transformed[0]" in p and "missing" in p for p in problems), f"a Transform entry missing 'reason' was not caught: {problems}")

    # ---- THE PIN: the Reason vocabulary must be READ from its source, never
    # hand-copied beside this file (the house rule: a test that hand-sets a
    # derived value cannot catch a derivation bug). Mutate a TEMP COPY of
    # results.py to add a reason no real source names, and require BOTH
    # directions: rejected against the real (unmutated) source, accepted only
    # once pointed at the mutated copy. A hardcoded Python list matching
    # today's vocabulary would keep rejecting it even when handed the
    # mutated file's path — that is exactly the failure this pins against.
    with tempfile.TemporaryDirectory() as tmp:
        original = DEFAULT_RESULTS_PY.read_text(encoding="utf-8")
        marker = '    DUPLICATE_KEY_DROPPED: Final = "duplicate_key_dropped"'
        check(marker in original, f"the results.py mutation anchor line was not found in {DEFAULT_RESULTS_PY} — the source may have moved")
        mutated_source = original.replace(marker, f'{marker}\n    QUANTUM_FLUX: Final = "quantum_flux"', 1)
        mutated_results_py = Path(tmp) / "results.py"
        mutated_results_py.write_text(mutated_source, encoding="utf-8")

        quantum_doc = _mutate(doc, lambda d: d["cases"][0]["expect"]["rows"][0]["transformed"][0].__setitem__("reason", "quantum_flux"))

        vocab_real = load_vocab(DEFAULT_RESULTS_PY, DEFAULT_FORMAT_TS, DEFAULT_FILTERS_MD)
        problems_real = check_shape(quantum_doc, vocab_real)
        check(
            any("quantum_flux" in p for p in problems_real),
            "a reason absent from the REAL results.py was accepted — the reason vocabulary is not being checked at all",
        )

        vocab_mutated = load_vocab(mutated_results_py, DEFAULT_FORMAT_TS, DEFAULT_FILTERS_MD)
        problems_mutated = check_shape(quantum_doc, vocab_mutated)
        check(
            not any("quantum_flux" in p for p in problems_mutated),
            "pointed at a MUTATED results.py that now names 'quantum_flux' as a Reason, the checker still "
            "rejected it — the reason vocabulary is being read from somewhere other than the given source "
            "(a hardcoded list would fail exactly this way)",
        )

    if failures:
        for f in failures:
            print(f"SELFTEST FAILED: {f}", file=sys.stderr)
        return 1
    print(
        "check-goldens-shape: selftest ok — the real served shape passes; an unknown case/row key, an "
        "err_msg field (caught by name), a non-int err_code (bool included), an unknown Transform reason, "
        "an unknown case outcome, and an incomplete Transform entry are all caught by path; and the Reason "
        "vocabulary is proven to be read from its source rather than hardcoded (rejected against the real "
        "file, accepted only once pointed at a mutated copy that actually names the new spelling)"
    )
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--registry", default=os.environ.get("CHTYPES_REGISTRY"))
    parser.add_argument("--goldens", default=os.environ.get("CHTYPES_GOLDENS"))
    parser.add_argument("--results-py", type=Path, default=DEFAULT_RESULTS_PY)
    parser.add_argument("--format-ts", type=Path, default=DEFAULT_FORMAT_TS)
    parser.add_argument("--filters-md", type=Path, default=DEFAULT_FILTERS_MD)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    return run(args.registry, args.goldens, args.results_py, args.format_ts, args.filters_md)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
