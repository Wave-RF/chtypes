#!/usr/bin/env python3
"""standalone_census.py — read scripts/check-standalone.sh's `go test -json`
output and decide whether the untagged Go suite actually proved anything.

Extracted out of check-standalone.sh so the verdict logic can be pinned
directly: a --selftest that feeds it a synthetic log exercises the SAME code
the real run does, rather than a hand-rolled re-implementation of the rule
(see scripts/check-suite.sh's remarks on why a derived value must never be
hand-set in its own test).

The verdict: no test failed, at least one test ran, and `go test` exited 0
(or, if it did not, a failing record explains why). Every per-test skip is
named. One fixed line, `chtypes-count suite=go-no-artifacts ran=<n>
skipped=<n>`, goes out for scripts/policy-merge-check.py's `test-counts`
condition, derived from the same `ran`/`skip` the verdict reads.

    scripts/lib/standalone_census.py <log> <rc>
    scripts/lib/standalone_census.py --selftest
"""

from __future__ import annotations

import json
import os
import sys


def _skip_message(output_lines):
    """The message to print for one skipped test (chtypes#348): its own last
    non-empty `output` line before the skip event, with the `--- SKIP:`
    framing line Go always emits just before that event excluded — otherwise
    every skip would print that boilerplate instead of the t.Skip/t.Skipf
    text a line or two above it.
    """
    for line in reversed(output_lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("--- SKIP:"):
            continue
        return stripped
    return ""


def run_census(log_path, rc):
    """Read one go-test -json log and return (exit_code, printed_lines).

    Pure function of its arguments — no environment, no process exit — so a
    selftest can drive it directly against a synthetic log and read back
    exactly what a real run would have printed.
    """
    ran = skip = fail = 0
    skips: list[str] = []
    noise: list[str] = []
    failed: list[str] = []
    output: dict[str, list[str]] = {}
    passed: set[str] = set()
    lines: list[str] = []
    # A package-level skip (the whole package built but matched no tests, or
    # was excluded by a build constraint) carries no `Test` field at all —
    # chtypes#348 asks only for PER-TEST skips to be named here, so those
    # events stay excluded from `skip`/`skips` exactly as the pre-existing
    # `if not t: continue` already excluded them (they were never counted,
    # not merely unlabelled). `test_package` remembers each named test's own
    # package so the printed line can carry it without a second pass over
    # the log.
    test_package: dict[str, str] = {}

    with open(log_path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line.startswith("{"):
                if line:
                    noise.append(line)
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                noise.append(line)
                continue
            t, a = ev.get("Test"), ev.get("Action")
            if not t:
                continue
            test_package.setdefault(t, ev.get("Package", ""))
            if a == "output":
                output.setdefault(t, []).append(ev.get("Output", "").rstrip("\n"))
                continue
            if a == "pass":
                ran += 1
                passed.add(t)
            elif a == "fail":
                ran += 1
                fail += 1
                failed.append(t)
            elif a == "skip":
                skip += 1
                skips.append(t)

    lines.append("  standalone census — %d ran, %d skipped, %d failed" % (ran, skip, fail))
    # chtypes#285 §1b — one fixed, machine-readable line per job, read by
    # scripts/policy-merge-check.py's `test-counts` condition off THIS job's
    # own log (a check run's own `output.summary`/annotations are NOT
    # populated for a GitHub-Actions-authored job — verified against a real
    # run, not assumed; see that script's module docstring). Derived from
    # `ran`/`skip` above, never a second, hand-set count — see --selftest's
    # own pin that this line moves when the derivation does.
    lines.append("chtypes-count suite=go-no-artifacts ran=%d skipped=%d" % (ran, skip))
    # chtypes#348 — name every per-test skip, not just count it: a skip that
    # should have been an assertion must not look identical to an intended
    # one. `<package>` comes from `test_package` (captured above, off the
    # same events); `<message>` is that test's own last non-empty `output`
    # line before its skip event, with the `--- SKIP:` framing line Go itself
    # prints excluded, so what is left is the line carrying the actual
    # t.Skip/t.Skipf text (e.g. "    foo_test.go:12: some reason").
    for t in skips:
        lines.append("    SKIP %s %s: %s" % (test_package.get(t, ""), t, _skip_message(output.get(t, []))))
    # A failure is named, and its own last words are quoted — a count alone
    # sends whoever reads the log to fetch the -json file, which CI does not
    # keep.
    for t in failed:
        lines.append("    FAILED " + t)
        tail = [
            l for l in output.get(t, []) if l.strip() and not l.startswith("=== RUN") and not l.startswith("--- FAIL")
        ][-12:]
        for l in tail:
            lines.append("      | " + l[:200])
    for n in noise[:20]:
        lines.append("    " + n[:160])

    problems = []
    if fail:
        problems.append("%d test(s) failed" % fail)
    if ran == 0:
        problems.append("zero tests ran — the untagged suite asserted nothing")
    if rc != 0 and not problems:
        problems.append("go test exited %d with no failing record; read %s" % (rc, log_path))
    if problems:
        lines.append("  VERDICT: NOT a pass —")
        for p in problems:
            lines.append("    * " + p)
        return 1, lines
    lines.append("  VERDICT: pass — the dlopen-only SDK built, vetted and ran %d assertions from a bare copy (%d skipped by name above)" % (ran, skip))
    return 0, lines


def _write_log(path, events):
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def selftest():
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="chtypes-standalone-census-selftest-")

    def fail(msg, lines=()):
        print("SELFTEST FAILED: " + msg + ("\n" + "\n".join(lines) if lines else ""), file=sys.stderr)
        return 1

    try:
        # 1. A clean run passes, and the count line is DERIVED from the same
        # ran/skip the verdict reads (chtypes#285 §1b): two passes, no skip.
        clean = os.path.join(tmp, "clean.json")
        _write_log(clean, [{"Action": "pass", "Test": "TestA"}, {"Action": "pass", "Test": "TestB"}])
        code, lines = run_census(clean, 0)
        if code != 0:
            return fail("a clean run was refused:", lines)
        if "chtypes-count suite=go-no-artifacts ran=2 skipped=0" not in lines:
            return fail("the chtypes-count line did not read ran=2 skipped=0 off the clean fixture:", lines)

        # 2. A run that ran nothing is refused: a suite that asserted nothing
        # is not a pass.
        empty = os.path.join(tmp, "empty.json")
        _write_log(empty, [{"Action": "start", "Package": "example.com/pkg"}])
        code, lines = run_census(empty, 0)
        if code == 0 or not any("zero tests ran" in l for l in lines):
            return fail("a run with zero tests was accepted, or refused for the wrong reason:", lines)

        # 3. A failing test is refused and named, with its own last words.
        failing = os.path.join(tmp, "failing.json")
        _write_log(
            failing,
            [
                {"Action": "pass", "Test": "TestA"},
                {"Action": "output", "Test": "TestB", "Output": "    b_test.go:7: boom\n"},
                {"Action": "fail", "Test": "TestB"},
            ],
        )
        code, lines = run_census(failing, 1)
        if code == 0 or "    FAILED TestB" not in lines or not any("boom" in l for l in lines):
            return fail("a failing test was accepted, unnamed, or its output was not quoted:", lines)

        # 4. A non-zero exit with no failing record (a build failure) is refused.
        code, lines = run_census(clean, 2)
        if code == 0 or not any("exited 2 with no failing record" in l for l in lines):
            return fail("a non-zero go test exit with no failing record was accepted:", lines)

        # 5. THE PIN (chtypes#348): a named per-test skip prints its package,
        # name and message, a package-level skip (no `Test` at all) is never
        # printed as one and never inflates the counts.
        named = os.path.join(tmp, "named_skip.json")
        _write_log(
            named,
            [
                {"Action": "pass", "Test": "TestFoo", "Package": "example.com/pkg"},
                {"Action": "output", "Test": "TestBar", "Package": "example.com/pkg", "Output": "=== RUN   TestBar\n"},
                {
                    "Action": "output",
                    "Test": "TestBar",
                    "Package": "example.com/pkg",
                    "Output": "    bar_test.go:42: skipping because X is unavailable\n",
                },
                {"Action": "output", "Test": "TestBar", "Package": "example.com/pkg", "Output": "--- SKIP: TestBar (0.00s)\n"},
                {"Action": "skip", "Test": "TestBar", "Package": "example.com/pkg"},
                {"Action": "skip", "Package": "example.com/otherpkg"},
            ],
        )
        code, lines = run_census(named, 0)
        if "    SKIP example.com/pkg TestBar: bar_test.go:42: skipping because X is unavailable" not in lines:
            return fail("the named per-test skip was not printed as expected:", lines)
        if "chtypes-count suite=go-no-artifacts ran=1 skipped=1" not in lines:
            return fail("a package-level skip (no Test) inflated the skipped count:", lines)
        if any("otherpkg" in l for l in lines):
            return fail("the package-level skip (no Test field) was printed as a named skip:", lines)

        print(
            "check-standalone: selftest ok — a clean run passes with a count line derived from the same "
            "ran/skip, a zero-run, a failing test and a build failure are each refused for their own "
            "reason, and a named per-test skip prints its package, name and message while a "
            "package-level skip stays uncounted and unprinted (chtypes#348)"
        )
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv):
    if argv[:1] == ["--selftest"]:
        return selftest()
    if len(argv) != 2:
        print("usage: standalone_census.py <log> <rc>", file=sys.stderr)
        return 2
    code, lines = run_census(argv[0], int(argv[1]))
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
