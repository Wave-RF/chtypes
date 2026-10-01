#!/usr/bin/env python3
"""standalone_census.py — read scripts/check-standalone.sh's `go test -json`
output and decide whether the untagged Go suite actually proved anything.

Extracted out of check-standalone.sh's own inline heredoc (chtypes#225) so the
verdict logic can be pinned directly: a --selftest that feeds it a synthetic
log exercises the SAME code the real run does, rather than a hand-rolled
re-implementation of the rule that would only prove the pin agrees with
itself (see scripts/check-suite.sh's own remarks on why a derived value must
never be hand-set in its own test).

Also prints two chtypes#285 §1b lines, read by scripts/policy-merge-check.py's
`test-counts` condition off this job's own log: `chtypes-count suite=go-
no-artifacts|go-artifacts ran=<n> skipped=<n>` (derived from `ran`/`skip`
above), and — only under --require-artifacts, since that is the one run
where the golden set is actually exercised against a real registry —
`chtypes-count golden-cases=<n>`, `n` being `len(checked_subtests)`: the same
count the zero-checked guard (chtypes#225) already computes to tell
"TestGoldens ran" apart from "TestGoldens checked something", not a second,
freshly guessed number.

    scripts/lib/standalone_census.py <log> <rc> <have_reg 0|1> <require 0|1>
    scripts/lib/standalone_census.py --selftest
"""

from __future__ import annotations

import json
import os
import sys

# The parity contract (tests/parity/manifest.json) is checked by each
# language's own suite, and Go's runs here — from the bare copy, off the
# manifest it embeds, needing no artifact and no repository root. These names
# must have PASSED: a parity check that was deleted, renamed or skipped would
# otherwise leave the census looking exactly as healthy as one that ran.
PARITY = [
    "TestParityManifestMeetsItsOwnFloors",
    "TestParityManifestIsFullyDeclared",
    "TestGoExposesEveryCapabilityTheContractAssignsIt",
    "TestGoAnswersTheSameValuesAsTheOtherBindings",
    "TestGoCLIOffersEveryContractSubcommand",
    "TestNoGoPublicNameEscapesTheContract",
    "TestGoUnlistedAllowlistHasNotRotted",
]


def run_census(log_path, rc, have_reg, require, abi_fixtures_set):
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
    suite_label = "go-artifacts" if require else "go-no-artifacts"
    lines.append("chtypes-count suite=%s ran=%d skipped=%d" % (suite_label, ran, skip))
    # The golden-case count (chtypes#285 §1b) — sourced from where the
    # goldens are already counted, in this SAME required `artifacts` job:
    # every subtest actually named "TestGoldens/<version>/<case id>" that
    # passed is one checked golden case, the identical set the zero-checked
    # guard below already computes to tell "TestGoldens ran" apart from
    # "TestGoldens checked something" (chtypes#225) — never a second, freshly
    # guessed count. Emitted only under --require-artifacts: the no-artifact
    # job's TestGoldens skips wholesale and has nothing to count.
    checked_subtests = [t for t in passed if t.startswith("TestGoldens/")]
    if require:
        lines.append("chtypes-count golden-cases=%d" % len(checked_subtests))
    for t in skips:
        lines.append("    SKIPPED " + t)
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
    absent = [t for t in PARITY if t not in passed]
    if absent:
        problems.append(
            "the binding parity contract was not proven here: %s did not pass. It needs no "
            "artifact and no repository root, so there is no state in which it may be absent "
            "(tests/parity/manifest.json, docs/reference/bindings.md)" % ", ".join(absent)
        )
    if rc != 0 and not problems:
        problems.append("go test exited %d with no failing record; read %s" % (rc, log_path))
    if require:
        # The artifact-backed run's rule, read off the same census: the golden
        # set ran, and no test skipped for the one reason artifacts remove.
        if "TestGoldens" not in passed:
            problems.append("TestGoldens did not pass with artifacts required")
        # (chtypes#225) "TestGoldens" alone being in `passed` proves nothing
        # about whether a single case was actually checked: a registry and
        # golden set can both be present while every discovered version is
        # skipped (excluded by design, not generated on that line, a patch
        # mismatch) — the parent test then finishes normally, having run zero
        # per-case sub-tests, and reads exactly like a suite that proved
        # something. A real per-case sub-test is only ever reported under the
        # name "TestGoldens/<version>/<case id>" (Go's t.Run nesting), so at
        # least one of those, specifically, must have passed. This check is a
        # second, independent line of defense: go/chtypes/golden_test.go's own
        # assertAtLeastOneGoldenChecked already fails TestGoldens itself in
        # this situation (which the check above already catches), but this
        # census must not depend on that guard staying in place to say so.
        # `checked_subtests` is computed once, above (chtypes#285 §1b), so
        # the golden-case count this prints and the guard below can never
        # read a different set.
        if "TestGoldens" in passed and not checked_subtests:
            problems.append(
                "TestGoldens passed but checked zero cases — no subtest under TestGoldens/ passed "
                "(every version was skipped above); a golden pass must never be green having "
                "checked nothing"
            )
        starved = [t for t in skips if any("no chtypes artifacts under" in l for l in output.get(t, []))]
        if starved:
            problems.append(
                "%d test(s) skipped for want of a registry with artifacts required: %s"
                % (len(starved), ", ".join(starved))
            )
    # The ABI-revision handshake (#36). When $CHTYPES_ABI_FIXTURES names a
    # wrong-revision fixture set (scripts/abi-fixtures.sh), both halves must
    # have PASSED: a skipped or absent case reads exactly like a working
    # handshake.
    if abi_fixtures_set:
        for t in ("TestABIRevisionMismatchIsRefused", "TestABIRevisionControlLoads"):
            if t not in passed:
                problems.append("%s did not pass although $CHTYPES_ABI_FIXTURES is set" % t)

    if problems:
        lines.append("  VERDICT: NOT a pass —")
        for p in problems:
            lines.append("    * " + p)
        return 1, lines
    mode = "" if have_reg else " (no artifact registry: %d test(s) skipped by name above)" % skip
    lines.append("  VERDICT: pass — the dlopen-only SDK built, vetted and ran %d assertions from a bare copy%s" % (ran, mode))
    return 0, lines


