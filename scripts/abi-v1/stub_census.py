#!/usr/bin/env python3
"""stub_census.py: the verdict for a stub-backed test run (public issue #463).

The v1-abi-conformance legs run each binding's whole stub-backed public suite
with CHTYPES_ABI<N>_STUBS set. A test that SKIPS because the stubs are missing
while the stubs ARE set is a test that never ran, and a green exit code would
hide it. This reads the runner's own machine-readable result, never its exit
code alone, and refuses:

  * a run in which nothing passed (a zero-run);
  * any skip whose reason names CHTYPES_ABI<N>_STUBS (a stub-skip with the stubs set);
  * in the per-file formats (junit, vitest, cargo), a test FILE (a cargo test
    target) in which tests exist but none passed (a whole file skipped behind
    a stub gate);
  * a required test that did not pass, when --require is given;
  * a go package that failed outside any test (a race the detector caught
    after its tests, a panic in TestMain, a build failure);
  * a cargo test target whose own result lines do not add up to libtest's own
    summary of that target (its output was interleaved or cut, so it cannot
    be counted), or that printed no summary at all (it crashed);
  * any difference from the PINNED list (--pinned, public issue #727): a test
    the list names that did not pass on this OS (a dropped test: skipped,
    ignored, failed or gone), and a test that passed but the list does not
    name (an added or renamed test: the census prints the line to add).

The pinned list is the expectation, checked into the repository per ABI major
and binding (tests/fixtures/abi-v2/stub-census/<binding>.txt): one test
identity per line, exactly as the census prints it; blank lines and lines
starting with "#" are ignored; a line "[linux] <identity>" or
"[darwin] <identity>" is required on that OS only. A count alone cannot fail
on a dropped test, and a count that drifts cannot tell a dropped test from
noise; a list names what is missing. Adding a test fails the run until its
line is added, deliberately: an unlisted test is one whose later disappearance
nobody would see. The count is a multiset, so two tests that share one
identity are each counted.

Every count is read from a STRUCTURED source: go test -json, pytest's junit
XML, vitest's JSON reporter, and for cargo, libtest's own lines with each
test's output CAPTURED (the conformance leg passes --show-output, never
--nocapture: under --nocapture a test's own output, printed from another test
thread, split libtest's `test <name> ... ok` lines and the count drifted
between identical runs) and reconciled, per target, against libtest's own
`test result:` summary and its `running N tests` line. A target that does not
reconcile is refused, never miscounted.

Every skip for another reason is reported BY NAME with its reason, the way
scripts/check-standalone.sh reports its own.

Every failure carries its OWN TEXT into the job log, because the log is all
there is (no Actions artifacts): under each FAILED name, in a collapsible
::group::, the failing test's own output from the runner's result (go: its
`output` events, which hold an assertion's message and any `WARNING: DATA
RACE` report; junit: the failure's message and text; vitest: its
failureMessages; cargo: its panic and its captured stdout, from libtest's
`failures:` section), and for a go package that failed outside any test, the
package's own output. Each is capped at FAIL_OUTPUT_CAP lines, head and tail
kept, and says "truncated" when it cuts. Every quoted line is prefixed, so a
line of test output can never be read as a workflow command (an
`::endgroup::` or an `::error::`).

    scripts/abi-v1/stub_census.py go     <go test -json log>      <rc> --binding go [--require REGEX]...
    scripts/abi-v1/stub_census.py junit  <pytest --junitxml file> <rc> --binding python [--require REGEX]...
    scripts/abi-v1/stub_census.py vitest <vitest JSON file>       <rc> --binding ts
    scripts/abi-v1/stub_census.py cargo  <directory>              <rc> --binding rust
    scripts/abi-v1/stub_census.py --selftest

--binding NAME reads that binding's pinned list at the major
spec/binding-majors.json gives it (scripts/abi-v1/majors.py, the derivation
the conformance scripts use); --pinned FILE names a list directly instead.

For cargo the directory holds, per test target, `<target>.log` (that target's
own `cargo test ... -- --show-output` output) and `<target>.rc` (its exit
code); the target is `lib` for --lib. A test's identity is
`<target> <test name>`; a target with no libtest harness (harness = false)
is one identity, its own name, which passed when it exited 0.

The identities: go `<package> <test>`, junit `<classname>::<name>`, vitest
`<file name> :: <full name>`, cargo `<target> <test name>`.

The conformance scripts call it only when the stubs are set; without them they
keep their old behavior (skip loudly by name, exit 0).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import platform
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter

STUB_ENV = "CHTYPES_ABI1_STUBS"
# Each ABI major's stub directory variable (CHTYPES_ABI1_STUBS,
# CHTYPES_ABI2_STUBS, ...): a binding reads the one of the major it speaks
# (spec/binding-majors.json), and a skip naming any of them, with the stubs
# set, is a stub-skip.
STUB_ENV_RE = re.compile(r"CHTYPES_ABI\d+_STUBS")

# The most lines of one failure's own output the job log carries. A race
# report is a few dozen lines per race and an assertion one or two; past the
# cap the head (where a race report starts) and the tail (where a test's last
# words are) are kept, and the cut is said.
FAIL_OUTPUT_CAP = 400
FAIL_OUTPUT_TAIL = 100

# go test's own framing of a package's result: a package-level failure whose
# output is only these lines carries nothing a test's own output does not.
GO_PKG_FRAMING = re.compile(r"^(PASS|FAIL|ok\s.*|FAIL\s.*|exit status \d+|coverage: .*|testing: warning: no tests to run)$")

# The OS tags a pinned line may carry, as platform.system() names them.
KNOWN_OS = ("linux", "darwin")
# The most identities one pinned-list difference prints by name; past it, the
# rest are counted.
PINNED_SHOW_CAP = 200

# libtest's own lines (cargo test, stable output; the conformance leg passes
# --show-output, never --nocapture).
LIBTEST_RUNNING = re.compile(r"^running (\d+) tests?$")
LIBTEST_RESULT = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)(?:, (.*))?$")
LIBTEST_SUMMARY = re.compile(r"^test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored; ")
# libtest's end-of-run sections follow the per-test result lines.
LIBTEST_SECTION = re.compile(r"^(successes|failures):$")


class Run:
    """One run, reduced to what the verdict reads."""

    def __init__(self):
        self.passed: list[str] = []
        # Parallel to passed: each passed test's identity, as a pinned list
        # names it (go qualifies the test with its package).
        self.ids: list[str] = []
        self.failed: list[str] = []
        self.failed_ids: list[str] = []  # parallel to failed: each failure's identity
        self.failed_output: list[list[str]] = []  # parallel to failed: each failure's own text
        self.skipped: list[tuple[str, str]] = []  # (name, reason)
        self.by_file: dict[str, list[int]] = {}  # file -> [passed, total]
        self.notes: list[str] = []
        # The runner's own lines no test owns (go: the non-JSON lines, such as
        # a build error; cargo: the whole output), quoted when the runner
        # exits nonzero with no failing test to show.
        self.raw: list[str] = []
        # go: a package that failed with no failing test of its own, and its
        # own (package-level) output.
        self.pkg_failed: list[tuple[str, list[str]]] = []
        # go: a package with a failing test whose package-level output holds
        # more than go's own framing (a race reported between tests).
        self.pkg_output: list[tuple[str, list[str]]] = []
        # cargo: a target whose own result lines do not add up to libtest's
        # own summary, or that printed none.
        self.unreconciled: list[str] = []

    def ok(self, name, ident=None):
        self.passed.append(name)
        self.ids.append(name if ident is None else ident)

    def file(self, f, passed):
        c = self.by_file.setdefault(f, [0, 0])
        c[1] += 1
        if passed:
            c[0] += 1

    def fail(self, name, text, ident=None):
        self.failed.append(name)
        self.failed_ids.append(name if ident is None else ident)
        self.failed_output.append(list(text))


def _split(text):
    """A failure message or text as lines, with no trailing blank line."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def parse_go(path):
    run = Run()
    # Keyed by (package, test): two packages may hold a test of one name.
    output: dict[tuple[str, str], list[str]] = {}
    pkg_out: dict[str, list[str]] = {}
    pkg_fail: list[tuple[str, str]] = []  # (package, the build that failed, if any)
    pkg_with_failed_test: set[str] = set()
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw.startswith("{"):
                if raw:
                    run.notes.append(raw[:160])
                    run.raw.append(raw)
                continue
            try:
                ev = json.loads(raw)
            except ValueError:
                run.notes.append(raw[:160])
                run.raw.append(raw)
                continue
            t, a, p = ev.get("Test"), ev.get("Action"), ev.get("Package", "")
            if not t:
                # A package-level event: its output (a race caught after the
                # tests, a panic outside any test), a build's output (go 1.24+
                # reports it as build-output, keyed by ImportPath), and the
                # package's own result.
                if a == "output":
                    pkg_out.setdefault(p, []).append(ev.get("Output", "").rstrip("\n"))
                elif a == "build-output":
                    ip = ev.get("ImportPath", p)
                    pkg_out.setdefault(ip, []).append(ev.get("Output", "").rstrip("\n"))
                elif a == "fail" and p:
                    pkg_fail.append((p, ev.get("FailedBuild") or ""))
                continue
            key = (p, t)
            if a == "output":
                output.setdefault(key, []).append(ev.get("Output", "").rstrip("\n"))
            elif a == "pass":
                run.ok(t, p + " " + t)
            elif a == "fail":
                run.fail(t, output.get(key, []), p + " " + t)
                pkg_with_failed_test.add(p)
            elif a == "skip":
                lines = [
                    s.strip()
                    for s in output.get(key, [])
                    if s.strip() and not s.strip().startswith(("--- SKIP:", "=== "))
                ]
                run.skipped.append((p + " " + t, lines[-1] if lines else ""))
    for p, build in pkg_fail:
        lines = (pkg_out.get(build, []) if build else []) + pkg_out.get(p, [])
        if p not in pkg_with_failed_test:
            run.pkg_failed.append((p, lines))
        elif any(x.strip() and not GO_PKG_FRAMING.match(x.strip()) for x in lines):
            run.pkg_output.append((p, lines))
    return run


