#!/usr/bin/env python3
"""stub_census.py: the verdict for a stub-backed test run (public issue #463).

The v1-abi-conformance legs run each binding's whole stub-backed public suite
with CHTYPES_ABI1_STUBS set. A test that SKIPS because the stubs are missing
while the stubs ARE set is a test that never ran, and a green exit code would
hide it. This reads the runner's own machine-readable result, never its exit
code alone, and refuses:

  * a run in which nothing passed (a zero-run);
  * any skip whose reason names CHTYPES_ABI1_STUBS (a stub-skip with the stubs set);
  * in the per-file formats (junit, vitest), a test FILE in which tests exist
    but none passed (a whole file skipped behind a stub gate);
  * a required test that did not pass, when --require is given (go);
  * a go package that failed outside any test (a race the detector caught
    after its tests, a panic in TestMain, a build failure).

Every skip for another reason is reported BY NAME with its reason, the way
scripts/check-standalone.sh reports its own.

Every failure carries its OWN TEXT into the job log, because the log is all
there is (no Actions artifacts): under each FAILED name, in a collapsible
::group::, the failing test's own output from the runner's result (go: its
`output` events, which hold an assertion's message and any `WARNING: DATA
RACE` report; junit: the failure's message and text; vitest: its
failureMessages; cargo: its panic and its captured stdout), and for a go
package that failed outside any test, the package's own output. Each is
capped at FAIL_OUTPUT_CAP lines, head and tail kept, and says "truncated"
when it cuts. Every quoted line is prefixed, so a line of test output can
never be read as a workflow command (an `::endgroup::` or an `::error::`).

    scripts/abi-v1/stub_census.py go     <go test -json log> <rc> [--require REGEX]...
    scripts/abi-v1/stub_census.py junit  <pytest --junitxml file> <rc>
    scripts/abi-v1/stub_census.py vitest <vitest --reporter=json file> <rc>
    scripts/abi-v1/stub_census.py text   <cargo test output> <rc>
    scripts/abi-v1/stub_census.py --selftest

The conformance scripts call it only when CHTYPES_ABI1_STUBS is set; without
the stubs they keep their old behavior (skip loudly by name, exit 0).
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET

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


class Run:
    """One run, reduced to what the verdict reads."""

    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[str] = []
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

    def file(self, f, passed):
        c = self.by_file.setdefault(f, [0, 0])
        c[1] += 1
        if passed:
            c[0] += 1

    def fail(self, name, text):
        self.failed.append(name)
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
                run.passed.append(t)
            elif a == "fail":
                run.fail(t, output.get(key, []))
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
        elif any(l.strip() and not GO_PKG_FRAMING.match(l.strip()) for l in lines):
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
            run.passed.append(name)
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
                run.passed.append(name)
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


def parse_text(path):
    run = Run()
    failed: list[str] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        all_lines = [l.rstrip("\n") for l in f]
    run.raw = all_lines
    for line in all_lines:
        m = re.match(r"test (\S+) \.\.\. (ok|FAILED|ignored)", line)
        if m:
            if m.group(2) == "ok":
                run.passed.append(m.group(1))
            elif m.group(2) == "FAILED":
                failed.append(m.group(1))
            else:
                run.skipped.append((m.group(1), "ignored"))
            continue
        # The rust suites skip by early return with a loud line, not by
        # #[ignore]; the line itself is the evidence.
        if re.search(r"\bSKIP(PED)?\b", line):
            run.skipped.append((line.strip()[:80], line.strip()))
    # A failing test's own text: libtest's `---- <name> stdout ----` section
    # (its captured output and panic, without --nocapture), and every
    # `thread '<name>' panicked at` block up to the next blank line (printed
    # inline under --nocapture, which the conformance leg passes).
    text: dict[str, list[str]] = {}
    i = 0
    while i < len(all_lines):
        line = all_lines[i]
        m = re.match(r"---- (\S+) stdout ----$", line)
        p = re.match(r"thread '([^']+)' panicked at", line)
        if m or p:
            name = (m or p).group(1)
            block = [] if m else [line]
            i += 1
            while i < len(all_lines):
                nxt = all_lines[i]
                if re.match(r"---- \S+ stdout ----$|failures:$|test result: |test \S+ \.\.\. ", nxt):
                    break
                if p and not nxt.strip():
                    break
                block.append(nxt)
                i += 1
            text.setdefault(name, []).extend(_split("\n".join(block)))
            continue
        i += 1
    for name in failed:
        run.fail(name, text.get(name, []))
    return run


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
    for l in shown:
        if l is None:
            out.append("      | ... truncated: %d lines omitted here ..." % (n - FAIL_OUTPUT_CAP))
        else:
            out.append("      | " + l)
    out.append("::endgroup::")
    return out


def verdict(run, rc, require=()):
    """(exit_code, lines). Pure, so --selftest drives the real thing."""
    out = ["  stub census: %d passed, %d skipped, %d failed" % (len(run.passed), len(run.skipped), len(run.failed))]
    problems = []
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
    if rc != 0 and not run.failed and not run.pkg_failed and run.raw:
        out.append("    the runner exited %d with no failing test; its own output follows" % rc)
        out.extend(quoted("the runner (exit %d)" % rc, run.raw))
    if rc != 0 and not problems:
        problems.append("the runner exited %d with no failing record" % rc)
    if problems:
        out.append("  VERDICT: NOT a pass:")
        out.extend("    * " + p for p in problems)
        return 1, out
    out.append("  VERDICT: pass: %d stub-backed assertions ran, none skipped behind the stub gate" % len(run.passed))
    return 0, out


# ------------------------------------------------------------------ selftest


def _gojson(path, events):
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def selftest():
    tmp = tempfile.mkdtemp(prefix="stub-census-selftest-")

    def check(label, code, lines, want_fail, needle=None):
        bad = (code != 0) != want_fail or (needle and not any(needle in l for l in lines))
        if bad:
            print("SELFTEST FAILED: %s\n%s" % (label, "\n".join(lines)), file=sys.stderr)
            sys.exit(1)

    p = lambda n: {"Action": "pass", "Test": n, "Package": "x/chtypes"}
    req = ["TestSetupCases", "TestStub.*", "TestRegistry.*"]
    base = [p("TestSetupCases"), p("TestStubOpen"), p("TestRegistryA")]

    g = os.path.join(tmp, "ok.json")
    _gojson(g, base)
    c, l = verdict(parse_go(g), 0, req)
    check("a clean run was refused", c, l, False, "REQUIRED /TestStub.*/ RAN: 1 passed")

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
    c, l = verdict(parse_go(g), 0, req)
    check("a stub-skip with stubs set was accepted", c, l, True, "stub-skip with the stubs set")
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
    c, l = verdict(parse_go(g), 0, req)
    check("an ABI v2 stub-skip with stubs set was accepted", c, l, True, "stub-skip with the stubs set")

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
    c, l = verdict(parse_go(g), 0, req)
    check("a named non-stub skip was refused or unnamed", c, l, False, "SKIP x/o TestOther: o.go:1: SKIPPED: fixtures absent")

    # A required test that never ran is a failure (the derived-value pin: the
    # pattern list is what the script passes, so deleting TestSetupCases fails).
    g = os.path.join(tmp, "missing.json")
    _gojson(g, [p("TestStubOpen"), p("TestRegistryA")])
    c, l = verdict(parse_go(g), 0, req)
    check("a missing required test was accepted", c, l, True, "required test /TestSetupCases/")

    g = os.path.join(tmp, "zero.json")
    _gojson(g, [{"Action": "start", "Package": "x"}])
    c, l = verdict(parse_go(g), 0, [])
    check("a zero-run was accepted", c, l, True, "zero tests passed")

    c, l = verdict(parse_go(os.path.join(tmp, "ok.json")), 2, req)
    check("a nonzero exit with no failing record was accepted", c, l, True, "exited 2")

    # junit: a whole file skipped behind a stub gate.
    j = os.path.join(tmp, "j.xml")
    open(j, "w").write(
        '<testsuites><testsuite>'
        '<testcase classname="tests.abi1.test_a" name="t1"/>'
        '<testcase classname="tests.abi1.test_b" name="t2"><skipped message="CHTYPES_ABI1_STUBS not set"/></testcase>'
        "</testsuite></testsuites>"
    )
    c, l = verdict(parse_junit(j), 0)
    check("a stub-skipped pytest file was accepted", c, l, True, "tests.abi1.test_b")

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
    c, l = verdict(parse_vitest(v), 0)
    check("a wholly skipped vitest file was accepted", c, l, True, "b.test.ts")
    json.dump({"testResults": [{"name": "/r/a.test.ts", "assertionResults": [
        {"status": "passed", "fullName": "a", "title": "a"}, {"status": "skipped", "fullName": "p", "title": "p"}]}]}, open(v, "w"))
    c, l = verdict(parse_vitest(v), 0)
    check("a non-stub skip inside a passing vitest file was refused", c, l, False)

    # text (cargo): a loud skip line naming the stubs fails.
    t = os.path.join(tmp, "t.txt")
    open(t, "w").write("test one ... ok\nSKIPPED (loudly): api_v1 needs %s (the v1-abi-stubs directory); nothing was exercised\n" % STUB_ENV)
    c, l = verdict(parse_text(t), 0)
    check("a loud rust stub-skip was accepted", c, l, True, "stub-skip with the stubs set")

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
    c, l = verdict(parse_go(g), 1, req)
    check("a failing test was accepted", c, l, True, "1 test(s) failed")
    body = group("a failing go test", l, "FAILED TestRegistryX: its own output, 9 lines")
    has(
        "a failing go test's own output",
        body,
        "      | WARNING: DATA RACE",
        "      | Write at 0x00c0001a2b40 by goroutine 41:",
        "      |     registry_flight_test.go:555: GET /_gate/parked/s-flight-abandon?n=2: 504 Gateway Timeout",
        "      |     testing.go:1617: race detected during execution of test",
        absent=("the other package's",),
    )
    commands = [x for x in l if x.lstrip().startswith("::")]
    if len(commands) != 2 or not commands[0].startswith("::group::FAILED TestRegistryX") or commands[1] != "::endgroup::":
        print("SELFTEST FAILED: test output was emitted as a workflow command:\n%s" % "\n".join(commands), file=sys.stderr)
        sys.exit(1)
    has("go's own framing of a failed package", l, absent=("printed this outside its tests",))

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
    c, l = verdict(parse_go(g), 1, req)
    check("a failing test with a package-level race was accepted", c, l, True, "1 test(s) failed")
    has("a race reported between tests", group("a race between tests", l, "package x/chtypes: its own output"), "      | WARNING: DATA RACE")

    # Truncation: the head and the tail are kept, the cut is said, and the
    # group never exceeds the cap.
    g = os.path.join(tmp, "long.json")
    _gojson(g, base + [out("TestStubLong", "line %04d" % i) for i in range(1000)] + [{"Action": "fail", "Test": "TestStubLong", "Package": "x/chtypes"}])
    c, l = verdict(parse_go(g), 1, req)
    body = group("a long failure", l, "FAILED TestStubLong: its own output, 1000 lines, truncated")
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
    c, l = verdict(parse_go(g), 1, req)
    check("a package failing outside any test was accepted", c, l, True, "package x/leak failed outside any test")
    has("a package failing outside any test", group("a package failure", l, "FAILED package x/leak"), "      | panic: boom outside any test")

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
    c, l = verdict(parse_go(g), 1, req)
    check("a build failure was accepted", c, l, True, "package x/broken failed outside any test")
    has("a build failure", group("a build failure", l, "FAILED package x/broken"), "syntax error: planted")

    # junit: the failure's message and text.
    open(j, "w").write(
        '<testsuites><testsuite>'
        '<testcase classname="tests.abi2.test_a" name="t1"/>'
        '<testcase classname="tests.abi2.test_a" name="t2"><failure message="assert 1 == 2">'
        "def test_t2():\n&gt;       assert 1 == 2\nE       assert 1 == 2\n</failure></testcase>"
        "</testsuite></testsuites>"
    )
    c, l = verdict(parse_junit(j), 1)
    check("a failing pytest test was accepted", c, l, True, "1 test(s) failed")
    has("a failing pytest test", group("junit", l, "FAILED tests.abi2.test_a::t2"), "      | assert 1 == 2", "      | E       assert 1 == 2")

    # vitest: the assertion's failureMessages.
    json.dump({"testResults": [{"name": "/r/a.test.ts", "assertionResults": [
        {"status": "passed", "fullName": "a", "title": "a"},
        {"status": "failed", "fullName": "b", "title": "b",
         "failureMessages": ["AssertionError: expected 1 to be 2\n    at /r/a.test.ts:3:5"]}]}]}, open(v, "w"))
    c, l = verdict(parse_vitest(v), 1)
    check("a failing vitest test was accepted", c, l, True, "1 test(s) failed")
    has("a failing vitest test", group("vitest", l, "FAILED a.test.ts :: b"), "AssertionError: expected 1 to be 2", "at /r/a.test.ts:3:5")

    # cargo, under --nocapture (the conformance leg's): the panic block; and
    # libtest's captured `---- name stdout ----` section without it.
    open(t, "w").write(
        "running 3 tests\n"
        "test a ... ok\n"
        "thread 'b' panicked at rust/tests/api_v1.rs:9:5:\n"
        "assertion `left == right` failed\n"
        "  left: 1\n"
        " right: 2\n"
        "\n"
        "test b ... FAILED\n"
        "test c ... FAILED\n"
        "\nfailures:\n\n"
        "---- c stdout ----\n"
        "c printed this\n"
        "thread 'c' panicked at rust/tests/api_v1.rs:12:5:\n"
        "c's own message\n"
        "\n\nfailures:\n    b\n    c\n\n"
        "test result: FAILED. 1 passed; 2 failed; 0 ignored\n"
    )
    c, l = verdict(parse_text(t), 101)
    check("a failing cargo test was accepted", c, l, True, "2 test(s) failed")
    has("a failing cargo test (--nocapture)", group("cargo b", l, "FAILED b"), "assertion `left == right` failed", " right: 2", absent=("c's own message",))
    has("a failing cargo test (captured)", group("cargo c", l, "FAILED c"), "c printed this", "c's own message")

    # A runner that exits nonzero with no failing test (a crash, a build
    # error) quotes its own output, and is still refused.
    open(t, "w").write(
        "test a ... ok\n"
        "error: test failed, to rerun pass `--test api_v1`\n"
        "Caused by:\n  process didn't exit successfully (signal: 11, SIGSEGV: invalid memory reference)\n"
    )
    c, l = verdict(parse_text(t), 101)
    check("a crashed cargo run was accepted", c, l, True, "exited 101 with no failing record")
    has("a crashed cargo run", group("cargo crash", l, "the runner (exit 101)"), "SIGSEGV: invalid memory reference")

    print("stub_census: selftest ok: clean run passes; stub-skip, missing required test, zero-run, "
          "skipped-whole-file, a package failing outside any test and nonzero-exit-without-record are each "
          "refused for their own reason; and every failure's own text (go, junit, vitest, cargo, a package's, "
          "a crashed runner's) reaches the log in its group, capped, quoted, and said when truncated")
    return 0


def main(argv):
    if argv[:1] == ["--selftest"]:
        return selftest()
    if len(argv) < 3 or argv[0] not in ("go", "junit", "vitest", "text"):
        print(__doc__, file=sys.stderr)
        return 2
    kind, path, rc = argv[0], argv[1], int(argv[2])
    require = [argv[i + 1] for i, a in enumerate(argv[3:], 3) if a == "--require" and i + 1 < len(argv)]
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        print("stub census: no result file at %s (rc=%d): the runner produced nothing" % (path, rc))
        return 1
    run = {"go": parse_go, "junit": parse_junit, "vitest": parse_vitest, "text": parse_text}[kind](path)
    code, lines = verdict(run, rc, require)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