def _parity_passed_events():
    return [{"Action": "pass", "Test": t} for t in PARITY]


def _write_log(path, events):
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def selftest():
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="chtypes-standalone-census-selftest-")
    try:
        # 1. A clean run: parity passed, TestGoldens (parent) passed, and one
        # real per-case subtest passed. require=1, have_reg=1. Must PASS.
        clean = os.path.join(tmp, "clean.json")
        _write_log(
            clean,
            _parity_passed_events()
            + [
                {"Action": "pass", "Test": "TestGoldens"},
                {"Action": "pass", "Test": "TestGoldens/25.8/case-1"},
            ],
        )
        code, lines = run_census(clean, 0, True, True, False)
        if code != 0:
            print(
                "SELFTEST FAILED: a clean run with a real checked case was refused:\n" + "\n".join(lines),
                file=sys.stderr,
            )
            return 1

        # 1b. THE PIN (chtypes#285 §1b): the two printed chtypes-count lines
        # are DERIVED from ran/skip/checked_subtests, not a second, hand-set
        # number — so this fails if that derivation ever changes without this
        # test noticing. The clean fixture above has 7 PARITY passes +
        # TestGoldens (parent) + one real per-case subtest, all passing, none
        # skipped: ran=9, skipped=0, one golden case checked.
        if "chtypes-count suite=go-artifacts ran=9 skipped=0" not in lines:
            print(
                "SELFTEST FAILED: the go-artifacts chtypes-count line did not read ran=9 skipped=0 off the "
                "same clean fixture:\n" + "\n".join(lines),
                file=sys.stderr,
            )
            return 1
        if "chtypes-count golden-cases=1" not in lines:
            print(
                "SELFTEST FAILED: the golden-cases chtypes-count line did not read 1 off the same clean "
                "fixture's one checked subtest:\n" + "\n".join(lines),
                file=sys.stderr,
            )
            return 1
        # Without --require-artifacts the label switches to go-no-artifacts
        # and the golden-cases line is not printed at all — the no-artifact
        # job's TestGoldens skips wholesale and has nothing to count, and a
        # line asserting 0 would be indistinguishable from a real zero-case
        # regression under --require-artifacts.
        _, no_artifact_lines = run_census(clean, 0, False, False, False)
        if "chtypes-count suite=go-no-artifacts ran=9 skipped=0" not in no_artifact_lines:
            print(
                "SELFTEST FAILED: the go-no-artifacts chtypes-count line did not read ran=9 skipped=0:\n"
                + "\n".join(no_artifact_lines),
                file=sys.stderr,
            )
            return 1
        if any(l.startswith("chtypes-count golden-cases=") for l in no_artifact_lines):
            print(
                "SELFTEST FAILED: a golden-cases line was printed without --require-artifacts",
                file=sys.stderr,
            )
            return 1

        # 2. THE PIN (chtypes#225): TestGoldens (the parent) PASSED, but every
        # version-level subtest SKIPPED, so no test named "TestGoldens/..." is
        # anywhere in `passed`. Before this guard existed, "TestGoldens" alone
        # being in `passed` was accepted as proof the golden set had run — a
        # registry and golden set can both be present and every version still
        # excluded or mismatched, checking zero cases while the parent test
        # itself reports pass. require=1 must REFUSE this.
        zero_checked = os.path.join(tmp, "zero_checked.json")
        _write_log(
            zero_checked,
            _parity_passed_events()
            + [
                {"Action": "pass", "Test": "TestGoldens"},
                {"Action": "skip", "Test": "TestGoldens/25.8"},
                {"Action": "skip", "Test": "TestGoldens/26.9"},
            ],
        )
        code, lines = run_census(zero_checked, 0, True, True, False)
        if code == 0:
            print(
                "SELFTEST FAILED: TestGoldens passed with every version-level subtest skipped and "
                "zero cases checked, and --require-artifacts accepted it (the zero-checked guard is "
                "gone)",
                file=sys.stderr,
            )
            return 1
        if not any("checked zero cases" in l for l in lines):
            print(
                "SELFTEST FAILED: the zero-checked run was refused for the wrong reason:\n" + "\n".join(lines),
                file=sys.stderr,
            )
            return 1

        # 3. Negative control on the guard's OWN condition: TestGoldens absent
        # from `passed` entirely (it failed outright) must still be caught by
        # the existing "TestGoldens did not pass" problem, and the new
        # zero-checked message must not also fire and confuse which failure
        # this is.
        not_passed = os.path.join(tmp, "not_passed.json")
        _write_log(not_passed, _parity_passed_events() + [{"Action": "fail", "Test": "TestGoldens"}])
        code, lines = run_census(not_passed, 1, True, True, False)
        if code == 0:
            print("SELFTEST FAILED: TestGoldens failing was accepted as a pass", file=sys.stderr)
            return 1
        if not any("TestGoldens did not pass" in l for l in lines):
            print(
                "SELFTEST FAILED: TestGoldens failing was refused for the wrong reason:\n" + "\n".join(lines),
                file=sys.stderr,
            )
            return 1
        if any("checked zero cases" in l for l in lines):
            print(
                "SELFTEST FAILED: the zero-checked message fired even though TestGoldens did not pass "
                "— it must only ever add detail when the parent itself is reported as a pass",
                file=sys.stderr,
            )
            return 1

        # 4. Without --require-artifacts the new check must stay silent: a
        # --no-artifacts run has no registry and every golden test skips by
        # name deliberately, which is the documented, honest shape.
        code, lines = run_census(zero_checked, 0, False, False, False)
        if code != 0:
            print(
                "SELFTEST FAILED: the zero-checked shape was refused without --require-artifacts:\n"
                + "\n".join(lines),
                file=sys.stderr,
            )
            return 1

        print(
            "check-standalone: selftest ok — a clean checked run passes, a parent that passed with "
            "every subtest skipped (checked zero cases) is refused under --require-artifacts "
            "(chtypes#225), a parent that outright failed is refused for its own reason without the "
            "new message masking it, the new check stays silent without --require-artifacts, and the "
            "chtypes-count suite/golden-cases lines (chtypes#285 §1b) are derived from the same "
            "ran/skip/checked_subtests, switch label without --require-artifacts, and drop the "
            "golden-cases line entirely there"
        )
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv):
    if argv[:1] == ["--selftest"]:
        return selftest()
    if len(argv) != 4:
        print("usage: standalone_census.py <log> <rc> <have_reg 0|1> <require 0|1>", file=sys.stderr)
        return 2
    log_path, rc_s, have_reg_s, require_s = argv
    abi_fixtures_set = bool(os.environ.get("CHTYPES_ABI_FIXTURES"))
    code, lines = run_census(log_path, int(rc_s), have_reg_s == "1", require_s == "1", abi_fixtures_set)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
