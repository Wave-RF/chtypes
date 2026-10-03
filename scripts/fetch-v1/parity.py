#!/usr/bin/env python3
r"""scripts/fetch-v1/parity.py — the v1 fetch layer's merge gate (plan
§3.3, docs/guides/fetch-v1.md §10): every ENROLLED binding passes every
conformance case, on every toolchain leg, with the registry transport
covered by the v1-network job. Python stdlib only.

    scripts/fetch-v1/parity.py --selftest
        proves this script's own failure modes on fabricated, in-memory
        report fixtures — never files under tests/fixtures/fetch-v1/ —
        the same discipline every other --selftest in this repository
        follows: missing report, a failed case, an extra (case, transport)
        pair, a stale cases_sha256, a shrunk enrolled set, and the "0
        enrolled" vacuous pass are each independently proven to behave as
        this script's own docstring claims.

    scripts/fetch-v1/parity.py --reports-dir <dir> [--cases <path>]
                                [--enrolled-dir <path>] [--base-ref <ref>]
        the real check, run by the `v1-parity` job: <dir> holds every
        report.schema.json file every v1-conformance toolchain leg AND the
        v1-network job uploaded (as `report-<binding>-<toolchain>.json`;
        the network job's own report uses the sentinel toolchain name
        "registry" — see TOOLCHAINS below).

ENROLLMENT. spec/fetch-v1/enrolled/{go,python,ts,rust} — one empty marker
file per binding, added by that binding's own lane (plan §3.3). A binding
with no marker file is "not enrolled (allowed only on v1)"; the switch
lane is what first requires all four.

WHAT "PASS" MEANS, for every enrolled binding:

  1. a report exists for EVERY toolchain leg TOOLCHAINS below lists for
     that binding (this list mirrors .github/workflows/v1.yml's
     `v1-conformance` matrix EXACTLY — lane 0B's own MERGE NOTES flag this
     coupling for the integration step to keep in sync if that matrix ever
     changes, since no single file is the source of truth for both);
  2. every one of those reports' `cases_sha256` equals the sha256 of the
     cases.json this run was given (a STALE report — built against an
     older case table — never counts, even if every result in it says
     "pass");
  3. every (case, transport) pair in cases.json whose transport is "file"
     or "http" is present, with verdict "pass", across that binding's
     toolchain-leg reports, with no extras (a toolchain-leg report is
     never expected to attempt the "registry" transport, so one is never
     an "extra" relative to those reports alone — see point 4);
  4. every (case, transport="registry") pair is present, with verdict
     "pass", in some report for that binding whose toolchain is exactly
     "registry" (the v1-network job's own report).

The enrolled set may only grow between any two runs this script is told
to compare (`--base-ref`, default "origin/v1"): a binding disappearing
from spec/fetch-v1/enrolled/ is refused outright, never silently treated
as "that binding opted out". With zero bindings enrolled, this script
still parses cases.json and validates its own internal consistency, prints
a LOUD "0 enrolled" line, and exits 0 — vacuously green, not skipped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CASES = ROOT / "tests" / "fixtures" / "fetch-v1" / "cases.json"
DEFAULT_ENROLLED_DIR = ROOT / "spec" / "fetch-v1" / "enrolled"
BINDINGS = ("go", "python", "ts", "rust")

# Mirrors .github/workflows/v1.yml's `v1-conformance` matrix. Keep in sync
# by hand; see the module docstring.
TOOLCHAINS: dict[str, tuple[str, ...]] = {
    "go": ("go1.27.x",),
    "python": ("3.11", "3.13", "3.14"),
    "ts": ("node22.21.0", "node24"),
    "rust": ("1.87", "stable"),  # PM's MSRV ruling (v1.yml's own comment); was 1.85
}
NETWORK_TOOLCHAIN = "registry"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_cases(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def enrolled_bindings(enrolled_dir: Path) -> set[str]:
    if not enrolled_dir.is_dir():
        return set()
    return {p.name for p in enrolled_dir.iterdir() if p.is_file() and p.name in BINDINGS}


def enrolled_at_ref(ref: str) -> set[str] | None:
    """Returns the enrolled set spec/fetch-v1/enrolled/ held at ref, or
    None if that ref cannot be read (no git, no such ref, shallow clone
    missing it, ...) — a soft failure: this check is a refinement, never
    the reason the whole gate cannot run."""
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "ls-tree", "--name-only", f"{ref}:spec/fetch-v1/enrolled"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return {line.strip() for line in out.stdout.splitlines() if line.strip() in BINDINGS}


def load_reports(reports_dir: Path) -> list[dict[str, Any]]:
    reports = []
    if not reports_dir.is_dir():
        return reports
    for p in sorted(reports_dir.glob("*.json")):
        try:
            reports.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError as e:
            reports.append({"_parse_error": f"{p}: {e}"})
    return reports


def case_transport_pairs(cases_doc: dict[str, Any]) -> set[tuple[str, str]]:
    return {(c["id"], t) for c in cases_doc["cases"] for t in c["transports"]}


def check_binding(
    binding: str,
    reports: list[dict[str, Any]],
    cases_sha256: str,
    required_pairs: set[tuple[str, str]],
) -> list[str]:
    problems: list[str] = []
    own_reports = [r for r in reports if r.get("binding") == binding and "_parse_error" not in r]

    for toolchain in TOOLCHAINS[binding]:
        matching = [r for r in own_reports if r.get("toolchain") == toolchain]
        if not matching:
            problems.append(f"{binding}: missing report for toolchain {toolchain!r}")
            continue
        for r in matching:
            if r.get("cases_sha256") != cases_sha256:
                problems.append(
                    f"{binding}/{toolchain}: cases_sha256 {r.get('cases_sha256')!r} does not match the head's "
                    f"cases.json ({cases_sha256!r}) — stale report"
                )

    non_registry_required = {(cid, t) for cid, t in required_pairs if t != "registry"}
    registry_required = {(cid, t) for cid, t in required_pairs if t == "registry"}

    toolchain_reports = [r for r in own_reports if r.get("toolchain") in TOOLCHAINS[binding]]
    problems += _check_pairs(f"{binding} (toolchain legs)", toolchain_reports, non_registry_required, allow_registry=False)

    network_reports = [r for r in own_reports if r.get("toolchain") == NETWORK_TOOLCHAIN]
    if registry_required and not network_reports:
        problems.append(f"{binding}: registry-transport cases are required but no report with toolchain "
                         f"{NETWORK_TOOLCHAIN!r} (the v1-network job) was found")
    else:
        for r in network_reports:
            if r.get("cases_sha256") != cases_sha256:
                problems.append(
                    f"{binding}/{NETWORK_TOOLCHAIN}: cases_sha256 {r.get('cases_sha256')!r} does not match the "
                    f"head's cases.json — stale report"
                )
        problems += _check_pairs(f"{binding} (registry)", network_reports, registry_required, allow_registry=True)

    return problems


def _check_pairs(label: str, reports: list[dict[str, Any]], required: set[tuple[str, str]], allow_registry: bool) -> list[str]:
    problems: list[str] = []
    seen: dict[tuple[str, str], list[str]] = {}
    for r in reports:
        for result in r.get("results", []):
            pair = (result.get("id"), result.get("transport"))
            seen.setdefault(pair, []).append(result.get("verdict", "?"))

    missing = sorted(p for p in required if p not in seen)
    for p in missing:
        problems.append(f"{label}: missing result for case {p[0]!r} transport {p[1]!r}")

    for pair, verdicts in seen.items():
        if pair not in required:
            if not allow_registry and pair[1] == "registry":
                continue  # toolchain-leg reports never attempt registry; not an "extra" to flag twice
            problems.append(f"{label}: extra result not in cases.json: case {pair[0]!r} transport {pair[1]!r}")
            continue
        if any(v != "pass" for v in verdicts):
            problems.append(f"{label}: case {pair[0]!r} transport {pair[1]!r} did not pass: {verdicts}")

    return problems


def run(cases_path: Path, enrolled_dir: Path, reports_dir: Path, base_ref: str | None) -> tuple[int, str]:
    lines: list[str] = []
    cases_doc = load_cases(cases_path)
    cases_sha256 = sha256_of(cases_path)
    required_pairs = case_transport_pairs(cases_doc)

    enrolled = enrolled_bindings(enrolled_dir)
    lines.append(f"cases.json: {len(cases_doc['cases'])} cases, sha256 {cases_sha256}")
    lines.append(f"enrolled: {sorted(enrolled) if enrolled else '(none)'}")

    if base_ref:
        base_enrolled = enrolled_at_ref(base_ref)
        if base_enrolled is None:
            lines.append(f"enrolled-shrink check: could not read {base_ref}:spec/fetch-v1/enrolled (skipped)")
        else:
            shrunk = base_enrolled - enrolled
            if shrunk:
                return 1, "\n".join(lines) + f"\nFAIL: binding(s) {sorted(shrunk)} were enrolled at {base_ref} " \
                                               "and are not enrolled now — a binding may never be un-enrolled silently"
            lines.append(f"enrolled-shrink check: {sorted(base_enrolled)} at {base_ref}, no shrink")

    if not enrolled:
        lines.append("")
        lines.append("v1-parity: 0 ENROLLED — vacuously green. No binding has added "
                      "spec/fetch-v1/enrolled/<name> yet; this run only proves cases.json "
                      "itself is well-formed.")
        lines.append(_summary_table(cases_doc, enrolled, {}))
        return 0, "\n".join(lines)

    reports = load_reports(reports_dir)
    parse_errors = [r["_parse_error"] for r in reports if "_parse_error" in r]
    for e in parse_errors:
        lines.append(f"FAIL: unreadable report: {e}")

    all_problems: list[str] = list(parse_errors)
    per_binding_pass: dict[str, bool] = {}
    for binding in sorted(enrolled):
        problems = check_binding(binding, reports, cases_sha256, required_pairs)
        per_binding_pass[binding] = not problems
        if problems:
            lines.append(f"{binding}: FAIL")
            for p in problems:
                lines.append(f"  {p}")
        else:
            lines.append(f"{binding}: pass ({len(TOOLCHAINS[binding])} toolchain leg(s) "
                         f"+ registry: {'yes' if any(t == 'registry' for _, t in required_pairs) else 'n/a'})")
        all_problems += problems

    lines.append(_summary_table(cases_doc, enrolled, per_binding_pass))
    return (1 if all_problems else 0), "\n".join(lines)


def _summary_table(cases_doc: dict[str, Any], enrolled: set[str], per_binding_pass: dict[str, bool]) -> str:
    rows = ["", "case x binding summary:", f"{'case':<42}" + "".join(f"{b:>10}" for b in BINDINGS)]
    for c in cases_doc["cases"]:
        row = f"{c['id']:<42}"
        for b in BINDINGS:
            if b not in enrolled:
                row += f"{'-':>10}"
            else:
                row += f"{'ok' if per_binding_pass.get(b) else 'FAIL':>10}"
        rows.append(row)
    return "\n".join(rows)


def selftest() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cases_path = tmp / "cases.json"
        cases_doc = {
            "schema": 1,
            "source": {"generator_commit": "0" * 40, "go_sum_sha256": "0" * 64},
            "cases": [
                {"id": "a", "transports": ["file", "http"]},
                {"id": "b", "transports": ["file", "http", "registry"]},
            ],
        }
        cases_path.write_text(json.dumps(cases_doc))
        cases_sha = sha256_of(cases_path)

        enrolled_dir = tmp / "enrolled"
        enrolled_dir.mkdir()

        def report(binding, toolchain, results, sha=None):
            return {
                "schema": 1, "binding": binding, "toolchain": toolchain,
                "cases_sha256": sha or cases_sha,
                "results": [{"id": cid, "transport": t, "verdict": v, "detail": ""} for cid, t, v in results],
            }

        def full_pass_reports(binding):
            out = [report(binding, tc, [("a", "file", "pass"), ("a", "http", "pass"), ("b", "file", "pass"), ("b", "http", "pass")])
                   for tc in TOOLCHAINS[binding]]
            out.append(report(binding, "registry", [("b", "registry", "pass")]))
            return out

        # 0 enrolled: vacuously green.
        code, out = run(cases_path, enrolled_dir, tmp / "reports-empty", None)
        assert code == 0, f"0-enrolled run should be green:\n{out}"
        assert "0 ENROLLED" in out

        # a clean "go" enrollment, fully passing.
        (enrolled_dir / "go").touch()
        reports_dir = tmp / "reports-ok"
        reports_dir.mkdir()
        for i, r in enumerate(full_pass_reports("go")):
            (reports_dir / f"r{i}.json").write_text(json.dumps(r))
        code, out = run(cases_path, enrolled_dir, reports_dir, None)
        assert code == 0, f"a fully-passing enrolled binding should be green:\n{out}"

        # missing report: drop the go1.27.x leg.
        reports_dir2 = tmp / "reports-missing"
        reports_dir2.mkdir()
        for i, r in enumerate(full_pass_reports("go")[1:]):  # drop index 0 (the only toolchain leg for go)
            (reports_dir2 / f"r{i}.json").write_text(json.dumps(r))
        code, out = run(cases_path, enrolled_dir, reports_dir2, None)
        assert code != 0 and "missing report" in out, f"a missing report should fail:\n{out}"

        # failed case.
        reports_dir3 = tmp / "reports-failed"
        reports_dir3.mkdir()
        bad = full_pass_reports("go")
        bad[0]["results"][0]["verdict"] = "fail"
        for i, r in enumerate(bad):
            (reports_dir3 / f"r{i}.json").write_text(json.dumps(r))
        code, out = run(cases_path, enrolled_dir, reports_dir3, None)
        assert code != 0 and "did not pass" in out, f"a failed case should fail:\n{out}"

        # extra (case, transport) pair not in cases.json.
        reports_dir4 = tmp / "reports-extra"
        reports_dir4.mkdir()
        extra = full_pass_reports("go")
        extra[0]["results"].append({"id": "nonexistent-case", "transport": "file", "verdict": "pass", "detail": ""})
        for i, r in enumerate(extra):
            (reports_dir4 / f"r{i}.json").write_text(json.dumps(r))
        code, out = run(cases_path, enrolled_dir, reports_dir4, None)
        assert code != 0 and "extra result" in out, f"an extra result should fail:\n{out}"

        # stale cases_sha256.
        reports_dir5 = tmp / "reports-stale"
        reports_dir5.mkdir()
        stale = full_pass_reports("go")
        stale[0] = report("go", TOOLCHAINS["go"][0], [("a", "file", "pass"), ("a", "http", "pass"),
                                                        ("b", "file", "pass"), ("b", "http", "pass")],
                           sha="f" * 64)
        for i, r in enumerate(stale):
            (reports_dir5 / f"r{i}.json").write_text(json.dumps(r))
        code, out = run(cases_path, enrolled_dir, reports_dir5, None)
        assert code != 0 and "stale report" in out, f"a stale cases_sha256 should fail:\n{out}"

        # enrolled-shrink: base had "go" and "python"; now only "go".
        def fake_enrolled_at_ref(ref: str) -> set[str] | None:
            return {"go", "python"}

        global enrolled_at_ref
        original = enrolled_at_ref
        enrolled_at_ref = fake_enrolled_at_ref  # type: ignore[assignment]
        try:
            code, out = run(cases_path, enrolled_dir, reports_dir, "fake-ref")
            assert code != 0 and "never be un-enrolled" in out, f"a shrunk enrolled set should fail:\n{out}"
        finally:
            enrolled_at_ref = original  # type: ignore[assignment]

    print("parity.py --selftest: OK")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    ap.add_argument("--enrolled-dir", type=Path, default=DEFAULT_ENROLLED_DIR)
    ap.add_argument("--reports-dir", type=Path)
    ap.add_argument("--base-ref", default="origin/v1")
    ap.add_argument("--no-base-ref", action="store_true", help="skip the enrolled-shrink check entirely")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return 0

    if not args.reports_dir:
        print("parity.py: --reports-dir is required (unless --selftest)", file=sys.stderr)
        return 2

    code, out = run(args.cases, args.enrolled_dir, args.reports_dir, None if args.no_base_ref else args.base_ref)
    print(out)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("```\n" + out + "\n```\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
