#!/usr/bin/env python3
"""parity.py: the v1-abi-parity merge gate — every ENROLLED binding passes
every case in tests/fixtures/abi-v1/cases.json, on every leg it declares, with
no leg missing, no leg stale against the current cases.json, no case failed,
and no extra case a report claims that the current cases.json does not
describe.

    scripts/abi-v1/parity.py                        check (0 enrolled is a vacuous pass)
    scripts/abi-v1/parity.py --reports-dir DIR       check against downloaded v1-abi-conformance reports
    scripts/abi-v1/parity.py --selftest              prove each of the four refusals fires, on a fabricated tree

ENROLLMENT. spec/abi-v1/enrolled/<binding> (go, python, ts or rust) marks
that binding's lane as landed. An empty file enrolls it with no declared
legs, so this gate requires at least one passing, non-stale, complete report
for it, any (toolchain, os); a non-empty file is JSON
`{"required_legs": [{"toolchain": "...", "os": "..."}, ...]}`, and every
listed leg must have its own report. Today spec/abi-v1/enrolled/ holds only
.gitkeep, so this prints a loud "0 enrolled" line and exits 0: GREEN BUT
VACUOUS, by design (the plan's F-B acceptance), until a binding lane adds its
own enrollment file.

REPORTS. --reports-dir points at a directory of per-leg JSON files matching
spec/abi-v1/schema/report.schema.json (one per v1-abi-conformance matrix
leg, downloaded as CI artifacts in the real workflow). Without it, this
treats zero reports as present, which only passes when nothing is enrolled.

PER-OS CASES. A case may carry `os` ("linux" or "darwin"). Each leg is expected
to report exactly the cases whose `os` is absent or whose report `os`
(linux-amd64, linux-arm64, darwin-arm64) starts with that value plus "-".

THE FOUR WAYS A CHECK REFUSES (the exact words --selftest proves each fires
for), matching the acceptance criterion verbatim:
  * MISSING  — an enrolled binding's required leg has no matching report.
  * FAILED   — a report exists for a required leg, but at least one of its
               results has "pass": false.
  * EXTRA    — a report's results name a case id the current cases.json does
               not contain, or one whose `os` does not match this leg (runners
               OMIT those; see CASE_OS_TO_REPORT_PREFIX) (the report was built against a superset, or a
               since-renamed case).
  * STALE    — a report's cases_sha256 does not match the current
               tests/fixtures/abi-v1/cases.json (compute_cases_hash): it was
               generated against a different case set, so its results prove
               nothing about THIS one, whether they happen to be all `pass:
               true` or not. A stale report that is also missing a case id
               the current set added is reported as stale, not as a second,
               redundant "failed" for the same underlying cause.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))

import model as abimodel  # noqa: E402

CASES_PATH = "tests/fixtures/abi-v1/cases.json"
CASES_SCHEMA = "spec/abi-v1/schema/cases.schema.json"
REPORT_SCHEMA = "spec/abi-v1/schema/report.schema.json"
ENROLLED_DIR = "spec/abi-v1/enrolled"
# cases.json's per-case `os` vocabulary ("linux", "darwin") onto a report's
# `os` ("<os>-<arch>": linux-amd64, linux-arm64, darwin-arm64): a case with
# os X applies to every report whose os starts with "X-". A case with no `os`
# applies on every leg. Runners OMIT a case whose os does not match their leg.
CASE_OS_TO_REPORT_PREFIX = {"linux": "linux-", "darwin": "darwin-"}
BINDINGS = ("go", "python", "ts", "rust")


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_schema_file(root: Path, schema_rel: str) -> dict:
    """Load a schema and refuse, loudly, if it uses a keyword
    model.py's validator does not implement — the same "a derivation checked
    against itself proves nothing" rule gen.py's selftest already applies to
    abi.json's own schemas, applied here to this script's two."""
    schema = _read_json(root / schema_rel)
    problems = abimodel.schema_keyword_problems(schema)
    if problems:
        raise SystemExit("\n".join(f"{schema_rel}: {p}" for p in problems))
    return schema


def compute_cases_hash(cases_doc: dict) -> str:
    """sha256 of the sorted, compact JSON of cases.json's own "cases" array —
    deliberately excluding "_generated" (a banner/fingerprint change alone
    must not make every report stale) and "schema" (a format bump without a
    case change is not a behavior change either)."""
    return hashlib.sha256(
        json.dumps(cases_doc["cases"], sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def load_cases(root: Path) -> tuple[dict, set[str], str]:
    cases_schema = _validate_schema_file(root, CASES_SCHEMA)
    doc = _read_json(root / CASES_PATH)
    errs = abimodel.validate(doc, cases_schema)
    if errs:
        raise SystemExit("\n".join(f"{CASES_PATH}: {e}" for e in errs))
    ids = {c["id"] for c in doc["cases"]}
    if len(ids) != len(doc["cases"]):
        raise SystemExit(f"{CASES_PATH}: duplicate case ids")
    return doc, ids, compute_cases_hash(doc)


def case_os_map(doc: dict) -> dict[str, str]:
    """case id -> its `os` ("linux" or "darwin"), for the cases that carry one."""
    return {c["id"]: c["os"] for c in doc["cases"] if "os" in c}


def expected_for_leg(cases_ids: set[str], case_os: dict[str, str], report_os: str) -> set[str]:
    """The case ids a leg on `report_os` must report: those with no `os`, or
    whose `os` maps (CASE_OS_TO_REPORT_PREFIX) onto this report's os."""
    out = set()
    for cid in cases_ids:
        o = case_os.get(cid)
        if o is None or report_os.startswith(CASE_OS_TO_REPORT_PREFIX[o]):
            out.add(cid)
    return out