def parse_junit(path):
    run = Run()
    root = ET.parse(path).getroot()
    for case in root.iter("testcase"):
        name = case.get("classname", "") + "::" + case.get("name", "")
        fname = case.get("classname", "")
        sk = case.find("skipped")
        bad = [e for e in (case.find("failure"), case.find("error")) if e is not None]
        if bad:
            text = []
            for e in bad:
                text += _split(e.get("message", ""))
                text += _split(e.text)
            for tag in ("system-out", "system-err"):
                e = case.find(tag)
                if e is not None and (e.text or "").strip():
                    text += ["--- captured %s ---" % tag] + _split(e.text)
            run.fail(name, text)
            run.file(fname, False)
        elif sk is not None:
            run.skipped.append((name, (sk.get("message") or sk.text or "").strip()))
            run.file(fname, False)
        else:
            run.ok(name)
            run.file(fname, True)
    return run


def parse_vitest(path):
    run = Run()
    doc = json.load(open(path, encoding="utf-8"))
    for tr in doc.get("testResults", []):
        fname = os.path.basename(tr.get("name", ""))
        for a in tr.get("assertionResults", []):
            name = fname + " :: " + a.get("fullName", a.get("title", ""))
            st = a.get("status")
            if st == "passed":
                run.ok(name)
                run.file(fname, True)
            elif st == "failed":
                text = []
                for m in a.get("failureMessages") or []:
                    text += _split(str(m))
                run.fail(name, text)
                run.file(fname, False)
            else:
                # vitest's JSON carries no skip reason; the title is the best
                # name there is (an explicit it.skip title states its own reason).
                run.skipped.append((name, a.get("title", "")))
                run.file(fname, False)
    return run


def _libtest_text(lines):
    """Each test's own text, by name: libtest's `---- <name> stdout ----`
    sections (its captured output and its panic), and any `thread '<name>'
    panicked at` block printed outside one, up to the next blank line."""
    text: dict[str, list[str]] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"---- (\S+) stdout ----$", line)
        p = re.match(r"thread '([^']+)' panicked at", line)
        if m or p:
            name = (m or p).group(1)
            block = [] if m else [line]
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if re.match(r"---- \S+ stdout ----$|(successes|failures):$|test result: |test \S+ \.\.\. ", nxt):
                    break
                if p and not nxt.strip():
                    break
                block.append(nxt)
                i += 1
            text.setdefault(name, []).extend(_split("\n".join(block)))
            continue
        i += 1
    return text


