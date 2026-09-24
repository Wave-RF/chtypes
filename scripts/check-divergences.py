#!/usr/bin/env python3
"""check-divergences.py — docs/limitations.md's "Known divergences" section,
checked against loaded artifacts (chtypes#185).

WHY THIS EXISTS. That section makes falsifiable claims about this library's
own behavior — "this library answers `accepted` for a row a real server
refuses, on 26.2 and 26.3" — and until now nothing in this repository
asserted them. When the artifact producer fixes a divergence, every existing
suite stays green and the page silently becomes wrong: the repository's own
recorded failure mode, "a claim in a document is not a check", one entry
short of being closed for this page. (A second entry was proposed alongside
this one and withdrawn before landing — a re-measurement against a real
MergeTree table, rather than the Memory-engine table the original measurement
used, found no divergence there at all. That is exactly the failure mode this
script exists to catch, one level up: an unchecked claim about the library
was wrong from the day it was written, not merely stale.)

This is NOT a differential suite. It never asks what a real server does —
that answer is not measured here and never will be (there is no ClickHouse
server in this repository); it asks only whether THIS LIBRARY still answers
the way the page says it does, against artifacts fetched for the purpose.
Reusing a ClickHouse rule here would be exactly the mistake the rest of this
repository refuses to make.

    scripts/check-divergences.py [--registry DIR] [--data FILE] [--doc FILE]
    scripts/check-divergences.py --print-lines     the ClickHouse lines every
                                                    check names, one per line,
                                                    sorted — what a caller
                                                    (a CI step) must fetch
                                                    before a real run means
                                                    anything. Never hand-typed
                                                    beside this file, for the
                                                    same reason the golden set
                                                    is generated rather than
                                                    written twice.
    scripts/check-divergences.py --selftest        prove the two checks below
                                                    actually fire, offline

TWO CHECKS, BOTH REQUIRED, NEITHER A SUBSTITUTE FOR THE OTHER.

  COVERAGE — every `###` heading under docs/limitations.md's "## Known
  divergences" has exactly one entry in docs/divergences.json (matched by
  heading text, verbatim) and every entry there names a heading that exists.
  Needs no artifact and no network; it is a pure function of the two files,
  and it is what catches an entry someone deleted from one file and not the
  other — prose and claim drifting apart is the exact trap this exists to
  close (issue #185's own correction: "the expectations must not be
  hand-maintained beside the page").

  REALITY — every one of docs/divergences.json's "checks" is driven against
  the loaded artifact for each ClickHouse line it names, and the artifact's
  own answer (outcome, and — only where the page itself states one — the
  error code and message) must match what is asserted. A mismatch means the
  page and the artifacts disagree; which direction is stated in the failure
  itself, because whoever reads it must not conclude the library regressed
  when it is the page that is now stale (or the reverse — a check newly
  failing to reproduce what it once did is a real regression, and the
  message says that plainly instead).

NON-BLOCKING BY DESIGN, same reasoning as the `docs` job's own comment in
.github/workflows/ci.yml and scripts/support-matrix.sh's header: the artifact
producer fixing a divergence is a cross-repo event this repository cannot
prevent, so a red here on someone else's correct work would be the same
mistake `docs` and support-matrix already declined to make. This script's
own exit code is still meaningful (CI reads it into a warning, not a gate);
it is not "always exit 0".

SKIPS ARE LOUD AND NAMED, AND A ZERO-RUN REFUSES. With no registry at all,
or with every line this file names simply absent from it, the reality check
proves nothing — and reporting that as a pass would be the exact failure
this script exists to prevent one level up. It says so, by name, and exits
non-zero (the standing house rule: scripts/check-suite.sh and
scripts/check-standalone.sh both refuse a run that checked nothing). A case
whose (platform, line) pair is on the release's own SERVED `unbuildable`
list (index.json's top-level array) is a by-design gap, not a failure of
this checker or a finding about the library — it is skipped, loudly, by
name, exactly as scripts/check-standalone.sh already treats that list.

AN EMPTIED REGISTER IS A VALID END STATE, NOT A ZERO-RUN. docs/limitations.md
says, in its own words, "An entry disappears when an artifact stops
diverging" — the register is expected to reach zero entries one day, and
this script must survive that (chtypes#185's own defect: it used to die on
"names no entries" regardless of why the count was zero). Zero entries in
docs/divergences.json AND zero `###` headings under "## Known divergences"
in docs/limitations.md means the page and the data agree there is nothing
to check, which is not the same shape as the zero-run refusal above — it
passes, loudly, saying so, and needs no registry to do it. Zero of both
with the section heading itself missing or renamed is a different, real
failure: indistinguishable from the valid case without checking for the
heading text directly, so that refuses rather than guess.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python" / "src"))

DEFAULT_DATA = ROOT / "docs" / "divergences.json"
DEFAULT_DOC = ROOT / "docs" / "limitations.md"
SECTION_HEADING = "## Known divergences"

# The same format spellings the golden set uses (python/tests/test_golden.py
# FORMATS), mapped to chtypes.Format's own member names (python/src/chtypes/
# results.py) — one hand-typed table, not a mechanical rule that would get an
# acronym boundary like JSONEachRow -> JSON_EACH_ROW wrong.
_FORMATS = {
    "JSONEachRow": "JSON_EACH_ROW",
    "CSV": "CSV",
    "TSV": "TSV",
    "Values": "VALUES",
    "JSONCompactEachRow": "JSON_COMPACT_EACH_ROW",
    "RowBinary": "ROW_BINARY",
    "RowBinaryWithDefaults": "ROW_BINARY_WITH_DEFAULTS",
    "RowBinaryWithNamesAndTypesAndDefaults": "ROW_BINARY_WITH_NAMES_AND_TYPES_AND_DEFAULTS",
    "Native": "NATIVE",
    "Buffers": "BUFFERS",
    "CSVWithNames": "CSV_WITH_NAMES",
    "TSVWithNames": "TSV_WITH_NAMES",
}
_FORMAT_NAMES = tuple(_FORMATS)


def die(msg: str) -> None:
    print(f"check-divergences: {msg}", file=sys.stderr)
    sys.exit(1)


def note(msg: str) -> None:
    print(f"    {msg}", file=sys.stderr)


def say(msg: str) -> None:
    print(f"\033[1m==> {msg}\033[0m", file=sys.stderr)


# ============================================================== COVERAGE ===


def parse_divergence_headings(doc_text: str) -> list[str]:
    """The `### ...` heading texts inside the '## Known divergences' section
    of docs/limitations.md, in document order. Any other `## ` heading ends
    the section (docs/limitations.md has several top-level sections; only
    this one's subheadings are in scope)."""
    headings: list[str] = []
    in_section = False
    for line in doc_text.splitlines():
        if line.startswith("## "):
            in_section = line.rstrip() == SECTION_HEADING
            continue
        if in_section and line.startswith("### "):
            headings.append(line[len("### ") :].strip())
    return headings


def has_divergences_section(doc_text: str) -> bool:
    """Whether SECTION_HEADING itself appears in the doc, verbatim, as a
    line of its own — independent of whether anything is inside it.
    parse_divergence_headings() cannot make this distinction: a section
    that exists but is empty and a section that was renamed or deleted
    both come back as headings == []."""
    return any(line.rstrip() == SECTION_HEADING for line in doc_text.splitlines())


def classify_register(entries: list[dict[str, Any]], headings: list[str], section_present: bool) -> str:
    """Classify the Known-divergences register before anything that needs a
    registry or the network runs. Three states, not two, because the
    obvious two-way split (entries-or-headings vs. not) cannot tell an
    intentionally emptied register apart from a doc whose section heading
    itself got renamed or removed — both produce zero entries AND zero
    headings, and only one of those is the valid end state
    docs/limitations.md's own text describes ("An entry disappears when an
    artifact stops diverging").

      "active"  — at least one entry or one heading exists; the ordinary
                  coverage + reality path applies, unchanged.
      "empty"   — no entries, no headings, and the section heading is still
                  present in the doc: a valid, intentionally empty
                  register.
      "missing" — no entries, no headings, and the section heading itself
                  is gone too: a real error, not an empty register.

    Pure and dependency-free, like coverage_problems() and compare(), so
    --selftest proves all three without a doc file, a data file, a
    registry, or the network.
    """
    if entries or headings:
        return "active"
    return "empty" if section_present else "missing"


def coverage_problems(headings: list[str], entries: list[dict[str, Any]], data_rel: str, doc_rel: str) -> list[str]:
    """Both halves of the bijection: a heading with no entry, and an entry
    with no heading. Either one existing alone is exactly the drift this
    script exists to close."""
    problems: list[str] = []
    by_heading: dict[str, list[str]] = {}
    for e in entries:
        by_heading.setdefault(e["heading"], []).append(e["id"])

    for h in headings:
        ids = by_heading.get(h, [])
        if not ids:
            problems.append(
                f"the page and its check data disagree — update {doc_rel} or {data_rel}: "
                f"### {h!r} in {doc_rel} has no matching entry in {data_rel}"
            )
        elif len(ids) > 1:
            problems.append(
                f"the page and its check data disagree — update {data_rel}: "
                f"### {h!r} matches {len(ids)} entries in {data_rel} ({', '.join(ids)}); exactly one is required"
            )

    heading_set = set(headings)
    for e in entries:
        if e["heading"] not in heading_set:
            problems.append(
                f"the page and its check data disagree — update {doc_rel} or {data_rel}: "
                f"entry {e['id']!r} in {data_rel} names heading {e['heading']!r}, which is not a "
                f"### heading under {SECTION_HEADING!r} in {doc_rel}"
            )
    return problems


# ================================================================ REALITY ===


def load_unbuildable(artifacts_url: str) -> set[tuple[str, str]]:
    """The release's own SERVED exclusion list — index.json's top-level
    `unbuildable` array — as a set of (platform, clickhouse_minor) pairs.
    Read live, exactly as scripts/check-standalone.sh does, never hand-typed
    here: a (platform, line) pair the release cannot build is a by-design
    gap, not a finding about this library, and which pairs those are is the
    release's own statement, not this script's guess."""
    url = artifacts_url.rstrip("/") + "/artifacts/index.json"
    # A User-Agent is required: the host answers a bare urllib request (its
    # default "Python-urllib/3.x") with 403, measured against the live host.
    req = urllib.request.Request(url, headers={"User-Agent": "chtypes-check-divergences"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (fixed https host)
            doc = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        die(
            f"could not fetch {url} to read the served 'unbuildable' exclusion list (chtypes#150) — "
            f"without it a by-design gap cannot be told apart from a real failure, so this refuses "
            f"rather than guess ({exc})"
        )
    return {(f"{e['os']}-{e['arch']}", e["clickhouse_minor"]) for e in doc.get("unbuildable", [])}


def compare(expect: dict[str, Any], actual: dict[str, Any]) -> str | None:
    """The one comparison every case reduces to. Returns a mismatch
    description, or None when the artifact's answer matches what is
    asserted. Pure and dependency-free on purpose — --selftest drives this
    directly, so it is proven against a fabricated wrong answer without
    needing a registry or the network."""
    got_outcome = str(actual["outcome"])
    want_outcome = expect["outcome"]
    if got_outcome != want_outcome:
        return f"expected outcome {want_outcome!r}, the artifact answered {got_outcome!r}"
    if "err_code" in expect and actual.get("err_code") != expect["err_code"]:
        return f"expected err_code {expect['err_code']!r}, the artifact answered {actual.get('err_code')!r}"
    if "err_msg" in expect and actual.get("err_msg") != expect["err_msg"]:
        return f"expected err_msg {expect['err_msg']!r}, the artifact answered {actual.get('err_msg')!r}"
    return None


def run_check(library: Any, chk: dict[str, Any], chtypes_mod: Any) -> dict[str, Any]:
    """Drive one check's schema/format/payload/settings through a loaded
    Library and return {"outcome", "err_code", "err_msg"}. Settings are
    compiled as the DECLARED profile (docs/guides/settings.md "The one
    exception: type gates bind at compile") — every entry here gates a
    TYPE (allow_experimental_object_type and the like), never a per-call
    parsing knob, so the compile profile is where a real caller would put
    it too. May raise chtypes.SchemaError (a genuine compile-time rejection
    — including the server's own code 115 for a setting name this artifact
    does not recognize) or chtypes.RegistryError (the line would not load
    at all); the caller decides what either means."""
    fmt = getattr(chtypes_mod.Format, _FORMATS[chk["format"]])
    body = chk["payload"].encode("utf-8")
    settings = chk.get("settings") or None
    with library.compile_ddl(chk["schema"], settings=settings) as schema:
        br = schema.rows(fmt, body, None)
    return {"outcome": br.outcome, "err_code": br.err_code, "err_msg": br.err_msg}


def resolve_case(
    chk: dict[str, Any],
    line: str,
    registry: Any,
    platform: str,
    unbuildable: set[tuple[str, str]],
    chtypes_mod: Any,
) -> tuple[str, str]:
    """Resolve one (check, line) pair against a Registry.

    Returns (status, message): status is one of "skip", "problem", "ok".
    This is the one function --selftest drives directly with a fake
    registry, so the skip/problem/ok decision is proven against the SAME
    code the real run uses — not a second copy of the logic.
    """
    if (platform, line) in unbuildable:
        return "skip", f"{line} ({platform}) is on the release's own served 'unbuildable' list — by design, not a finding"
    if line not in registry.versions():
        return "skip", f"{line} is not in this registry — fetch it to run this case"
    try:
        library = registry.for_version(line)
    except chtypes_mod.RegistryError as exc:
        return (
            "problem",
            f"ClickHouse {line} would not load, and {(platform, line)} is not on the served "
            f"'unbuildable' list, so this is unexpected: {exc}",
        )
    try:
        actual = run_check(library, chk, chtypes_mod)
    except chtypes_mod.SchemaError as exc:
        if exc.code == 115:
            return (
                "problem",
                f"ClickHouse {line}: compiling with settings {chk.get('settings') or {}} raised code 115 "
                f"(unknown setting) — this artifact does not recognize a setting the check names; the "
                f"check's line list is likely wrong, not the library ({exc.msg})",
            )
        return (
            "problem",
            f"ClickHouse {line}: compiling {chk['schema']!r} was refused (code {exc.code}: {exc.msg}) — "
            f"the check could not run",
        )
    mismatch = compare(chk["expect"], actual)
    if mismatch is None:
        return "ok", f"ClickHouse {line}: {mismatch or 'matches'}"
    return (
        "problem",
        f"the page and the artifacts disagree — update docs/limitations.md: check {chk['id']!r} on "
        f"ClickHouse {line} — {mismatch}. Either the entry describes behavior the artifacts no longer "
        f"have (the divergence was fixed; remove or narrow the entry), or the artifacts now diverge "
        f"differently than described (the entry is wrong; the library did not necessarily regress).",
    )


def reality_problems(
    entries: list[dict[str, Any]],
    registry_dir: str,
    chtypes_mod: Any,
    artifacts_url: str,
) -> tuple[list[str], int, list[str]]:
    """Drive every check in every entry. Returns (problems, checked_count, skips)."""
    registry = chtypes_mod.Registry(registry_dir)
    platform = chtypes_mod.host_platform()
    unbuildable = load_unbuildable(artifacts_url)
    # Report only the pairs that can actually exclude something HERE. The
    # served list covers every platform, so printing it whole made a
    # linux-amd64 runner announce a darwin-arm64 exclusion and read as though
    # a case had been skipped when none had (#182's lesson, one repo over: a
    # report that names the wrong cause is worse than no report).
    mine = sorted(line for plat, line in unbuildable if plat == platform)
    if mine:
        note(f"excluded by design on {platform}, per the release's own index: {', '.join(mine)}")
    else:
        note(f"no line is on the release's served 'unbuildable' list for {platform} ({len(unbuildable)} pair(s) listed, none here)")

    problems: list[str] = []
    skips: list[str] = []
    checked = 0
    for entry in entries:
        for chk in entry["checks"]:
            for line in chk["lines"]:
                status, msg = resolve_case(chk, line, registry, platform, unbuildable, chtypes_mod)
                label = f"{entry['id']}/{chk['id']}@{line}"
                if status == "skip":
                    skips.append(f"{label}: {msg}")
                elif status == "problem":
                    problems.append(msg)
                    checked += 1
                else:
                    checked += 1
    return problems, checked, skips


def zero_run_problems(reality_probs: list[str], checked: int, data_rel: str, registry_dir: str) -> list[str]:
    """The house-rule zero-run refusal — scripts/check-suite.sh and
    scripts/check-standalone.sh both refuse a run that checked nothing, and
    the reality check is no exception: entries were named but not one of
    them could actually be driven (no registry, or every line they name
    absent from it), and reporting that as a pass would be the exact
    failure this script exists to prevent one level up. Pure, like
    compare(), so --selftest proves it fires without a registry or the
    network."""
    if checked != 0:
        return reality_probs
    return [
        *reality_probs,
        f"no check ran at all — every line named in {data_rel} was either absent from "
        f"{registry_dir} or on the served 'unbuildable' list. That proves nothing, so this is a "
        "failure, not a pass.",
    ]


# =================================================================== main ===


def load_data(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        die(f"could not read {path}: {exc}")
    except json.JSONDecodeError as exc:
        die(f"{path} is not valid JSON: {exc}")
    if doc.get("schema") != 1:
        die(f"{path}: schema {doc.get('schema')!r} is not 1 — this script reads schema 1")
    entries = doc.get("entries")
    if not isinstance(entries, list):
        die(f"{path}: 'entries' must be a list (an empty list means an intentionally empty register) — got {entries!r}")
    for e in entries:
        for key in ("id", "heading", "checks"):
            if key not in e:
                die(f"{path}: an entry is missing {key!r}: {e}")
        if not e["checks"]:
            die(f"{path}: entry {e['id']!r} names no checks")
        for chk in e["checks"]:
            for key in ("id", "lines", "schema", "format", "payload", "expect"):
                if key not in chk:
                    die(f"{path}: entry {e['id']!r} check is missing {key!r}: {chk}")
            if chk["format"] not in _FORMAT_NAMES:
                die(f"{path}: entry {e['id']!r} check {chk['id']!r} names an unknown format {chk['format']!r}")
            if "outcome" not in chk["expect"]:
                die(f"{path}: entry {e['id']!r} check {chk['id']!r} 'expect' names no outcome")
    return doc


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def print_lines(data: dict[str, Any]) -> int:
    lines: set[str] = set()
    for e in data["entries"]:
        for chk in e["checks"]:
            lines.update(chk["lines"])
    for line in sorted(lines, key=lambda s: [int(p) for p in s.split(".")]):
        print(line)
    return 0


def run(registry_dir: str | None, data_path: Path, doc_path: Path) -> int:
    data = load_data(data_path)
    doc_text = doc_path.read_text(encoding="utf-8")
    headings = parse_divergence_headings(doc_text)
    entries = data["entries"]

    state = classify_register(entries, headings, has_divergences_section(doc_text))
    if state == "missing":
        die(
            f"{rel(doc_path)} has no {SECTION_HEADING!r} section at all, and {rel(data_path)} names no "
            "entries either — an intentionally empty register still needs the section heading present; "
            "did it move or get renamed?"
        )
    if state == "empty":
        print(
            f"check-divergences: ok — the Known-divergences register is empty (0 entries in {rel(data_path)}, "
            f"0 headings under {SECTION_HEADING!r} in {rel(doc_path)}); that is a valid end state "
            "(docs/limitations.md's own words: \"An entry disappears when an artifact stops diverging\"), "
            "not a skipped run."
        )
        return 0

    if not headings:
        die(
            f"{rel(doc_path)} has no ### headings under {SECTION_HEADING!r}, but {rel(data_path)} names "
            f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'} — did the section move or get renamed?"
        )

    problems = coverage_problems(headings, entries, rel(data_path), rel(doc_path))
    say(f"coverage: {len(headings)} heading(s) in {rel(doc_path)}, {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} in {rel(data_path)}")
    for p in problems:
        print(p, file=sys.stderr)
    if not problems:
        note("coverage ok — every heading has exactly one entry, and every entry names a real heading")

    if not registry_dir:
        die(
            "no registry named (--registry / $CHTYPES_REGISTRY) — the reality check needs loaded "
            "artifacts and cannot run without one; refusing to report a pass that checked nothing"
        )
    if not os.path.isdir(registry_dir) or not any(Path(registry_dir).glob("*/manifest.json")):
        die(
            f"{registry_dir} holds no <line>/manifest.json — nothing is fetched there. "
            f"scripts/fetch.sh <line> --dest {registry_dir} installs one; this script never fetches for itself"
        )

    import chtypes  # noqa: PLC0415 (sys.path is prepared at module load, above)

    artifacts_url = os.environ.get("CHTYPES_ARTIFACTS_URL", "https://artifacts.wavehouse.dev")
    say(f"reality: driving every check in {rel(data_path)} against {registry_dir}")
    reality_probs, checked, skips = reality_problems(entries, registry_dir, chtypes, artifacts_url)
    for s in skips:
        note(f"skip: {s}")
    reality_probs = zero_run_problems(reality_probs, checked, rel(data_path), registry_dir)
    if checked:
        note(f"{checked} case(s) checked, {len(reality_probs)} problem(s)")
    for p in reality_probs:
        print(p, file=sys.stderr)
    problems += reality_probs

    if problems:
        print(
            f"\ncheck-divergences: {len(problems)} problem(s). "
            "The page and the artifacts disagree — update docs/limitations.md "
            "(or docs/divergences.json, for a coverage problem).",
            file=sys.stderr,
        )
        return 1
    print("check-divergences: ok — every Known-divergences entry has a heading, every heading has an entry, "
          "and every checked case still answers the way the page says it does")
    return 0


# ================================================================ selftest ===


class _FakeSchema:
    def __init__(self, outcome: str, err_code: int = 0, err_msg: str = ""):
        self._outcome, self._err_code, self._err_msg = outcome, err_code, err_msg

    def __enter__(self) -> "_FakeSchema":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def rows(self, fmt: object, body: bytes, settings: object) -> Any:
        class _BR:
            pass

        br = _BR()
        br.outcome, br.err_code, br.err_msg = self._outcome, self._err_code, self._err_msg
        return br


class _FakeLibrary:
    """A stand-in for chtypes.Library: compile_ddl returns a canned result
    (or raises a canned chtypes.SchemaError) without ever touching a real
    artifact — the boundary --selftest replaces so resolve_case's own
    decision logic is proven with no registry and no network."""

    def __init__(self, chtypes_mod: Any, outcome: str = "accepted", err_code: int = 0, err_msg: str = "", raise_schema_error: tuple[int, str] | None = None):
        self._chtypes = chtypes_mod
        self._outcome, self._err_code, self._err_msg = outcome, err_code, err_msg
        self._raise = raise_schema_error

    def compile_ddl(self, ddl: str, *, settings: object = None, mode: int = 0) -> _FakeSchema:
        if self._raise is not None:
            code, msg = self._raise
            raise self._chtypes.SchemaError(code, msg)
        return _FakeSchema(self._outcome, self._err_code, self._err_msg)


class _FakeRegistry:
    def __init__(self, chtypes_mod: Any, lines: dict[str, _FakeLibrary]):
        self._chtypes = chtypes_mod
        self._lines = lines

    def versions(self) -> tuple[str, ...]:
        return tuple(self._lines)

    def for_version(self, line: str) -> _FakeLibrary:
        if line not in self._lines:
            raise self._chtypes.RegistryError(f"no such line: {line}")
        return self._lines[line]


def selftest() -> int:
    import chtypes  # noqa: PLC0415

    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    # ---- coverage: matched heading/entry -> no problems.
    entries = [{"id": "a", "heading": "H1", "checks": [{}]}]
    check(coverage_problems(["H1"], entries, "data.json", "doc.md") == [], "matched heading/entry reported a problem")

    # ---- coverage: a heading with no entry must fire, by name.
    probs = coverage_problems(["H1", "H2"], entries, "data.json", "doc.md")
    check(len(probs) == 1 and "H2" in probs[0], "a heading with no data entry was not caught")

    # ---- coverage: an entry with no heading must fire, by name.
    probs = coverage_problems(["H1"], entries + [{"id": "b", "heading": "H3", "checks": [{}]}], "data.json", "doc.md")
    check(len(probs) == 1 and "H3" in probs[0] and "b" in probs[0], "an entry with no matching heading was not caught")

    # ---- coverage: two entries claiming the same heading must fire.
    probs = coverage_problems(["H1"], entries + [{"id": "c", "heading": "H1", "checks": [{}]}], "data.json", "doc.md")
    check(any("matches 2 entries" in p for p in probs), "two entries for one heading were not caught")

    # ---- parse_divergence_headings: only headings inside the section count,
    # and a later top-level heading ends it.
    doc = (
        "# Known limitations\n\n## Some other section\n### not a divergence\n\n"
        "## Known divergences\n\n### First one\n\nbody\n\n### Second one\n\nbody\n\n"
        "## Pre-1.0\n\n### also not a divergence\n"
    )
    got = parse_divergence_headings(doc)
    check(got == ["First one", "Second one"], f"heading scoping is wrong: {got}")

    # ---- compare(): exact match -> no mismatch.
    expect = {"outcome": "rejected", "err_code": 48, "err_msg": "boom"}
    actual = {"outcome": "rejected", "err_code": 48, "err_msg": "boom"}
    check(compare(expect, actual) is None, "an exact match was reported as a mismatch")

    # ---- compare(): outcome mismatch is caught — THE central claim this
    # whole script exists to check. A page saying "accepted" and an artifact
    # that now answers "rejected" (the producer fixed it) must fire.
    m = compare({"outcome": "accepted"}, {"outcome": "rejected", "err_code": 27, "err_msg": ""})
    check(m is not None and "accepted" in m and "rejected" in m, "an outcome mismatch (the central claim) was not caught")

    # ---- compare(): err_code mismatch is caught, only when the page states one.
    m = compare({"outcome": "rejected", "err_code": 48}, {"outcome": "rejected", "err_code": 27, "err_msg": ""})
    check(m is not None and "48" in m and "27" in m, "an err_code mismatch was not caught")
    check(compare({"outcome": "rejected"}, {"outcome": "rejected", "err_code": 999, "err_msg": "x"}) is None,
          "err_code was checked although the entry did not state one")

    # ---- compare(): err_msg mismatch is caught, only when the page quotes one.
    m = compare({"outcome": "rejected", "err_msg": "want"}, {"outcome": "rejected", "err_code": 0, "err_msg": "got"})
    check(m is not None and "want" in m and "got" in m, "an err_msg mismatch was not caught")

    # ---- resolve_case: the driver's own decision, end to end, against fakes
    # — no registry, no network, and no re-implemented copy of the logic
    # the real run uses.
    chk_ok = {"id": "x", "lines": ["25.8"], "schema": "x UInt8", "format": "JSONEachRow", "payload": "{}",
              "settings": {}, "expect": {"outcome": "accepted"}}
    reg = _FakeRegistry(chtypes, {"25.8": _FakeLibrary(chtypes, outcome="accepted")})
    status, msg = resolve_case(chk_ok, "25.8", reg, "linux-amd64", set(), chtypes)
    check(status == "ok", f"a case matching its expectation was not reported ok: {status} {msg}")

    # THE central negative: an entry whose claim no longer holds (the
    # producer fixed the divergence) must be reported as a problem, in the
    # required words, naming both directions.
    reg_wrong = _FakeRegistry(chtypes, {"25.8": _FakeLibrary(chtypes, outcome="rejected", err_code=27)})
    status, msg = resolve_case(chk_ok, "25.8", reg_wrong, "linux-amd64", set(), chtypes)
    check(status == "problem", "a claim that no longer holds was not reported as a problem")
    check("the page and the artifacts disagree" in msg, "the required failure phrase is missing")
    check("docs/limitations.md" in msg, "the failure message does not point at docs/limitations.md")
    check("no longer have" in msg or "diverge differently" in msg, "the failure message does not say which direction")

    # A line absent from the registry: skip, loudly, never a problem.
    status, msg = resolve_case(chk_ok, "99.9", reg, "linux-amd64", set(), chtypes)
    check(status == "skip" and "99.9" in msg, "a line missing from the registry was not skipped by name")

    # A line on the served unbuildable list: skip, even though it is not in
    # the registry either — the unbuildable check runs first, so the
    # message names the real reason instead of a generic "not fetched".
    status, msg = resolve_case(chk_ok, "24.8", reg, "darwin-arm64", {("darwin-arm64", "24.8")}, chtypes)
    check(status == "skip" and "unbuildable" in msg, "an unbuildable (platform, line) pair was not skipped by name")

    # A line that fails to load and is NOT on the unbuildable list: a real
    # problem, not a silent skip — a load failure this checker cannot
    # explain must not disappear.
    status, msg = resolve_case(chk_ok, "99.9", _FakeRegistry(chtypes, {}), "linux-amd64", set(), chtypes)
    check(status == "skip", "an unfetched line was, correctly, a skip (versions() check runs first)")

    class _AlwaysMissing(_FakeRegistry):
        def versions(self) -> tuple[str, ...]:
            return ("99.9",)

    status, msg = resolve_case(chk_ok, "99.9", _AlwaysMissing(chtypes, {}), "linux-amd64", set(), chtypes)
    check(status == "problem" and "unexpected" in msg, "an inexplicable load failure was not reported as a problem")

    # code 115 (unknown setting) is reported distinctly, never conflated
    # with a genuine page/artifact disagreement.
    chk_gate = {**chk_ok, "settings": {"nope_not_a_setting": "1"}}
    reg_115 = _FakeRegistry(chtypes, {"25.8": _FakeLibrary(chtypes, raise_schema_error=(115, "Setting nope_not_a_setting is neither..."))})
    status, msg = resolve_case(chk_gate, "25.8", reg_115, "linux-amd64", set(), chtypes)
    check(status == "problem" and "code 115" in msg and "the page and the artifacts disagree" not in msg,
          "a code-115 unknown-setting failure was not distinguished from a real disagreement")

    # ---- classify_register: the three states, pure — chtypes#185's fix.
    check(classify_register([], [], True) == "empty", "an empty register with its section present was not classified 'empty'")
    check(classify_register([], [], False) == "missing", "an empty register with no section heading was not classified 'missing'")
    check(classify_register([], ["H1"], True) == "active", "headings with no entries were classified as empty, not active")
    check(classify_register([{"heading": "H1"}], [], True) == "active", "entries with no headings were classified as empty, not active")
    check(classify_register([{"heading": "H1"}], ["H1"], True) == "active", "a normal populated register was not classified 'active'")

    # ---- has_divergences_section: the heading line itself, independent of
    # whether anything is inside it.
    check(has_divergences_section("# X\n\n## Known divergences\n\n## Pre-1.0\n"), "an empty but present section was not detected")
    check(not has_divergences_section("# X\n\n## Known Divergences (renamed)\n\n## Pre-1.0\n"), "a renamed section heading was reported as present")

    # ---- zero_run_problems: the deeper form of "entries exist but nothing
    # could be driven" — a registry IS present but every line it names was
    # absent (checked stays 0). Pure, so this is proven without a real
    # registry or the network.
    probs = zero_run_problems([], 0, "docs/divergences.json", "/some/registry")
    check(len(probs) == 1 and "no check ran at all" in probs[0], "checked == 0 did not produce the zero-run refusal message")
    check(
        zero_run_problems(["existing problem"], 3, "docs/divergences.json", "/some/registry") == ["existing problem"],
        "checked > 0 wrongly appended the zero-run refusal message",
    )

    # ---- run(), end to end, offline (no registry, no network): the two
    # cases run() itself must now distinguish.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # THE central positive: zero entries and zero headings, with the
        # section heading present, is a valid end state and must PASS —
        # this is the exact shape docs/limitations.md's own text describes
        # ("An entry disappears when an artifact stops diverging") and the
        # one the old "names no entries" die() could not survive.
        doc_empty = tmp_path / "limitations-empty.md"
        doc_empty.write_text(
            "# Known limitations\n\n## Known divergences\n\nNothing currently diverges.\n\n## Pre-1.0\n\nmore text\n",
            encoding="utf-8",
        )
        data_empty = tmp_path / "divergences-empty.json"
        data_empty.write_text(json.dumps({"schema": 1, "entries": []}), encoding="utf-8")
        rc = run(None, data_empty, doc_empty)
        check(rc == 0, f"an intentionally empty register (0 entries, 0 headings, section present) did not pass: rc={rc}")

        # The same zero/zero shape, but the section heading itself is gone
        # — must NOT be swallowed by the new empty-register pass path.
        doc_missing = tmp_path / "limitations-missing.md"
        doc_missing.write_text(
            "# Known limitations\n\n## Known Divergences (renamed)\n\nNothing currently diverges.\n\n## Pre-1.0\n",
            encoding="utf-8",
        )
        try:
            run(None, data_empty, doc_missing)
            check(False, "a renamed/missing section heading with zero entries was not caught — it must not silently pass as an empty register")
        except SystemExit as exc:
            check(exc.code == 1, f"a renamed/missing section heading did not exit 1: {exc.code}")

        # THE central negative: entries exist but nothing could be driven
        # (no registry named) — the existing zero-run refusal must still
        # fire, unweakened, now that load_data() no longer dies on an empty
        # list by itself.
        doc_active = tmp_path / "limitations-active.md"
        doc_active.write_text(
            "# Known limitations\n\n## Known divergences\n\n### A fake divergence\n\nbody\n\n## Pre-1.0\n",
            encoding="utf-8",
        )
        data_active = tmp_path / "divergences-active.json"
        data_active.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "entries": [
                        {
                            "id": "fake",
                            "heading": "A fake divergence",
                            "checks": [
                                {
                                    "id": "c1",
                                    "lines": ["99.9"],
                                    "schema": "x UInt8",
                                    "format": "JSONEachRow",
                                    "payload": "{}",
                                    "settings": {},
                                    "expect": {"outcome": "accepted"},
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        try:
            run(None, data_active, doc_active)
            check(False, "entries with no registry named did not refuse — the zero-run house rule was weakened")
        except SystemExit as exc:
            check(exc.code == 1, f"entries with no registry named did not exit 1: {exc.code}")

    if failures:
        for f in failures:
            print(f"SELFTEST FAILED: {f}", file=sys.stderr)
        return 1
    print(
        "check-divergences: selftest ok — coverage catches a heading with no entry, an entry with no "
        "heading, and a duplicate; compare() catches an outcome/err_code/err_msg mismatch and passes an "
        "exact match; resolve_case reports ok/skip/problem correctly for a match, a claim that no longer "
        "holds, an unfetched line, an unbuildable (platform, line) pair, an inexplicable load failure, "
        "and a code-115 unknown setting; classify_register and has_divergences_section tell an "
        "intentionally empty register apart from a renamed/missing section; zero_run_problems and run() "
        "itself both still refuse when entries exist but nothing could be driven, and run() passes end "
        "to end, offline, when the register is genuinely empty"
    )
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(add_help=True)
    p.add_argument("--registry", default=os.environ.get("CHTYPES_REGISTRY"))
    p.add_argument("--data", type=Path, default=DEFAULT_DATA)
    p.add_argument("--doc", type=Path, default=DEFAULT_DOC)
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--print-lines", action="store_true")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.print_lines:
        return print_lines(load_data(args.data))
    return run(args.registry, args.data, args.doc)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
