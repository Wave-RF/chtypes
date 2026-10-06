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
  * a required test that did not pass, when --require is given (go).

Every skip for another reason is reported BY NAME with its reason, the way
scripts/check-standalone.sh reports its own.

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


class Run:
    """One run, reduced to what the verdict reads."""

    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[str] = []
        self.skipped: list[tuple[str, str]] = []  # (name, reason)
        self.by_file: dict[str, list[int]] = {}  # file -> [passed, total]
        self.notes: list[str] = []

    def file(self, f, passed):
        c = self.by_file.setdefault(f, [0, 0])
        c[1] += 1
        if passed:
            c[0] += 1


def parse_go(path):
    run = Run()
    output: dict[str, list[str]] = {}
    pkg: dict[str, str] = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw.startswith("{"):
                if raw:
                    run.notes.append(raw[:160])
                continue
            try:
                ev = json.loads(raw)
            except ValueError:
                run.notes.append(raw[:160])
                continue
            t, a = ev.get("Test"), ev.get("Action")
            if not t:
                continue
            pkg.setdefault(t, ev.get("Package", ""))
            if a == "output":
                output.setdefault(t, []).append(ev.get("Output", "").rstrip("\n"))
            elif a == "pass":
                run.passed.append(t)
            elif a == "fail":
                run.failed.append(t)
            elif a == "skip":
                lines = [
                    s.strip()
                    for s in output.get(t, [])
                    if s.strip() and not s.strip().startswith(("--- SKIP:", "=== "))
                ]
                run.skipped.append((pkg.get(t, "") + " " + t, lines[-1] if lines else ""))
    return run


def parse_junit(path):
    run = Run()
    root = ET.parse(path).getroot()
    for case in root.iter("testcase"):
        name = case.get("classname", "") + "::" + case.get("name", "")
        fname = case.get("classname", "")
        sk = case.find("skipped")
        if case.find("failure") is not None or case.find("error") is not None:
            run.failed.append(name)
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
                run.failed.append(name)
                run.file(fname, False)
            else:
                # vitest's JSON carries no skip reason; the title is the best
                # name there is (an explicit it.skip title states its own reason).
                run.skipped.append((name, a.get("title", "")))
                run.file(fname, False)
    return run


def parse_text(path):
    run = Run()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            m = re.match(r"test (\S+) \.\.\. (ok|FAILED|ignored)", line)
            if m:
                {"ok": run.passed, "FAILED": run.failed}.get(m.group(2), []).append(m.group(1))
                if m.group(2) == "ignored":
                    run.skipped.append((m.group(1), "ignored"))
                continue
            # The rust suites skip by early return with a loud line, not by
            # #[ignore]; the line itself is the evidence.
            if re.search(r"\bSKIP(PED)?\b", line):
                run.skipped.append((line.strip()[:80], line.strip()))
    return run


def verdict(run, rc, require=()):
    """(exit_code, lines). Pure, so --selftest drives the real thing."""
    out = ["  stub census: %d passed, %d skipped, %d failed" % (len(run.passed), len(run.skipped), len(run.failed))]
    problems = []
    for name, reason in run.skipped:
        out.append("    SKIP %s: %s" % (name, reason))
        if STUB_ENV_RE.search(reason) or STUB_ENV_RE.search(name):
            problems.append("stub-skip with the stubs set: %s" % name)
    for name in run.failed:
        out.append("    FAILED " + name)
    if run.failed:
        problems.append("%d test(s) failed" % len(run.failed))
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

    print("stub_census: selftest ok: clean run passes; stub-skip, missing required test, zero-run, "
          "skipped-whole-file and nonzero-exit-without-record are each refused for their own reason")
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