def _cargo_target(run, target, lines, rc):
    """One cargo test target's own output, into run."""
    for line in lines:
        # The rust suites skip by early return with a loud line, not by
        # #[ignore]; the line itself is the evidence (--show-output prints a
        # passing test's captured lines, so it reaches the log).
        if re.search(r"\bSKIP(PED)?\b", line):
            run.skipped.append((target, line.strip()))
    if rc != 0:
        run.raw += ["---- the %s target's own output (exit %s) ----" % (target, rc)] + lines
    start = next((i for i, line in enumerate(lines) if LIBTEST_RUNNING.match(line)), None)
    if start is None:
        # No libtest harness (harness = false, or a target that never got
        # as far as running): the whole target is one test, its exit its result.
        if rc == 0:
            run.ok(target)
            run.file(target, True)
        else:
            run.fail(target, lines)
            run.file(target, False)
        return
    want = int(LIBTEST_RUNNING.match(lines[start]).group(1))
    # The per-test result lines run from libtest's `running N tests` to its
    # first end-of-run section; a test's own captured lines are printed only
    # inside those sections, so none can be read as a result.
    got = Counter()
    failed = []
    i = start + 1
    while i < len(lines) and not LIBTEST_SECTION.match(lines[i]) and not LIBTEST_SUMMARY.match(lines[i]):
        m = LIBTEST_RESULT.match(lines[i])
        i += 1
        if not m:
            continue
        name, status, reason = m.groups()
        ident = target + " " + name
        got[status] += 1
        if status == "ok":
            run.ok(ident)
            run.file(target, True)
        elif status == "FAILED":
            failed.append((name, ident))
            run.file(target, False)
        else:
            run.skipped.append((ident, "ignored" + (": " + reason if reason else "")))
            run.file(target, False)
    text = _libtest_text(lines)
    for name, ident in failed:
        run.fail(ident, text.get(name, []), ident)
    # libtest prints its own summary last; a test's own lines come before it.
    summary = [m for m in map(LIBTEST_SUMMARY.match, lines) if m]
    if not summary:
        run.unreconciled.append(
            "%s: %d of the %d test(s) libtest started reported a result, and it printed no `test result:` line "
            "(exit %s): it crashed or was killed before libtest summed it up" % (target, sum(got.values()), want, rc)
        )
        return
    passed, nfailed, ignored = (int(x) for x in summary[-1].groups())
    if (got["ok"], got["FAILED"], got["ignored"]) != (passed, nfailed, ignored) or passed + nfailed + ignored != want:
        run.unreconciled.append(
            "%s: libtest's own summary says %d passed, %d failed, %d ignored of the %d it ran, but its result lines "
            "say %d, %d, %d: its output was interleaved or cut, so this target is not counted"
            % (target, passed, nfailed, ignored, want, got["ok"], got["FAILED"], got["ignored"])
        )


def parse_cargo(path):
    """A directory of `<target>.log` and `<target>.rc`, one pair per cargo
    test target (scripts/abi-v1/conformance/rust.sh writes it)."""
    run = Run()
    targets = sorted(f[: -len(".rc")] for f in os.listdir(path) if f.endswith(".rc"))
    for target in targets:
        try:
            with open(os.path.join(path, target + ".rc"), encoding="utf-8") as f:
                rc = int(f.read().strip())
        except (OSError, ValueError):
            rc = "unreadable"
        try:
            with open(os.path.join(path, target + ".log"), encoding="utf-8", errors="replace") as f:
                lines = [x.rstrip("\r\n") for x in f]
        except OSError:
            lines = []
        _cargo_target(run, target, lines, rc)
    return run


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _majors():
    """scripts/abi-v1/majors.py: the binding-to-major map the conformance
    scripts read, so a binding's pinned list is found by the same derivation."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import majors

    return majors


def pinned_list(binding):
    """A binding's pinned list, repository-relative, at the ABI major
    spec/binding-majors.json gives it."""
    return "tests/fixtures/abi-v%d/stub-census/%s.txt" % (_majors().load()[binding], binding)


def host_os():
    return platform.system().lower()


class Pinned:
    """A pinned list (see the module docstring), read for one OS."""

    def __init__(self, path, os_name, shown=None):
        self.shown = shown or path
        self.os = os_name
        self.problems: list[str] = []
        self.order: list[str] = []  # every identity, tag aside, in the file's order
        untagged: Counter = Counter()
        tagged: dict[str, Counter] = {o: Counter() for o in KNOWN_OS}
        with open(path, encoding="utf-8") as f:
            for n, raw in enumerate(f, 1):
                line = raw.rstrip("\r\n")
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                m = re.match(r"\[([^\]]*)\] (.+)$", line)
                self.order.append(m.group(2) if m else line)
                if not m:
                    untagged[line] += 1
                elif m.group(1) in KNOWN_OS:
                    tagged[m.group(1)][m.group(2)] += 1
                else:
                    self.problems.append("%s:%d: unknown OS tag [%s] (known: %s)" % (self.shown, n, m.group(1), ", ".join(KNOWN_OS)))
        # What this OS must pass, and what any OS may (a test pinned for
        # another OS only is neither required nor unpinned here).
        self.required = untagged + tagged.get(os_name, Counter())
        self.allowed = Counter(untagged)
        for o in KNOWN_OS:
            for ident, k in (untagged + tagged[o]).items():
                self.allowed[ident] = max(self.allowed[ident], k)


def _pinned_lines(run, pinned):
    """(lines, problems): the run's identities against the pinned list."""
    out: list[str] = []
    problems = list(pinned.problems)
    got = Counter(run.ids)
    missing = pinned.required - got
    extra = got - pinned.allowed
    need = sum(pinned.required.values())
    out.append(
        "    PINNED %s: %d test(s) required on %s, %d of them passed; %d passed unpinned"
        % (pinned.shown, need, pinned.os, need - sum(missing.values()), sum(extra.values()))
    )
    skipped = dict(run.skipped)
    failed = set(run.failed_ids)
    if missing:
        out.append("    MISSING: %s pins these, and they did not pass on %s:" % (pinned.shown, pinned.os))
        for ident in sorted(missing)[:PINNED_SHOW_CAP]:
            if got[ident]:
                why = "%d of the %d it pins passed" % (got[ident], pinned.required[ident])
            elif ident in failed:
                why = "it failed"
            elif ident in skipped:
                why = "it skipped: " + skipped[ident]
            else:
                why = "it did not run: gone, renamed or never reported"
            out.append("      - %s  (%s)" % (ident, why))
        if len(missing) > PINNED_SHOW_CAP:
            out.append("      ... and %d more" % (len(missing) - PINNED_SHOW_CAP))
        problems.append("%d pinned test(s) did not pass on %s (named under MISSING): a dropped test is not a pass" % (sum(missing.values()), pinned.os))
    if extra:
        out.append(
            "    UNPINNED: these passed, and %s does not name them; add each line below to it "
            "(as \"[linux] <identity>\" or \"[darwin] <identity>\" if it runs on that OS only):" % pinned.shown
        )
        shown = 0
        for ident, k in sorted(extra.items()):
            for _ in range(k):
                if shown < PINNED_SHOW_CAP:
                    out.append("      + " + ident)
                shown += 1
        if shown > PINNED_SHOW_CAP:
            out.append("      ... and %d more" % (shown - PINNED_SHOW_CAP))
        problems.append(
            "%d test(s) passed that %s does not pin (named under UNPINNED): an added test fails the census until its line is added"
            % (sum(extra.values()), pinned.shown)
        )
    return out, problems


def quoted(what, lines):
    """One failure's own text as a collapsible group in the job log: at most
    FAIL_OUTPUT_CAP lines, the head and the tail kept, the cut said, and every
    line prefixed so none of it can be read as a workflow command."""
    n = len(lines)
    if n > FAIL_OUTPUT_CAP:
        head = FAIL_OUTPUT_CAP - FAIL_OUTPUT_TAIL
        shown = lines[:head] + [None] + lines[n - FAIL_OUTPUT_TAIL :]
        title = "%s: its own output, %d lines, truncated to the first %d and the last %d" % (what, n, head, FAIL_OUTPUT_TAIL)
    else:
        shown = lines
        title = "%s: its own output, %d line%s" % (what, n, "" if n == 1 else "s")
    out = ["::group::" + title]
    if not lines:
        out.append("      | (it printed nothing of its own)")
    for x in shown:
        if x is None:
            out.append("      | ... truncated: %d lines omitted here ..." % (n - FAIL_OUTPUT_CAP))
        else:
            out.append("      | " + x)
    out.append("::endgroup::")
    return out