def load_enrolled(root: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    d = root / ENROLLED_DIR
    for name in BINDINGS:
        p = d / name
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8").strip()
        out[name] = json.loads(text) if text else {"required_legs": []}
    return out


def load_reports(reports_dir: Path | None, report_schema: dict) -> list[dict]:
    if reports_dir is None or not reports_dir.is_dir():
        return []
    reports = []
    for p in sorted(reports_dir.glob("*.json")):
        doc = _read_json(p)
        errs = abimodel.validate(doc, report_schema)
        if errs:
            raise SystemExit("\n".join(f"{p}: {e}" for e in errs))
        reports.append(doc)
    return reports


def _leg_key(binding: str, toolchain: str, os_: str) -> tuple[str, str, str]:
    return (binding, toolchain, os_)


def check(
    enrolled: dict[str, dict],
    cases_ids: set[str],
    cases_hash: str,
    reports: list[dict],
    case_os: dict[str, str] | None = None,
) -> tuple[list[str], int]:
    """Return (problems, enrolled_count). An empty `problems` list is green —
    vacuously if enrolled_count is 0."""
    if not enrolled:
        return [], 0
    case_os = case_os or {}

    by_binding: dict[str, list[dict]] = {b: [] for b in enrolled}
    for r in reports:
        if r["binding"] in by_binding:
            by_binding[r["binding"]].append(r)

    problems: list[str] = []
    for binding, meta in sorted(enrolled.items()):
        required = meta.get("required_legs") or []
        reports_here = by_binding[binding]
        if not required:
            # No declared legs: at least one clean, current report for this
            # binding, any leg, is required.
            if not reports_here:
                problems.append(f"MISSING: {binding} is enrolled but no report was found for it")
                continue
            candidates = reports_here
        else:
            candidates = []
            for leg in required:
                matches = [
                    r
                    for r in reports_here
                    if r["toolchain"] == leg["toolchain"] and r["os"] == leg["os"]
                ]
                if not matches:
                    problems.append(
                        f"MISSING: {binding} {leg['toolchain']} {leg['os']}: no report found for a required leg"
                    )
                    continue
                candidates += matches

        for r in candidates:
            leg = f"{r['binding']} {r['toolchain']} {r['os']}"
            if r["cases_sha256"] != cases_hash:
                problems.append(
                    f"STALE: {leg}: report cases_sha256 {r['cases_sha256']} != current {cases_hash} "
                    f"({CASES_PATH} changed since this report was generated)"
                )
                continue
            result_ids = {res["id"] for res in r["results"]}
            expected = expected_for_leg(cases_ids, case_os, r["os"])
            extra = result_ids - expected
            if extra:
                problems.append(
                    f"EXTRA: {leg}: report names case(s) not expected on this leg (absent from {CASES_PATH} "
                    f"or for another os): {sorted(extra)}"
                )
            missing_cases = expected - result_ids
            if missing_cases:
                problems.append(f"MISSING: {leg}: report has no result for case(s): {sorted(missing_cases)}")
            failed = sorted(res["id"] for res in r["results"] if not res["pass"])
            if failed:
                problems.append(f"FAILED: {leg}: case(s) did not pass: {failed}")
    return problems, len(enrolled)


def run(root: Path, reports_dir: Path | None) -> int:
    report_schema = _validate_schema_file(root, REPORT_SCHEMA)
    cases_doc, cases_ids, cases_hash = load_cases(root)
    enrolled = load_enrolled(root)
    reports = load_reports(reports_dir, report_schema)
    problems, n = check(enrolled, cases_ids, cases_hash, reports, case_os_map(cases_doc))
    if n == 0:
        print(
            "v1-abi-parity: 0 ENROLLED bindings — vacuously green. No binding lane has landed yet "
            f"(spec/abi-v1/enrolled/ holds only .gitkeep); {len(cases_ids)} cases are described in {CASES_PATH} "
            "waiting for the first one."
        )
        return 0
    for p in problems:
        print(f"v1-abi-parity: {p}", file=sys.stderr)
    if problems:
        print(f"v1-abi-parity: {len(problems)} problem(s) across {n} enrolled binding(s)", file=sys.stderr)
        return 1
    print(f"v1-abi-parity: ok: {n} enrolled binding(s), {len(cases_ids)} cases, every required leg passes")
    return 0


# ------------------------------------------------------------------ selftest


def _write(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")


def _fake_cases() -> dict:
    return {
        "_generated": "GENERATED by scripts/abi-v1/gen.py from spec/abi-v1/abi.json (CHS_ABI_FINGERPRINT sha256:"
        + "0" * 64
        + ") — DO NOT EDIT",
        "schema": 1,
        "cases": [
            {"id": "a.echo", "kind": "echo", "fn": "chs_a", "args": [], "expect": {"status": "CHS_OK"}},
            {"id": "b.echo", "kind": "echo", "fn": "chs_b", "args": [], "expect": {"status": "CHS_OK"}},
        ],
    }


def _good_report(cases_hash: str, binding: str = "go", toolchain: str = "go.mod", os_: str = "linux-amd64") -> dict:
    return {
        "schema": 1,
        "binding": binding,
        "toolchain": toolchain,
        "os": os_,
        "cases_sha256": cases_hash,
        "results": [{"id": "a.echo", "pass": True}, {"id": "b.echo", "pass": True}],
    }


def selftest() -> int:
    fails: list[str] = []
    with tempfile.TemporaryDirectory(prefix="abi-v1-parity-selftest-") as tmp:
        root = Path(tmp)
        for rel in (
            "spec/abi-v1",
            "scripts/abi-v1",
            "scripts/policy-merge-check.py",
        ):
            src = ROOT / rel
            dst = root / rel
            if src.is_dir():
                import shutil

                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
            elif src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                import shutil

                shutil.copy2(src, dst)

        # Start from an EMPTY enrolled/ directory, independent of whatever is
        # actually enrolled in the live tree. The copytree above just carried
        # over spec/abi-v1/enrolled/ as it exists for real right now (only
        # .gitkeep today) — but once any binding lane lands for real, this
        # fixture would silently start including its genuine enrollment file
        # too, and "exactly one enrolled" / "clean report must pass" below
        # would begin failing or passing for the wrong reason the day a real
        # lane merges, not because of anything this selftest actually tests.
        # Every case below enrolls (or doesn't) exactly what IT needs via
        # enroll(), never the live tree's own state.
        import shutil

        enrolled_dir = root / ENROLLED_DIR
        if enrolled_dir.is_dir():
            shutil.rmtree(enrolled_dir)
        enrolled_dir.mkdir(parents=True)

        cases = _fake_cases()
        _write(root / CASES_PATH, cases)
        cases_hash = compute_cases_hash(cases)

        def enroll(required_legs=()) -> None:
            (root / ENROLLED_DIR).mkdir(parents=True, exist_ok=True)
            _write(root / ENROLLED_DIR / "go", {"required_legs": list(required_legs)})

        def reports_dir_with(*docs) -> Path:
            d = root / "reports"
            if d.is_dir():
                import shutil

                shutil.rmtree(d)
            d.mkdir()
            for i, doc in enumerate(docs):
                _write(d / f"r{i}.json", doc)
            return d

        # 1. Vacuous: nothing enrolled.
        if (root / ENROLLED_DIR / "go").exists():
            (root / ENROLLED_DIR / "go").unlink()
        problems, n = check({}, {"a.echo", "b.echo"}, cases_hash, [])
        if problems or n != 0:
            fails.append(f"vacuous (0 enrolled) case was not green: {problems}")

        # 2. A clean report: must pass.
        enroll([{"toolchain": "go.mod", "os": "linux-amd64"}])
        enrolled = load_enrolled(root)
        problems, n = check(enrolled, {"a.echo", "b.echo"}, cases_hash, [_good_report(cases_hash)])
        if problems or n != 1:
            fails.append(f"a clean, matching report was refused: {problems}")

        # 3. MISSING: the required leg has no report at all.
        problems, _ = check(enrolled, {"a.echo", "b.echo"}, cases_hash, [])
        if not any(p.startswith("MISSING") for p in problems):
            fails.append(f"a missing required leg was not caught: {problems}")

        # 4. FAILED: a report exists but one case did not pass.
        bad = _good_report(cases_hash)
        bad["results"][0]["pass"] = False
        problems, _ = check(enrolled, {"a.echo", "b.echo"}, cases_hash, [bad])
        if not any(p.startswith("FAILED") for p in problems):
            fails.append(f"a failed case was not caught: {problems}")

        # 5. EXTRA: a report names a case the current cases.json does not have.
        extra = _good_report(cases_hash)
        extra["results"].append({"id": "c.echo.gone", "pass": True})
        problems, _ = check(enrolled, {"a.echo", "b.echo"}, cases_hash, [extra])
        if not any(p.startswith("EXTRA") for p in problems):
            fails.append(f"an extra case id was not caught: {problems}")

        # 6. STALE: cases_sha256 does not match the current cases.json.
        stale = _good_report(cases_hash)
        stale["cases_sha256"] = "1" * 64
        problems, _ = check(enrolled, {"a.echo", "b.echo"}, cases_hash, [stale])
        if not any(p.startswith("STALE") for p in problems):
            fails.append(f"a stale cases_sha256 was not caught: {problems}")

        # 7. Per-OS cases: a leg reports only the cases whose os is absent or
        #    matches. Fake set: a.echo (all), l.only (linux), d.only (darwin).
        osids = {"a.echo", "l.only", "d.only"}
        osmap = {"l.only": "linux", "d.only": "darwin"}

        def os_report(os_, ids):
            r = _good_report(cases_hash, os_=os_)
            r["results"] = [{"id": i, "pass": True} for i in ids]
            return r

        for os_, want in (("linux-amd64", ["a.echo", "l.only"]), ("linux-arm64", ["a.echo", "l.only"]),
                          ("darwin-arm64", ["a.echo", "d.only"])):
            enroll([{"toolchain": "go.mod", "os": os_}])
            en = load_enrolled(root)
            problems, _ = check(en, osids, cases_hash, [os_report(os_, want)], osmap)
            if problems:
                fails.append(f"{os_}: a report with exactly its matching cases was refused: {problems}")
        enroll([{"toolchain": "go.mod", "os": "darwin-arm64"}])
        en = load_enrolled(root)
        problems, _ = check(en, osids, cases_hash, [os_report("darwin-arm64", ["a.echo", "d.only", "l.only"])], osmap)
        if not any(p.startswith("EXTRA") and "l.only" in p for p in problems):
            fails.append(f"a mismatched-os case present in a report was not EXTRA: {problems}")
        problems, _ = check(en, osids, cases_hash, [os_report("darwin-arm64", ["a.echo"])], osmap)
        if not any(p.startswith("MISSING") and "d.only" in p for p in problems):
            fails.append(f"a matching-os case absent from a report was not MISSING: {problems}")

        # run() end-to-end, through the real schemas, for the vacuous and the
        # MISSING cases (proves the CLI path, not just check()).
        if (root / ENROLLED_DIR / "go").exists():
            (root / ENROLLED_DIR / "go").unlink()
        if run(root, None) != 0:
            fails.append("run(): the vacuous (0 enrolled) case did not exit 0")
        enroll([{"toolchain": "go.mod", "os": "linux-amd64"}])
        if run(root, reports_dir_with()) == 0:
            fails.append("run(): a MISSING leg did not exit nonzero")

    for f in fails:
        print(f"parity.py --selftest: FAIL {f}", file=sys.stderr)
    if fails:
        return 1
    print(
        "parity.py --selftest: ok: vacuous-0-enrolled is green, a clean report passes, and MISSING/FAILED/EXTRA/"
        "STALE each refuse, and per-os cases are expected only on their own leg (omitted = ok, absent match = MISSING, present mismatch = EXTRA)"
    )
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reports-dir", type=Path, default=None, help="directory of per-leg report JSON files")
    ap.add_argument("--selftest", action="store_true", help="prove each refusal fires, on a fabricated tree")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    return run(ROOT, args.reports_dir)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
