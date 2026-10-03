#!/usr/bin/env python3
r"""scripts/fetch-v1/staging.py — the staging conformance run's case table and
verdict (docs/guides/fetch-v1.md §10, "The staging run"). Python stdlib only.

    scripts/fetch-v1/staging.py --selftest
    scripts/fetch-v1/staging.py emit --out <dir> --trust-test-key <hex>
    scripts/fetch-v1/staging.py verdict --derived <dir> --reports-dir <dir>

WHAT IT IS FOR. `.github/workflows/v1.yml`'s dispatch-only `v1-staging` job
points each ENROLLED binding's conformance runner at a live staging registry
(`registry_base`) instead of the loopback server. A real host is not the
scripted server, so the full case table cannot run against it. `emit`
derives the subset that can, and `verdict` reads what the runners reported.

`emit` writes a fixtures directory a runner can use UNCHANGED as
`CHTYPES_V1_CONFORMANCE`: every entry of tests/fixtures/fetch-v1/ is
symlinked in except `cases.json`, which is replaced by the derived table (every
included case with `transports: ["registry"]` only), plus one sidecar,
`staging-manifest.json`, that is NOT part of the case schema (runners never
read it): which cases were included and why each other one was not, the notes,
and the trust opt-in. The runner then runs exactly those cases against
`CHTYPES_V1_REGISTRY_BASE`.

WHICH CASES APPLY (the rules in `classify`, each exclusion names its rule). A
case applies when the real host can answer it with nothing scripted: it runs
over the staged fixture tree (STAGED_TREES), has no http script (nothing
scripted to misbehave), starts from an empty cache with no lock, no hook, no
environment and a single `{base}`, asks for the plain online resolve, trusts
the SDK TEST key, and asserts nothing about the request log (a real host has
no `/_log`). Everything else is excluded LOUDLY, one line per case, in the
sidecar and in the job summary.

TRUST. The SDK TEST key is never trusted by default. `emit` refuses to write
anything unless it is given the test key's own public hex on the command line
(`--trust-test-key`), and that value must equal
tests/fixtures/fetch-v1/test-key/public.hex. The opt-in is therefore an
explicit argument in the workflow step, recorded in the sidecar, and absent
from every other job.

PER-CASE DIFFERENCES. `OVERRIDES` maps a case id to `{"expect": {...},
"reason": "..."}` for a case whose expected outcome differs on the real host;
`emit` applies it to the derived table and the verdict prints the reason next
to that case. `NOTES` records a wire difference that does NOT change the
expected outcome. Nothing is ever silently adjusted.

THE VERDICT IS INFORMATION. It goes to the job summary and the exit status is
0 whatever it says; the dispatch-only job is never a required context and
never reads or feeds `parity.py`.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "fetch-v1"
ENROLLED_DIR = ROOT / "spec" / "fetch-v1" / "enrolled"
BINDINGS = ("go", "python", "ts", "rust")
SIDECAR = "staging-manifest.json"

# The fixture trees the delivery side staged on the staging host. `basic` is
# the only one; every case over another tree needs data that host does not hold.
STAGED_TREES = frozenset({"basic"})

# case id -> {"expect": {<fields of the case's `expect` object>}, "reason": str}.
# Empty today: every included case's expected outcome is the same on the host.
OVERRIDES: dict[str, dict[str, Any]] = {}

# case id -> a wire difference worth reading next to the verdict that does not
# change the expected outcome. Labels follow the repository convention.
NOTES: dict[str, str] = {
    "missing-platform": (
        "wire differs, outcome should not: the host serves no partial set, so tag 26.6 is a 404 "
        "(measured 2026-10-03), where the fixture tree serves a 200 index that omits the platform; "
        "a tag 404 on the only base is CHTYPES_ARTIFACT_UNPUBLISHED (§7), so the expected code is "
        "unchanged (inferred; the first run settles it)"
    ),
    "unpublished-line": "the host answers an unpublished line's tag with a 404, like the fixture tree",
}


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def classify(case: dict[str, Any]) -> tuple[bool, str]:
    """(applies, rule) — the rule names why a case was excluded, or says
    that it was included."""
    cid = case["id"]
    req, setup, exp = case["request"], case["setup"], case["expect"]
    if case["tree"] not in STAGED_TREES:
        return False, f"rule 1: tree {case['tree']!r} is not a staged tree"
    if cid.startswith(("goldens-", "fixtures-")):
        return False, "rule 2: a generic-fetch case needs artifacts the host does not hold"
    if case.get("http_script") is not None:
        return False, "rule 3: needs a scripted server response; a real host cannot misbehave on cue"
    if setup["cache"] != "empty" or setup["system_dirs"] or setup["lock"] is not None or setup["before_index_rename_hook"] is not None:
        return False, "rule 4: needs a seeded cache, system directory, lock or hook"
    if case["env"]:
        return False, "rule 5: sets environment variables for the client"
    if req["bases"] != ["{base}"]:
        return False, "rule 6: not exactly one {base}"
    if req["offline"] or req["frozen"] or req["lock_write"] or req["update"] or req["allow_unsigned"]:
        return False, "rule 7: not the plain online resolve"
    if req["trust"] != "test":
        return False, "rule 8: does not trust the SDK test key (the staged fixtures are signed with it)"
    rq = exp["requests"]
    if rq["max"] is not None or rq["none_matching"] or rq["auth_on_second_origin"]:
        return False, "rule 9: asserts on the request log, which a real host does not expose"
    return True, "included"


def derive(cases_doc: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, str]]]:
    derived = copy.deepcopy(cases_doc)
    kept: list[dict[str, Any]] = []
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for case in cases_doc["cases"]:
        ok, rule = classify(case)
        if not ok:
            excluded.append({"id": case["id"], "reason": rule})
            continue
        c = copy.deepcopy(case)
        c["transports"] = ["registry"]
        entry: dict[str, Any] = {"id": c["id"]}
        ov = OVERRIDES.get(c["id"])
        if ov:
            c["expect"].update(ov["expect"])
            entry["override"] = {"expect": ov["expect"], "reason": ov["reason"]}
        if c["id"] in NOTES:
            entry["note"] = NOTES[c["id"]]
        kept.append(c)
        included.append(entry)
    derived["cases"] = kept
    return derived, included, excluded


def emit(out: Path, fixtures: Path, trust_hex: str) -> dict[str, Any]:
    want = (fixtures / "test-key" / "public.hex").read_text(encoding="utf-8").strip()
    if trust_hex.strip() != want:
        raise SystemExit(
            "staging.py: --trust-test-key must be the SDK test key's own public hex "
            f"({want[:8]}…); refusing to emit a table that trusts anything else."
        )
    cases_path = fixtures / "cases.json"
    doc = json.loads(cases_path.read_text(encoding="utf-8"))
    derived, included, excluded = derive(doc)
    if not derived["cases"]:
        raise SystemExit("staging.py: no case applies to a staging host; refusing to emit an empty table")
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"staging.py: {out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    for entry in sorted(fixtures.iterdir()):
        if entry.name == "cases.json":
            continue
        (out / entry.name).symlink_to(entry.resolve())
    cases_out = out / "cases.json"
    cases_out.write_text(json.dumps(derived, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "schema": 1,
        "source_cases_sha256": sha256_of(cases_path),
        "derived_cases_sha256": sha256_of(cases_out),
        "trust_test_key": want,
        "included": included,
        "excluded": excluded,
    }
    (out / SIDECAR).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    _validate_against_schema(cases_out)
    return manifest


def _validate_against_schema(cases_out: Path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import schema_check  # type: ignore[import-not-found]

    problems = schema_check.validate_file(cases_out, schema_check.load_schema("cases.schema.json"))
    if problems:
        raise SystemExit("staging.py: the derived cases.json is not schema-valid:\n  " + "\n  ".join(problems))


def enrolled_bindings(enrolled_dir: Path) -> set[str]:
    if not enrolled_dir.is_dir():
        return set()
    return {p.name for p in enrolled_dir.iterdir() if p.is_file() and p.name in BINDINGS}


def verdict(derived_dir: Path, reports_dir: Path, enrolled_dir: Path, registry_base: str = "") -> str:
    manifest = json.loads((derived_dir / SIDECAR).read_text(encoding="utf-8"))
    derived_sha = manifest["derived_cases_sha256"]
    ids = [e["id"] for e in manifest["included"]]
    notes = {e["id"]: [x for x in (e.get("note"), (e.get("override") or {}).get("reason")) if x] for e in manifest["included"]}
    enrolled = enrolled_bindings(enrolled_dir)

    reports: list[dict[str, Any]] = []
    unreadable: list[str] = []
    if reports_dir.is_dir():
        for p in sorted(reports_dir.glob("*.json")):
            try:
                reports.append(json.loads(p.read_text(encoding="utf-8")))
            except json.JSONDecodeError as e:
                unreadable.append(f"{p.name}: {e}")

    lines = ["### v1-staging verdict (information only; never gates a PR)", ""]
    if registry_base:
        lines.append(f"registry base: `{registry_base}`")
    lines.append(f"derived cases: {len(ids)} of {len(ids) + len(manifest['excluded'])}, sha256 `{derived_sha}`")
    lines.append(f"enrolled: {', '.join(sorted(enrolled)) if enrolled else '(none)'}")
    lines.append(f"test key trusted by explicit opt-in: `{manifest['trust_test_key'][:8]}…`")
    for u in unreadable:
        lines.append(f"unreadable report: {u}")
    if not enrolled:
        lines += ["", "0 ENROLLED: no binding ran, so there is nothing to report."]

    per: dict[str, dict[str, str]] = {}
    problems: list[str] = []
    for b in sorted(enrolled):
        own = [r for r in reports if r.get("binding") == b]
        if not own:
            problems.append(f"{b}: no report was uploaded")
            per[b] = {i: "missing" for i in ids}
            continue
        cell: dict[str, str] = {}
        for r in own:
            if r.get("cases_sha256") != derived_sha:
                problems.append(f"{b}: report cases_sha256 {r.get('cases_sha256')!r} is not the derived table's (stale)")
            for res in r.get("results", []):
                if res.get("transport") != "registry" or res.get("id") not in ids:
                    problems.append(f"{b}: unexpected result {res.get('id')!r} on transport {res.get('transport')!r}")
                    continue
                v = res.get("verdict", "?")
                cell[res["id"]] = v if v == "pass" else f"FAIL: {res.get('detail', '')}"
        per[b] = {i: cell.get(i, "missing") for i in ids}

    if enrolled:
        lines += ["", "| case | " + " | ".join(sorted(enrolled)) + " | note |", "| --- | " + " | ".join("---" for _ in enrolled) + " | --- |"]
        for i in ids:
            cells = " | ".join(per[b][i].replace("|", "/") for b in sorted(enrolled))
            lines.append(f"| {i} | {cells} | {' '.join(notes[i])} |")
    if problems:
        lines += ["", "problems:"] + [f"- {p}" for p in problems]
    lines += ["", "excluded, with the rule that excluded each:"]
    lines += [f"- `{e['id']}`: {e['reason']}" for e in manifest["excluded"]]
    return "\n".join(lines)


def selftest() -> None:
    def mk(cid: str, **kw: Any) -> dict[str, Any]:
        c: dict[str, Any] = {
            "id": cid, "tree": "basic", "transports": ["file", "http"], "http_script": None,
            "setup": {"cache": "empty", "system_dirs": [], "lock": None, "before_index_rename_hook": None},
            "request": {"spelling": "26.8", "platform": "linux-arm64", "offline": False, "frozen": False,
                         "lock_write": False, "update": False, "allow_unsigned": False, "trust": "test", "bases": ["{base}"]},
            "env": {},
            "expect": {"ok": True, "version": "1", "build": "b", "manifest": "m", "library_sha256": "l", "code": None,
                        "sleeps": [], "warnings": [],
                        "requests": {"max": None, "none_matching": [], "auth_on_second_origin": False}, "lock_after": None},
        }
        c.update(kw)
        return c

    def mutate(cid: str, path: tuple[str, ...], value: Any) -> dict[str, Any]:
        c = mk(cid)
        d = c
        for k in path[:-1]:
            d = d[k]
        d[path[-1]] = value
        return c

    # every exclusion rule fires on a case that violates only that rule, and the plain case is included.
    assert classify(mk("plain"))[0]
    for cid, path, value, rule in [
        ("t", ("tree",), "trust", "rule 1"),
        ("goldens-x", ("id",), "goldens-x", "rule 2"),
        ("s", ("http_script",), "retry-5xx", "rule 3"),
        ("c", ("setup", "cache"), "offline-hit", "rule 4"),
        ("e", ("env",), {"A": "b"}, "rule 5"),
        ("b", ("request", "bases"), ["{base}", "{base2}"], "rule 6"),
        ("o", ("request", "offline"), True, "rule 7"),
        ("k", ("request", "trust"), "none", "rule 8"),
        ("r", ("expect", "requests", "max"), 0, "rule 9"),
    ]:
        ok, why = classify(mutate(cid, path, value))
        assert not ok and why.startswith(rule), f"{cid}: expected {rule}, got {ok} {why}"

    doc = {"schema": 1, "source": {}, "cases": [mk("a"), mutate("b", ("tree",), "trust")]}
    derived, included, excluded = derive(doc)
    assert [c["id"] for c in derived["cases"]] == ["a"] and derived["cases"][0]["transports"] == ["registry"]
    assert [e["id"] for e in excluded] == ["b"], excluded
    assert doc["cases"][0]["transports"] == ["file", "http"], "derive must not mutate its input"

    # an override is applied AND recorded, never silent.
    OVERRIDES["a"] = {"expect": {"ok": False, "code": "CHTYPES_ARTIFACT_UNPUBLISHED"}, "reason": "selftest"}
    try:
        d2, inc2, _ = derive(doc)
    finally:
        del OVERRIDES["a"]
    assert d2["cases"][0]["expect"]["code"] == "CHTYPES_ARTIFACT_UNPUBLISHED" and inc2[0]["override"]["reason"] == "selftest"

    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        fx = t / "fx"
        (fx / "test-key").mkdir(parents=True)
        (fx / "test-key" / "public.hex").write_text("ab" * 32 + "\n")
        (fx / "trees").mkdir()
        (fx / "cases.json").write_text(json.dumps(doc))
        # the trust opt-in: a wrong key and a missing key both refuse, and nothing is written.
        for bad in ("cd" * 32, ""):
            try:
                emit(t / "o1", fx, bad)
            except SystemExit as e:
                assert "refusing" in str(e), e
            else:
                raise AssertionError("emit accepted a key that is not the test key")
            assert not (t / "o1").exists()
        # the schema gate runs against the derived table; an incomplete fixture (no `source` shape) is its job to refuse,
        # so use the real fixtures for the end-to-end run instead.
        out = t / "real"
        m = emit(out, FIXTURES, (FIXTURES / "test-key" / "public.hex").read_text().strip())
        assert (out / "trees").is_symlink() and not (out / "cases.json").is_symlink()
        assert (out / SIDECAR).is_file() and m["included"], "the real tree must yield at least one staged case"
        ids = [e["id"] for e in m["included"]]
        assert "line-ok" in ids and "missing-platform" in ids, ids

        # verdict: a pass, a fail, a missing case, a stale report, and the 0-enrolled line.
        en = t / "en"
        en.mkdir()
        sha = m["derived_cases_sha256"]
        assert "0 ENROLLED" in verdict(out, t / "none", en)
        (en / "go").touch()
        rep = t / "rep"
        rep.mkdir()
        res = [{"id": i, "transport": "registry", "verdict": "pass", "detail": ""} for i in ids]
        res[0] = {"id": ids[0], "transport": "registry", "verdict": "fail", "detail": "boom"}
        (rep / "a.json").write_text(json.dumps({"schema": 1, "binding": "go", "toolchain": "x", "cases_sha256": sha, "results": res[:-1]}))
        out_text = verdict(out, rep, en)
        assert "FAIL: boom" in out_text and "missing" in out_text, out_text
        (rep / "a.json").write_text(json.dumps({"schema": 1, "binding": "go", "toolchain": "x", "cases_sha256": "0" * 64, "results": res}))
        assert "stale" in verdict(out, rep, en)
        assert "no report was uploaded" in verdict(out, t / "none", en)
    print("staging.py --selftest: OK")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    e = sub.add_parser("emit")
    e.add_argument("--out", type=Path, required=True)
    e.add_argument("--fixtures", type=Path, default=FIXTURES)
    e.add_argument("--trust-test-key", default="", help="the SDK test key's public hex: the explicit opt-in")
    v = sub.add_parser("verdict")
    v.add_argument("--derived", type=Path, required=True)
    v.add_argument("--reports-dir", type=Path, required=True)
    v.add_argument("--enrolled-dir", type=Path, default=ENROLLED_DIR)
    v.add_argument("--registry-base", default="")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return 0
    if args.cmd == "emit":
        m = emit(args.out, args.fixtures, args.trust_test_key)
        print(f"staging: {len(m['included'])} case(s) derived, {len(m['excluded'])} excluded -> {args.out}")
        return 0
    if args.cmd == "verdict":
        text = verdict(args.derived, args.reports_dir, args.enrolled_dir, args.registry_base)
        print(text)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        return 0  # information only: the dispatch-only job never gates anything
    ap.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