def verdict(run, rc, require=(), pinned=None):
    """(exit_code, lines). Pure, so --selftest drives the real thing."""
    out = ["  stub census: %d passed, %d skipped, %d failed" % (len(run.passed), len(run.skipped), len(run.failed))]
    problems = []
    for u in run.unreconciled:
        out.append("    UNCOUNTED " + u)
        problems.append("not counted: " + u.split(":", 1)[0] + " (named under UNCOUNTED)")
    for name, reason in run.skipped:
        out.append("    SKIP %s: %s" % (name, reason))
        if STUB_ENV_RE.search(reason) or STUB_ENV_RE.search(name):
            problems.append("stub-skip with the stubs set: %s" % name)
    for i, name in enumerate(run.failed):
        out.append("    FAILED " + name)
        text = run.failed_output[i] if i < len(run.failed_output) else []
        out.extend(quoted("FAILED " + name, text))
    if run.failed:
        problems.append("%d test(s) failed" % len(run.failed))
    for pkg, text in run.pkg_failed:
        out.append("    FAILED package %s, outside any test" % pkg)
        out.extend(quoted("FAILED package " + pkg, text))
        problems.append("package %s failed outside any test" % pkg)
    for pkg, text in run.pkg_output:
        out.append("    package %s printed this outside its tests" % pkg)
        out.extend(quoted("package " + pkg, text))
    if not run.passed:
        problems.append("zero tests passed: the stub-backed suite asserted nothing")
    for f, (p, total) in sorted(run.by_file.items()):
        if total and p == 0:
            problems.append("no test in %s passed (%d skipped or failed): the whole file did not run" % (f, total))
    for pat in require:
        hits = [n for n in run.passed if re.fullmatch(pat, n)]
        out.append("    REQUIRED /%s/ RAN: %d passed" % (pat, len(hits)))
        for n in hits[:40]:
            out.append("      ok " + n)
        if not hits:
            problems.append("required test /%s/ did not pass (missing, failed or skipped)" % pat)
    if pinned is not None:
        lines, more = _pinned_lines(run, pinned)
        out.extend(lines)
        problems.extend(more)
    if rc != 0 and not run.failed and not run.pkg_failed and run.raw:
        out.append("    the runner exited %d with no failing test; its own output follows" % rc)
        out.extend(quoted("the runner (exit %d)" % rc, run.raw))
    if rc != 0 and not problems:
        problems.append("the runner exited %d with no failing record" % rc)
    if problems:
        out.append("  VERDICT: NOT a pass:")
        out.extend("    * " + p for p in problems)
        return 1, out
    if pinned is not None:
        out.append(
            "  VERDICT: pass: %d stub-backed tests ran, none skipped behind the stub gate, all %d that %s pins "
            "on %s among them, and none it does not" % (len(run.passed), sum(pinned.required.values()), pinned.shown, pinned.os)
        )
    else:
        out.append("  VERDICT: pass: %d stub-backed tests ran, none skipped behind the stub gate" % len(run.passed))
    return 0, out


# ------------------------------------------------------------------ selftest


def _cargo_dir(tmp, name, targets):
    """A directory as rust.sh writes it, from {target: (its output, its exit code)}."""
    d = os.path.join(tmp, name)
    os.makedirs(d)
    for target, (log, rc) in targets.items():
        with open(os.path.join(d, target + ".log"), "w", encoding="utf-8") as f:
            f.write(log)
        with open(os.path.join(d, target + ".rc"), "w", encoding="utf-8") as f:
            f.write("%d\n" % rc)
    return d


def _libtest(ok, failed, ignored=(), show=None, failures=None):
    """One target's output in libtest's own shape under --show-output: cargo's
    lines, the result lines, the successes: and failures: sections, the
    summary. ignored is [(name, reason)]; show and failures map a test to
    its own captured lines."""
    show, failures = show or {}, failures or {}
    n = len(ok) + len(failed) + len(ignored)
    out = ["   Compiling chtypes v2.0.0-dev", "     Running unittests src/lib.rs (target/debug/deps/chtypes-0)", "", "running %d test%s" % (n, "" if n == 1 else "s")]
    out += ["test %s ... ok" % t for t in ok]
    out += ["test %s ... FAILED" % t for t in failed]
    out += ["test %s ... ignored, %s" % (t, why) for t, why in ignored]
    out += ["", "successes:", ""]
    for t, text in show.items():
        out += ["---- %s stdout ----" % t, text, ""]
    out += ["", "successes:"] + ["    " + t for t in sorted(ok)]
    if failed:
        out += ["", "failures:", ""]
        for t in failed:
            out += ["---- %s stdout ----" % t] + failures.get(t, ["thread '%s' panicked at x.rs:1:1:" % t, "boom"]) + [""]
        out += ["", "failures:"] + ["    " + t for t in sorted(failed)]
    out += [
        "",
        "test result: %s. %d passed; %d failed; %d ignored; 0 measured; 0 filtered out; finished in 0.01s"
        % ("FAILED" if failed else "ok", len(ok), len(failed), len(ignored)),
        "",
    ]
    return "\n".join(out) + "\n"


def _gojson(path, events):
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def selftest():
    tmp = tempfile.mkdtemp(prefix="stub-census-selftest-")

    def check(label, code, lines, want_fail, needle=None):
        bad = (code != 0) != want_fail or (needle and not any(needle in x for x in lines))
        if bad:
            print("SELFTEST FAILED: %s\n%s" % (label, "\n".join(lines)), file=sys.stderr)
            sys.exit(1)

    def p(n):
        return {"Action": "pass", "Test": n, "Package": "x/chtypes"}

    req = ["TestSetupCases", "TestStub.*", "TestRegistry.*"]
    base = [p("TestSetupCases"), p("TestStubOpen"), p("TestRegistryA")]

    g = os.path.join(tmp, "ok.json")
    _gojson(g, base)
    c, res = verdict(parse_go(g), 0, req)
    check("a clean run was refused", c, res, False, "REQUIRED /TestStub.*/ RAN: 1 passed")

    # THE PIN: a stub-skip while the stubs are set is a failure, named.
    g = os.path.join(tmp, "stubskip.json")
    _gojson(
        g,
        base
        + [
            {"Action": "output", "Test": "TestStubB", "Package": "x/chtypes", "Output": "    s.go:1: SKIPPED: CHTYPES_ABI1_STUBS is not set\n"},
            {"Action": "skip", "Test": "TestStubB", "Package": "x/chtypes"},
        ],
    )
    c, res = verdict(parse_go(g), 0, req)
    check("a stub-skip with stubs set was accepted", c, res, True, "stub-skip with the stubs set")
    # The same for an ABI v2 binding's own variable.
    g = os.path.join(tmp, "stubskip2.json")
    _gojson(
        g,
        base
        + [
            {"Action": "output", "Test": "TestStubB", "Package": "x/chtypes", "Output": "    s.go:1: SKIPPED: CHTYPES_ABI2_STUBS is not set\n"},
            {"Action": "skip", "Test": "TestStubB", "Package": "x/chtypes"},
        ],
    )
    c, res = verdict(parse_go(g), 0, req)
    check("an ABI v2 stub-skip with stubs set was accepted", c, res, True, "stub-skip with the stubs set")

    # A skip for another named reason is reported, not failed.
    g = os.path.join(tmp, "othskip.json")
    _gojson(
        g,
        base
        + [
            {"Action": "output", "Test": "TestOther", "Package": "x/o", "Output": "    o.go:1: SKIPPED: fixtures absent\n"},
            {"Action": "skip", "Test": "TestOther", "Package": "x/o"},
        ],
    )
    c, res = verdict(parse_go(g), 0, req)
    check("a named non-stub skip was refused or unnamed", c, res, False, "SKIP x/o TestOther: o.go:1: SKIPPED: fixtures absent")

    # A required test that never ran is a failure (the derived-value pin: the
    # pattern list is what the script passes, so deleting TestSetupCases fails).
    g = os.path.join(tmp, "missing.json")
    _gojson(g, [p("TestStubOpen"), p("TestRegistryA")])
    c, res = verdict(parse_go(g), 0, req)
    check("a missing required test was accepted", c, res, True, "required test /TestSetupCases/")

    g = os.path.join(tmp, "zero.json")
    _gojson(g, [{"Action": "start", "Package": "x"}])
    c, res = verdict(parse_go(g), 0, [])
    check("a zero-run was accepted", c, res, True, "zero tests passed")

    c, res = verdict(parse_go(os.path.join(tmp, "ok.json")), 2, req)
    check("a nonzero exit with no failing record was accepted", c, res, True, "exited 2")

    # junit: a whole file skipped behind a stub gate.
    j = os.path.join(tmp, "j.xml")
    open(j, "w").write(
        '<testsuites><testsuite>'
        '<testcase classname="tests.abi1.test_a" name="t1"/>'
        '<testcase classname="tests.abi1.test_b" name="t2"><skipped message="CHTYPES_ABI1_STUBS not set"/></testcase>'
        "</testsuite></testsuites>"
    )
    c, res = verdict(parse_junit(j), 0)
    check("a stub-skipped pytest file was accepted", c, res, True, "tests.abi1.test_b")

    # vitest: same, and an explicit platform skip inside a passing file is fine.
    v = os.path.join(tmp, "v.json")
    json.dump(
        {
            "testResults": [
                {"name": "/r/test/abi1/a.test.ts", "assertionResults": [
                    {"status": "passed", "fullName": "a ok", "title": "ok"},
                    {"status": "skipped", "fullName": "a linux only", "title": "linux only"}]},
                {"name": "/r/test/abi1/b.test.ts", "assertionResults": [
                    {"status": "skipped", "fullName": "b x", "title": "x"}]},
            ]
        },
        open(v, "w"),
    )
    c, res = verdict(parse_vitest(v), 0)
    check("a wholly skipped vitest file was accepted", c, res, True, "b.test.ts")
    json.dump({"testResults": [{"name": "/r/a.test.ts", "assertionResults": [
        {"status": "passed", "fullName": "a", "title": "a"}, {"status": "skipped", "fullName": "p", "title": "p"}]}]}, open(v, "w"))
    c, res = verdict(parse_vitest(v), 0)
    check("a non-stub skip inside a passing vitest file was refused", c, res, False)

    # cargo: a loud skip line naming the stubs fails, wherever libtest prints
    # it (--show-output puts a passing test's own lines in `successes:`).
    t = _cargo_dir(
        tmp,
        "stubskip",
        {
            "api_v1": (
                _libtest(["one"], [], show={"one": "SKIPPED (loudly): api_v1 needs %s (the v1-abi-stubs directory); nothing was exercised" % STUB_ENV}),
                0,
            )
        },
    )
    c, res = verdict(parse_cargo(t), 0)
    check("a loud rust stub-skip was accepted", c, res, True, "stub-skip with the stubs set")

    # ---- a failure carries its own text into the log, in a group ----------

    def group(label, lines, title):
        """The quoted lines of the group whose title starts with `title`."""
        for i, line in enumerate(lines):
            if line.startswith("::group::" + title):
                body = []
                for x in lines[i + 1 :]:
                    if x == "::endgroup::":
                        return body
                    body.append(x)
                break
        print("SELFTEST FAILED: %s: no ::group:: titled %r\n%s" % (label, title, "\n".join(lines)), file=sys.stderr)
        sys.exit(1)

    def has(label, body, *needles, absent=()):
        missing = [n for n in needles if not any(n in x for x in body)]
        present = [n for n in absent if any(n in x for x in body)]
        if missing or present:
            print("SELFTEST FAILED: %s: missing %r, present %r\n%s" % (label, missing, present, "\n".join(body)), file=sys.stderr)
            sys.exit(1)

    def out(t, o, pkg="x/chtypes"):
        return {"Action": "output", "Test": t, "Package": pkg, "Output": o + "\n"}

    # THE PIN for the reporter: a failing go test whose own output holds a
    # race report and an assertion. Both reach the log inside its group,
    # every line quoted; a same-named test of ANOTHER package (which passed)
    # lends it none of its output; a package result that is only go's own
    # framing adds no group; and a test-output line shaped as a workflow
    # command cannot close the group or raise an annotation.
    g = os.path.join(tmp, "failtext.json")
    _gojson(
        g,
        base
        + [
            out("TestRegistryX", "this line is the other package's", pkg="x/other"),
            {"Action": "pass", "Test": "TestRegistryX", "Package": "x/other"},
            out("TestRegistryX", "=== RUN   TestRegistryX"),
            out("TestRegistryX", "=================="),
            out("TestRegistryX", "WARNING: DATA RACE"),
            out("TestRegistryX", "Write at 0x00c0001a2b40 by goroutine 41:"),
            out("TestRegistryX", "::endgroup::"),
            out("TestRegistryX", "  ::error::planted"),
            out("TestRegistryX", "    registry_flight_test.go:555: GET /_gate/parked/s-flight-abandon?n=2: 504 Gateway Timeout"),
            out("TestRegistryX", "    testing.go:1617: race detected during execution of test"),
            out("TestRegistryX", "--- FAIL: TestRegistryX (60.08s)"),
            {"Action": "fail", "Test": "TestRegistryX", "Package": "x/chtypes"},
            {"Action": "output", "Package": "x/chtypes", "Output": "FAIL\n"},
            {"Action": "output", "Package": "x/chtypes", "Output": "FAIL\tx/chtypes\t61.2s\n"},
            {"Action": "fail", "Package": "x/chtypes"},
        ],
    )
    c, res = verdict(parse_go(g), 1, req)
    check("a failing test was accepted", c, res, True, "1 test(s) failed")
    body = group("a failing go test", res, "FAILED TestRegistryX: its own output, 9 lines")
    has(
        "a failing go test's own output",
        body,
        "      | WARNING: DATA RACE",
        "      | Write at 0x00c0001a2b40 by goroutine 41:",
        "      |     registry_flight_test.go:555: GET /_gate/parked/s-flight-abandon?n=2: 504 Gateway Timeout",
        "      |     testing.go:1617: race detected during execution of test",
        absent=("the other package's",),
    )
    commands = [x for x in res if x.lstrip().startswith("::")]
    if len(commands) != 2 or not commands[0].startswith("::group::FAILED TestRegistryX") or commands[1] != "::endgroup::":
        print("SELFTEST FAILED: test output was emitted as a workflow command:\n%s" % "\n".join(commands), file=sys.stderr)
        sys.exit(1)
    has("go's own framing of a failed package", res, absent=("printed this outside its tests",))

    # A race between tests is reported at package level: printed beside the
    # failing test, in its own group.
    g = os.path.join(tmp, "pkgrace.json")
    _gojson(
        g,
        base
        + [
            out("TestRegistryY", "    y_test.go:9: boom"),
            {"Action": "fail", "Test": "TestRegistryY", "Package": "x/chtypes"},
            {"Action": "output", "Package": "x/chtypes", "Output": "WARNING: DATA RACE\n"},
            {"Action": "output", "Package": "x/chtypes", "Output": "FAIL\tx/chtypes\t1.2s\n"},
            {"Action": "fail", "Package": "x/chtypes"},
        ],
    )
    c, res = verdict(parse_go(g), 1, req)
    check("a failing test with a package-level race was accepted", c, res, True, "1 test(s) failed")
    has("a race reported between tests", group("a race between tests", res, "package x/chtypes: its own output"), "      | WARNING: DATA RACE")

    # Truncation: the head and the tail are kept, the cut is said, and the
    # group never exceeds the cap.
    g = os.path.join(tmp, "long.json")
    _gojson(g, base + [out("TestStubLong", "line %04d" % i) for i in range(1000)] + [{"Action": "fail", "Test": "TestStubLong", "Package": "x/chtypes"}])
    c, res = verdict(parse_go(g), 1, req)
    body = group("a long failure", res, "FAILED TestStubLong: its own output, 1000 lines, truncated")
    has("a long failure, truncated", body, "line 0000", "line 0299", "line 0900", "line 0999", "truncated: 600 lines omitted", absent=("line 0300", "line 0899"))
    if len(body) != FAIL_OUTPUT_CAP + 1:
        print("SELFTEST FAILED: a truncated group holds %d lines, want %d" % (len(body), FAIL_OUTPUT_CAP + 1), file=sys.stderr)
        sys.exit(1)

    # A package that fails outside any test (a race after its tests, a
    # panic in TestMain) is refused, by name, with its own output.
    g = os.path.join(tmp, "pkgfail.json")
    _gojson(
        g,
        base
        + [
            {"Action": "start", "Package": "x/leak"},
            {"Action": "output", "Package": "x/leak", "Output": "panic: boom outside any test\n"},
            {"Action": "output", "Package": "x/leak", "Output": "FAIL\tx/leak\t0.1s\n"},
            {"Action": "fail", "Package": "x/leak"},
        ],
    )
    c, res = verdict(parse_go(g), 1, req)
    check("a package failing outside any test was accepted", c, res, True, "package x/leak failed outside any test")
    has("a package failing outside any test", group("a package failure", res, "FAILED package x/leak"), "      | panic: boom outside any test")

    # A build failure (go 1.24+: build-output events, keyed by ImportPath,
    # and the package's fail naming FailedBuild).
    g = os.path.join(tmp, "buildfail.json")
    _gojson(
        g,
        base
        + [
            {"ImportPath": "x/broken [x/broken.test]", "Action": "build-output", "Output": "# x/broken [x/broken.test]\n"},
            {"ImportPath": "x/broken [x/broken.test]", "Action": "build-output", "Output": "broken/b.go:3:1: syntax error: planted\n"},
            {"ImportPath": "x/broken [x/broken.test]", "Action": "build-fail"},
            {"Action": "start", "Package": "x/broken"},
            {"Action": "output", "Package": "x/broken", "Output": "FAIL\tx/broken [build failed]\n"},
            {"Action": "fail", "Package": "x/broken", "FailedBuild": "x/broken [x/broken.test]"},
        ],
    )
    c, res = verdict(parse_go(g), 1, req)
    check("a build failure was accepted", c, res, True, "package x/broken failed outside any test")
    has("a build failure", group("a build failure", res, "FAILED package x/broken"), "syntax error: planted")

    # junit: the failure's message and text.
    open(j, "w").write(
        '<testsuites><testsuite>'
        '<testcase classname="tests.abi2.test_a" name="t1"/>'
        '<testcase classname="tests.abi2.test_a" name="t2"><failure message="assert 1 == 2">'
        "def test_t2():\n&gt;       assert 1 == 2\nE       assert 1 == 2\n</failure></testcase>"
        "</testsuite></testsuites>"
    )
    c, res = verdict(parse_junit(j), 1)
    check("a failing pytest test was accepted", c, res, True, "1 test(s) failed")
    has("a failing pytest test", group("junit", res, "FAILED tests.abi2.test_a::t2"), "      | assert 1 == 2", "      | E       assert 1 == 2")

    # vitest: the assertion's failureMessages.
    json.dump({"testResults": [{"name": "/r/a.test.ts", "assertionResults": [
        {"status": "passed", "fullName": "a", "title": "a"},
        {"status": "failed", "fullName": "b", "title": "b",
         "failureMessages": ["AssertionError: expected 1 to be 2\n    at /r/a.test.ts:3:5"]}]}]}, open(v, "w"))
    c, res = verdict(parse_vitest(v), 1)
    check("a failing vitest test was accepted", c, res, True, "1 test(s) failed")
    has("a failing vitest test", group("vitest", res, "FAILED a.test.ts :: b"), "AssertionError: expected 1 to be 2", "at /r/a.test.ts:3:5")

    # cargo: a failing test's own text, from libtest's `failures:` section
    # (its captured output and its panic; the leg passes --show-output, never
    # --nocapture), each under its own name.
    t = _cargo_dir(
        tmp,
        "failtext",
        {
            "api_v1": (
                _libtest(
                    ["a"],
                    ["b", "c"],
                    failures={
                        "b": ["thread 'b' panicked at rust/tests/api_v1.rs:9:5:", "assertion `left == right` failed", "  left: 1", " right: 2"],
                        "c": ["c printed this", "thread 'c' panicked at rust/tests/api_v1.rs:12:5:", "c's own message"],
                    },
                ),
                101,
            )
        },
    )
    c, res = verdict(parse_cargo(t), 101)
    check("a failing cargo test was accepted", c, res, True, "2 test(s) failed")
    has("a failing cargo test", group("cargo b", res, "FAILED api_v1 b"), "assertion `left == right` failed", " right: 2", absent=("c's own message",))
    has("a failing cargo test's captured lines", group("cargo c", res, "FAILED api_v1 c"), "c printed this", "c's own message")

    # A target that crashes mid-run (no `test result:` line) is refused as
    # uncounted, and quotes its own output.
    t = _cargo_dir(
        tmp,
        "crash",
        {
            "api_v1": (
                "running 2 tests\ntest a ... ok\n"
                "error: test failed, to rerun pass `--test api_v1`\n\n"
                "Caused by:\n  process didn't exit successfully (signal: 11, SIGSEGV: invalid memory reference)\n",
                101,
            )
        },
    )
    c, res = verdict(parse_cargo(t), 101)
    check("a crashed cargo target was accepted", c, res, True, "it crashed or was killed")
    check("a crashed cargo target's output was not quoted", c, res, True, "the runner exited 101 with no failing test")
    has("a crashed cargo target", group("cargo crash", res, "the runner (exit 101)"), "SIGSEGV: invalid memory reference")

    # A target without a libtest harness (harness = false) is one test: its
    # own name, passed when it exits 0, and failed with its whole output.
    t = _cargo_dir(tmp, "noharness", {"goldens_v1_runner": ("goldens_v1_runner: FAILED: planted\n", 1), "api_v1": (_libtest(["a"], []), 0)})
    c, res = verdict(parse_cargo(t), 1)
    check("a failing harness = false target was accepted", c, res, True, "1 test(s) failed")
    has("a failing harness = false target", group("cargo noharness", res, "FAILED goldens_v1_runner"), "goldens_v1_runner: FAILED: planted")

    # ---- interleaved output is refused, never miscounted (public issue #727)

    # THE PIN for #727: under --nocapture another test's own lines landed
    # between libtest's `test <name> ... ` and its `ok` (measured: the count
    # drifted 164 vs 183 between identical runs). The sample must really split
    # a line (the old whole-line count reads it short of libtest's summary),
    # and the census must refuse it by name rather than count it.
    interleaved = (
        "running 3 tests\n"
        "test a ... ok\n"
        "test b ... projection case red-1: Violates(\"e_row_rejects\") (expected Violates(\"e_row_rejects\"))\n"
        "projection case red-2: Violates(\"e_row_rejects\") (expected Violates(\"e_row_rejects\"))\n"
        "ok\n"
        "test c ... ok\n\n"
        "test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s\n"
    )
    old_count = sum(1 for x in interleaved.split("\n") if re.match(r"test (\S+) \.\.\. ok", x))
    if old_count != 2:
        print("SELFTEST FAILED: the interleaved sample does not split a line (the old count read %d of 3)" % old_count, file=sys.stderr)
        sys.exit(1)
    t = _cargo_dir(tmp, "interleaved", {"lib": (interleaved, 0)})
    c, res = verdict(parse_cargo(t), 0)
    check("an interleaved cargo target was counted", c, res, True, "UNCOUNTED lib: libtest's own summary says 3 passed")
    check("an interleaved cargo target was not said to be interleaved", c, res, True, "its output was interleaved or cut")
    # A test's own lines inside libtest's sections never read as results.
    t = _cargo_dir(tmp, "sections", {"lib": (_libtest(["a"], [], show={"a": "test planted ... ok\ntest result: ok. 9 passed; 0 failed; 0 ignored; "}), 0)})
    c, res = verdict(parse_cargo(t), 0)
    check("a test's own libtest-shaped line was read as a result", c, res, False, "stub census: 1 passed")

    # ---- the pinned list (public issue #727) -------------------------------

    def pin_file(name, text):
        path = os.path.join(tmp, name + ".txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def pinned(name, text, os_name="darwin"):
        return Pinned(pin_file(name, text), os_name, name + ".txt")

    clean = {
        "lib": (_libtest(["x::a", "x::b"], []), 0),
        "api_v1": (_libtest(["one"], []), 0),
        "goldens_v1_runner": ("goldens_v1_runner: stub self-test OK\n", 0),
    }
    pin = "# a comment\n\nlib x::a\nlib x::b\napi_v1 one\ngoldens_v1_runner\n[linux] lib x::linux_only\n"
    t = _cargo_dir(tmp, "pinclean", clean)
    c, res = verdict(parse_cargo(t), 0, (), pinned("pin", pin))
    check("a run that is exactly its pinned list was refused", c, res, False, "all 4 that pin.txt pins on darwin among them")

    # A dropped test fails, by name, for each way a test can drop.
    dropped = dict(clean, lib=(_libtest(["x::a"], [], ignored=[("x::b", "planted")]), 0))
    t = _cargo_dir(tmp, "pinignored", dropped)
    c, res = verdict(parse_cargo(t), 0, (), pinned("pin", pin))
    check("an ignored pinned cargo test was accepted", c, res, True, "- lib x::b  (it skipped: ignored: planted)")
    check("a dropped test did not change the count", c, res, True, "stub census: 3 passed")
    t = _cargo_dir(tmp, "pinremoved", dict(clean, lib=(_libtest(["x::a"], []), 0)))
    c, res = verdict(parse_cargo(t), 0, (), pinned("pin", pin))
    check("a removed pinned cargo test was accepted", c, res, True, "- lib x::b  (it did not run")
    t = _cargo_dir(tmp, "pinnoharness", dict(clean, goldens_v1_runner=("boom\n", 1)))
    c, res = verdict(parse_cargo(t), 1, (), pinned("pin", pin))
    check("a failed pinned harness = false target was accepted", c, res, True, "- goldens_v1_runner  (it failed)")
    g = os.path.join(tmp, "pinskip.json")
    _gojson(
        g,
        [
            p("TestStubOpen"),
            {"Action": "output", "Test": "TestStubB", "Package": "x/chtypes", "Output": "    s.go:1: SKIPPED: planted\n"},
            {"Action": "skip", "Test": "TestStubB", "Package": "x/chtypes"},
        ],
    )
    c, res = verdict(parse_go(g), 0, (), pinned("gopin", "x/chtypes TestStubOpen\nx/chtypes TestStubB\n"))
    check("a skipped pinned go test was accepted", c, res, True, "- x/chtypes TestStubB  (it skipped: s.go:1: SKIPPED: planted)")
    json.dump({"testResults": [{"name": "/r/a.test.ts", "assertionResults": [{"status": "passed", "fullName": "a", "title": "a"}]}]}, open(v, "w"))
    c, res = verdict(parse_vitest(v), 0, (), pinned("tspin", "a.test.ts :: a\na.test.ts :: b\n"))
    check("a removed pinned vitest test was accepted", c, res, True, "- a.test.ts :: b  (it did not run")
    open(j, "w").write('<testsuites><testsuite><testcase classname="tests.abi2.test_a" name="t1"/></testsuite></testsuites>')
    c, res = verdict(parse_junit(j), 0, (), pinned("pypin", "tests.abi2.test_a::t1\ntests.abi2.test_a::t2\n"))
    check("a removed pinned pytest test was accepted", c, res, True, "- tests.abi2.test_a::t2  (it did not run")

    # An added test fails until its line is added, and the census prints the line.
    t = _cargo_dir(tmp, "pinadded", dict(clean, api_v1=(_libtest(["one", "two"], []), 0)))
    c, res = verdict(parse_cargo(t), 0, (), pinned("pin", pin))
    check("an unpinned test was accepted", c, res, True, "1 test(s) passed that pin.txt does not pin")
    check("an unpinned test's line was not printed", c, res, True, "      + api_v1 two")

    # An OS tag: required on its OS only, and neither required nor unpinned
    # elsewhere; an unknown tag is refused.
    c, res = verdict(parse_cargo(os.path.join(tmp, "pinclean")), 0, (), pinned("pin", pin, "linux"))
    check("a [linux] test missing on linux was accepted", c, res, True, "- lib x::linux_only")
    t = _cargo_dir(tmp, "pinlinux", dict(clean, lib=(_libtest(["x::a", "x::b", "x::linux_only"], []), 0)))
    c, res = verdict(parse_cargo(t), 0, (), pinned("pin", pin, "linux"))
    check("a run that is exactly its pinned list on linux was refused", c, res, False)
    c, res = verdict(parse_cargo(t), 0, (), pinned("pin", pin, "darwin"))
    check("a [linux] test that also passed on darwin was refused", c, res, False)
    c, res = verdict(parse_cargo(os.path.join(tmp, "pinclean")), 0, (), pinned("pinbad", pin + "[windows] lib x::w\n"))
    check("an unknown OS tag was accepted", c, res, True, "unknown OS tag [windows]")

    # The list is a multiset: two tests that share an identity are each pinned.
    t = _cargo_dir(tmp, "pinmulti", {"lib": (_libtest(["x::a"], []), 0), "api_v1": (_libtest(["x::a"], []), 0)})
    c, res = verdict(parse_cargo(t), 0, (), pinned("multi", "lib x::a\nlib x::a\napi_v1 x::a\n"))
    check("a twice-pinned identity that passed once was accepted", c, res, True, "- lib x::a  (1 of the 2 it pins passed)")

    # main(): no pinned list (or two) is a usage error, a missing one a refusal.
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        rc_none = main(["go", os.path.join(tmp, "ok.json"), "0"])
        rc_gone = main(["go", os.path.join(tmp, "ok.json"), "0", "--pinned", os.path.join(tmp, "absent.txt")])
        good = pin_file("okpin", "x/chtypes TestSetupCases\nx/chtypes TestStubOpen\nx/chtypes TestRegistryA\n")
        rc_good = main(["go", os.path.join(tmp, "ok.json"), "0", "--pinned", good])
        rc_both = main(["go", os.path.join(tmp, "ok.json"), "0", "--pinned", good, "--binding", "go"])
    if (rc_none, rc_gone, rc_good, rc_both) != (2, 1, 0, 2):
        print(
            "SELFTEST FAILED: main() with no, a missing, a matching and two pinned lists exited %r, want (2, 1, 0, 2)\n%s"
            % ((rc_none, rc_gone, rc_good, rc_both), sink.getvalue()),
            file=sys.stderr,
        )
        sys.exit(1)

    # The repository's own lists: every binding's, at the major
    # spec/binding-majors.json gives it (majors.py, the same derivation the
    # conformance scripts use), exists, parses with no unknown tag, pins tests
    # on every OS the legs run on, and is sorted (tags aside), as each says.
    for b in _majors().BINDINGS:
        rel = pinned_list(b)
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            print("SELFTEST FAILED: %s has no pinned list at %s" % (b, rel), file=sys.stderr)
            sys.exit(1)
        for o in KNOWN_OS:
            pl = Pinned(path, o, rel)
            if pl.problems or not pl.required:
                print("SELFTEST FAILED: %s on %s: %s" % (pl.shown, o, pl.problems or "it pins nothing"), file=sys.stderr)
                sys.exit(1)
        if pl.order != sorted(pl.order):
            at = next(i for i, (a, b) in enumerate(zip(pl.order, sorted(pl.order))) if a != b)
            print("SELFTEST FAILED: %s is not sorted: %r comes before %r" % (rel, pl.order[at], sorted(pl.order)[at]), file=sys.stderr)
            sys.exit(1)

    print(
        "stub_census: selftest ok: clean run passes; stub-skip, missing required test, zero-run, "
        "skipped-whole-file, a package failing outside any test and nonzero-exit-without-record are each "
        "refused for their own reason; every failure's own text (go, junit, vitest, cargo, a package's, "
        "a crashed runner's) reaches the log in its group, capped, quoted, and said when truncated; an "
        "interleaved or crashed cargo target is refused, never miscounted; and against a pinned list a "
        "dropped test (skipped, ignored, failed or gone) and an added test each fail by name, an OS tag "
        "binds on its OS only, and every binding's own list exists at its major"
    )
    return 0


PARSERS = {"go": parse_go, "junit": parse_junit, "vitest": parse_vitest, "cargo": parse_cargo}


def _shown(path):
    """A path as the log shows it: relative to the repository when inside it."""
    rel = os.path.relpath(os.path.abspath(path), ROOT)
    return path if rel.startswith("..") else rel


def main(argv):
    if argv[:1] == ["--selftest"]:
        return selftest()
    if len(argv) < 3 or argv[0] not in PARSERS:
        print(__doc__, file=sys.stderr)
        return 2
    kind, path, rc = argv[0], argv[1], int(argv[2])
    opts: dict[str, list[str]] = {"--require": [], "--pinned": [], "--binding": []}
    rest = argv[3:]
    while rest:
        if rest[0] not in opts or len(rest) < 2:
            print("stub census: unknown or incomplete option %r\n%s" % (rest[0], __doc__), file=sys.stderr)
            return 2
        opts[rest[0]].append(rest[1])
        rest = rest[2:]
    if len(opts["--pinned"]) + len(opts["--binding"]) != 1:
        print(
            "stub census: exactly one of --binding NAME (its pinned list, at its major) or --pinned FILE is required: "
            "a count alone cannot fail on a dropped test",
            file=sys.stderr,
        )
        return 2
    if opts["--binding"]:
        try:
            pinned_path = os.path.join(ROOT, pinned_list(opts["--binding"][0]))
        except Exception as e:  # majors.py's own refusal (an unknown binding, a broken map), said
            print("stub census: no pinned list for %r: %s" % (opts["--binding"][0], e))
            return 1
    else:
        pinned_path = opts["--pinned"][0]
    if not os.path.isfile(pinned_path):
        print("stub census: no pinned list at %s: nothing says which tests this run must pass" % pinned_path)
        return 1
    if kind == "cargo":
        ran = os.path.isdir(path) and any(f.endswith(".rc") for f in os.listdir(path))
    else:
        ran = os.path.isfile(path) and os.path.getsize(path) > 0
    if not ran:
        print("stub census: no result at %s (rc=%d): the runner produced nothing" % (path, rc))
        return 1
    run = PARSERS[kind](path)
    pinned = Pinned(pinned_path, host_os(), _shown(pinned_path))
    code, lines = verdict(run, rc, opts["--require"], pinned)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
