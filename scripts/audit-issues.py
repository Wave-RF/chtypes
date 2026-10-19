#!/usr/bin/env python3
"""audit-issues.py - keep exactly one open issue per dependency advisory.

    scripts/audit-issues.py --results DIR --repo OWNER/NAME [--dry-run]
    scripts/audit-issues.py --selftest

WHY THIS EXISTS. The `security` job in ci.yml is report-only on pull requests
(a required advisory scan would block unrelated pull requests whenever an
upstream advisory lands), so a finding there is easy to miss: five RustSec
advisories sat unseen for days. .github/workflows/dependency-audit.yml runs
the same four scanners daily on `main` and `v2` and hands their JSON to this
script, which turns each advisory into one labeled issue.

RESULTS LAYOUT (written by the workflow):

    DIR/<branch>/govulncheck.json    govulncheck -format json (a stream of objects)
    DIR/<branch>/govulncheck.rc      its exit status (it exits 0 even with findings)
    DIR/<branch>/pip-audit.json      pip-audit --format json
    DIR/<branch>/pnpm-audit.json     pnpm audit --json
    DIR/<branch>/cargo-audit.json    cargo audit --json

The other three tools exit non-zero when they find something, so for them the
verdict is whether the file parses into the expected shape, never the exit
status.

BEHAVIOR.
  - normalize each finding to (ecosystem, package, advisory id, fixed
    versions, branches);
  - one issue per advisory id, found by the label plus the id in the title
    (`Dependency advisory <id>: ...`); an issue without the label is never
    listed, so never touched;
  - created when missing; edited, with ONE comment, only when the packages,
    fixed versions or branches changed; reopened if it was closed and the
    advisory is back; closed with a comment when no audited branch shows it;
  - a finding never fails the run. A missing, unparseable or failed scan does:
    exit 1, and no issue is closed that run (an unknown is not a fix).

The issue text names only the ecosystem, package, advisory id (linked to its
public advisory page), fixed version and branch names. Standard library plus
the `gh` CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Callable

LABEL = "dependency-advisory"
LABEL_COLOR = "d93f0b"
LABEL_DESC = "A dependency advisory found by the scheduled audit"
TITLE_RE = re.compile(r"^Dependency advisory (\S+?):")
TOOLS = ("govulncheck", "pip-audit", "pnpm-audit", "cargo-audit")
ECOSYSTEM = {"govulncheck": "go", "pip-audit": "python", "pnpm-audit": "typescript", "cargo-audit": "rust"}

Runner = Callable[[list[str], str | None], str]


class AuditError(Exception):
    """A scan or script failure (as opposed to a finding)."""


# ---------------------------------------------------------------- normalize


def advisory_url(adv_id: str) -> str:
    if adv_id.startswith("RUSTSEC-"):
        return f"https://rustsec.org/advisories/{adv_id}.html"
    if adv_id.startswith("GHSA-"):
        return f"https://github.com/advisories/{adv_id}"
    if adv_id.startswith("GO-"):
        return f"https://pkg.go.dev/vuln/{adv_id}"
    return f"https://osv.dev/vulnerability/{adv_id}"


def _load_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise AuditError(f"{path}: unreadable scan output ({e})") from e


def parse_cargo(doc) -> list[dict]:
    try:
        items = doc["vulnerabilities"]["list"]
        out = []
        for it in items:
            out.append(
                {
                    "id": it["advisory"]["id"],
                    "package": it["package"]["name"],
                    "fixed": list(it.get("versions", {}).get("patched") or []),
                }
            )
        return out
    except (KeyError, TypeError) as e:
        raise AuditError(f"cargo-audit output has an unexpected shape ({e!r})") from e


def parse_pip(doc) -> list[dict]:
    try:
        out = []
        for dep in doc["dependencies"]:
            for v in dep.get("vulns") or []:
                out.append({"id": v["id"], "package": dep["name"], "fixed": list(v.get("fix_versions") or [])})
        return out
    except (KeyError, TypeError) as e:
        raise AuditError(f"pip-audit output has an unexpected shape ({e!r})") from e


def parse_pnpm(doc) -> list[dict]:
    try:
        out = []
        for key, a in doc["advisories"].items():
            adv_id = a.get("github_advisory_id") or (a.get("cves") or [None])[0] or f"NPM-{a.get('id', key)}"
            patched = a.get("patched_versions")
            fixed = [] if not patched or patched.strip() == "<0.0.0" else [patched.strip()]
            out.append({"id": adv_id, "package": a["module_name"], "fixed": fixed})
        return out
    except (KeyError, TypeError, AttributeError) as e:
        raise AuditError(f"pnpm audit output has an unexpected shape ({e!r})") from e


def parse_govulncheck(text: str) -> list[dict]:
    """govulncheck prints concatenated, pretty-printed JSON objects."""
    dec = json.JSONDecoder()
    pos, n, msgs = 0, len(text), []
    while True:
        while pos < n and text[pos].isspace():
            pos += 1
        if pos >= n:
            break
        try:
            obj, pos = dec.raw_decode(text, pos)
        except ValueError as e:
            raise AuditError(f"govulncheck output is not a JSON stream ({e})") from e
        msgs.append(obj)
    if not any("config" in m for m in msgs):
        raise AuditError("govulncheck output has no config message")
    out = []
    for m in msgs:
        f = m.get("finding") if isinstance(m, dict) else None
        if not f:
            continue
        trace = f.get("trace") or []
        # Same bar as the text output: only a vulnerable symbol that is called.
        if not any("function" in fr for fr in trace):
            continue
        module = (trace[0].get("module") if trace else None) or "unknown"
        fixed = [f["fixed_version"]] if f.get("fixed_version") else []
        out.append({"id": f["osv"], "package": module, "fixed": fixed})
    return out


def collect(results_dir: str) -> dict[tuple[str, str], dict]:
    """advisory id -> {packages: set[(eco, pkg)], fixed: set, branches: set}."""
    branches = sorted(d for d in os.listdir(results_dir) if os.path.isdir(os.path.join(results_dir, d)))
    if not branches:
        raise AuditError(f"{results_dir}: no branch directories")
    found: dict[str, dict] = {}
    errors: list[str] = []
    for br in branches:
        for tool in TOOLS:
            base = os.path.join(results_dir, br, tool)
            try:
                if tool == "govulncheck":
                    try:
                        rc = open(base + ".rc", encoding="utf-8").read().strip()
                    except OSError as e:
                        raise AuditError(f"{base}.rc: missing ({e})") from e
                    if rc != "0":
                        raise AuditError(f"govulncheck on {br} exited {rc}")
                    try:
                        text = open(base + ".json", encoding="utf-8").read()
                    except OSError as e:
                        raise AuditError(f"{base}.json: missing ({e})") from e
                    items = parse_govulncheck(text)
                else:
                    doc = _load_json(base + ".json")
                    items = {"pip-audit": parse_pip, "pnpm-audit": parse_pnpm, "cargo-audit": parse_cargo}[tool](doc)
            except AuditError as e:
                errors.append(f"{br}/{tool}: {e}")
                continue
            for it in items:
                rec = found.setdefault(it["id"], {"packages": set(), "fixed": set(), "branches": set()})
                rec["packages"].add((ECOSYSTEM[tool], it["package"]))
                rec["fixed"].update(it["fixed"])
                rec["branches"].add(br)
    return found, errors  # type: ignore[return-value]


# -------------------------------------------------------------------- render


def title_for(adv_id: str, rec: dict) -> str:
    pkgs = ", ".join(sorted({p for _, p in rec["packages"]}))
    return f"Dependency advisory {adv_id}: {pkgs}"


def fields_for(adv_id: str, rec: dict) -> dict[str, str]:
    return {
        "Ecosystem": ", ".join(sorted({e for e, _ in rec["packages"]})),
        "Package": ", ".join(f"`{p}`" for p in sorted({p for _, p in rec["packages"]})),
        "Advisory": f"[{adv_id}]({advisory_url(adv_id)})",
        "Fixed version": ", ".join(f"`{v}`" for v in sorted(rec["fixed"])) or "none published yet",
        "Found on": ", ".join(f"`{b}`" for b in sorted(rec["branches"])),
    }


def body_for(adv_id: str, rec: dict) -> str:
    lines = [f"- **{k}:** {v}" for k, v in fields_for(adv_id, rec).items()]
    return (
        "\n".join(lines)
        + "\n\nThis issue is kept up to date by the scheduled dependency audit. "
        "It is updated when the affected branches or the fixed version change, "
        "and closed automatically once no audited branch reports the advisory.\n"
    )


def parse_body_fields(body: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r"^- \*\*([^:*]+):\*\* (.*)$", body or "", re.M):
        out[m.group(1)] = m.group(2).strip()
    return out


# ---------------------------------------------------------------------- plan


def plan(found: dict, issues: list[dict], can_close: bool) -> list[dict]:
    """issues: labeled issues only ({number,title,body,state}). Returns actions."""
    by_id: dict[str, dict] = {}
    for iss in issues:
        m = TITLE_RE.match(iss["title"])
        if not m:
            continue  # labeled but not ours to interpret: leave alone
        prev = by_id.get(m.group(1))
        # prefer an open issue if there are duplicates
        if prev is None or (prev["state"] != "OPEN" and iss["state"] == "OPEN"):
            by_id[m.group(1)] = iss
    actions: list[dict] = []
    for adv_id in sorted(found):
        rec = found[adv_id]
        new_fields = fields_for(adv_id, rec)
        iss = by_id.get(adv_id)
        if iss is None:
            actions.append({"op": "create", "id": adv_id, "title": title_for(adv_id, rec), "body": body_for(adv_id, rec)})
            continue
        old_fields = parse_body_fields(iss["body"])
        changed = [k for k, v in new_fields.items() if old_fields.get(k) != v]
        if iss["state"] != "OPEN":
            actions.append(
                {
                    "op": "reopen",
                    "id": adv_id,
                    "number": iss["number"],
                    "title": title_for(adv_id, rec),
                    "body": body_for(adv_id, rec),
                    "comment": "The advisory is reported again by the scheduled audit on "
                    + new_fields["Found on"]
                    + ".",
                }
            )
        elif changed:
            lines = [f"- {k}: {old_fields.get(k, '(none)')} -> {new_fields[k]}" for k in changed if k != "Advisory"]
            actions.append(
                {
                    "op": "update",
                    "id": adv_id,
                    "number": iss["number"],
                    "title": title_for(adv_id, rec),
                    "body": body_for(adv_id, rec),
                    "comment": "The scheduled audit found a change:\n\n" + "\n".join(lines),
                }
            )
    if can_close:
        for adv_id, iss in sorted(by_id.items()):
            if adv_id not in found and iss["state"] == "OPEN":
                actions.append(
                    {
                        "op": "close",
                        "id": adv_id,
                        "number": iss["number"],
                        "comment": "The scheduled audit no longer reports this advisory on any audited branch. Closing.",
                    }
                )
    return actions


def describe(a: dict) -> str:
    num = f" #{a['number']}" if "number" in a else ""
    return f"{a['op'].upper()}{num} {a['id']}"


# --------------------------------------------------------------------- apply


def gh_runner(args: list[str], stdin: str | None) -> str:
    r = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise AuditError(f"gh {' '.join(args[:3])} failed: {r.stderr.strip()}")
    return r.stdout


def list_issues(gh: Runner, repo: str) -> list[dict]:
    out = gh(
        ["issue", "list", "--repo", repo, "--label", LABEL, "--state", "all", "--limit", "1000", "--json", "number,title,body,state"],
        None,
    )
    return json.loads(out or "[]")


def apply(gh: Runner, repo: str, actions: list[dict]) -> None:
    for a in actions:
        op = a["op"]
        if op == "create":
            gh(["issue", "create", "--repo", repo, "--title", a["title"], "--body-file", "-", "--label", LABEL], a["body"])
        elif op in ("update", "reopen"):
            n = str(a["number"])
            gh(["issue", "edit", n, "--repo", repo, "--title", a["title"], "--body-file", "-"], a["body"])
            if op == "reopen":
                gh(["issue", "reopen", n, "--repo", repo], None)
            gh(["issue", "comment", n, "--repo", repo, "--body-file", "-"], a["comment"])
        elif op == "close":
            n = str(a["number"])
            gh(["issue", "comment", n, "--repo", repo, "--body-file", "-"], a["comment"])
            gh(["issue", "close", n, "--repo", repo, "--reason", "completed"], None)
        else:
            raise AuditError(f"unknown op {op}")


def ensure_label(gh: Runner, repo: str) -> None:
    gh(["label", "create", LABEL, "--repo", repo, "--color", LABEL_COLOR, "--description", LABEL_DESC, "--force"], None)


def sync(results_dir: str, repo: str, dry_run: bool, gh: Runner = gh_runner) -> int:
    found, errors = collect(results_dir)
    for e in errors:
        print(f"::error::audit scan failed: {e}", file=sys.stderr)
    if not dry_run:
        ensure_label(gh, repo)
    issues = list_issues(gh, repo)
    actions = plan(found, issues, can_close=not errors)
    print(f"audit-issues: {len(found)} advisory id(s) found, {len(issues)} labeled issue(s), {len(actions)} planned action(s)")
    for adv_id in sorted(found):
        rec = found[adv_id]
        print(f"  found {adv_id} {sorted(p for _, p in rec['packages'])} fixed={sorted(rec['fixed'])} on {sorted(rec['branches'])}")
    for a in actions:
        print(f"  {'would ' if dry_run else ''}{describe(a)}")
    if errors:
        print("audit-issues: closing disabled this run because a scan failed", file=sys.stderr)
    if not dry_run:
        apply(gh, repo, actions)
    return 1 if errors else 0


# ------------------------------------------------------------------ selftest


class StubGh:
    """In-memory stand-in for the gh CLI; records every call."""

    def __init__(self, issues: list[dict]):
        self.issues = issues  # each: number,title,body,state,labels
        self.calls: list[list[str]] = []
        self.touched: set[int] = set()
        self.next = 100

    def __call__(self, args: list[str], stdin: str | None) -> str:
        self.calls.append(args)
        if args[:2] == ["issue", "list"]:
            lab = args[args.index("--label") + 1]
            rows = [i for i in self.issues if lab in i["labels"]]
            return json.dumps([{k: i[k] for k in ("number", "title", "body", "state")} for i in rows])
        if args[:2] == ["issue", "create"]:
            self.next += 1
            self.issues.append(
                {"number": self.next, "title": args[args.index("--title") + 1], "body": stdin, "state": "OPEN", "labels": [LABEL]}
            )
            self.touched.add(self.next)
            return ""
        if args[:2] in (["issue", "edit"], ["issue", "comment"], ["issue", "close"], ["issue", "reopen"]):
            n = int(args[2])
            self.touched.add(n)
            iss = next(i for i in self.issues if i["number"] == n)
            if args[1] == "edit":
                iss["title"] = args[args.index("--title") + 1]
                iss["body"] = stdin
            elif args[1] == "close":
                iss["state"] = "CLOSED"
            elif args[1] == "reopen":
                iss["state"] = "OPEN"
            return ""
        if args[:2] == ["label", "create"]:
            return ""
        raise AssertionError(f"unexpected gh call {args}")


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _results(root: str, cargo: dict[str, list[tuple[str, str, list[str]]]]) -> None:
    """Write a full, clean result tree for branches main and v2, then plant cargo findings."""
    for br in ("main", "v2"):
        d = os.path.join(root, br)
        _write(os.path.join(d, "govulncheck.rc"), "0\n")
        _write(os.path.join(d, "govulncheck.json"), json.dumps({"config": {"scanner_name": "govulncheck"}}) + "\n")
        _write(os.path.join(d, "pip-audit.json"), json.dumps({"dependencies": [{"name": "x", "version": "1", "vulns": []}]}))
        _write(os.path.join(d, "pnpm-audit.json"), json.dumps({"advisories": {}}))
        lst = [
            {"advisory": {"id": i, "package": p}, "package": {"name": p}, "versions": {"patched": fx}}
            for i, p, fx in cargo.get(br, [])
        ]
        _write(os.path.join(d, "cargo-audit.json"), json.dumps({"vulnerabilities": {"found": bool(lst), "list": lst}}))


def selftest() -> int:
    import tempfile

    fails: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            fails.append(msg)

    # --- normalizers, each against a planted fixture
    check(
        parse_govulncheck(
            '{"config": {"a": 1}}\n{"finding": {"osv": "GO-1", "fixed_version": "v1.2.3", "trace": [{"module": "m", "function": "F"}]}}\n'
            '{"finding": {"osv": "GO-2", "trace": [{"module": "m"}]}}\n'
        )
        == [{"id": "GO-1", "package": "m", "fixed": ["v1.2.3"]}],
        "govulncheck: called symbol kept, module-only finding dropped",
    )
    check(
        parse_pnpm({"advisories": {"7": {"github_advisory_id": "GHSA-a", "module_name": "left", "patched_versions": ">=1.0.1"}}})
        == [{"id": "GHSA-a", "package": "left", "fixed": [">=1.0.1"]}],
        "pnpm: normalized",
    )
    check(
        parse_pip({"dependencies": [{"name": "p", "vulns": [{"id": "PYSEC-1", "fix_versions": ["2.0"]}]}]})
        == [{"id": "PYSEC-1", "package": "p", "fixed": ["2.0"]}],
        "pip-audit: normalized",
    )
    for bad, fn in (({}, parse_cargo), ({}, parse_pip), ({}, parse_pnpm)):
        try:
            fn(bad)
            check(False, f"{fn.__name__}: a wrong shape must raise")
        except AuditError:
            pass
    try:
        parse_govulncheck("not json")
        check(False, "govulncheck: garbage must raise")
    except AuditError:
        pass

    # --- the sync cases, driven through collect + plan + apply against the stub
    def issue(n, adv, pkg, fixed, branches, state="OPEN", labels=(LABEL,)):
        rec = {"packages": {("rust", pkg)}, "fixed": set(fixed), "branches": set(branches)}
        return {"number": n, "title": title_for(adv, rec), "body": body_for(adv, rec), "state": state, "labels": list(labels)}

    with tempfile.TemporaryDirectory() as tmp:
        res = os.path.join(tmp, "res")
        _results(
            res,
            {
                "main": [
                    ("RUSTSEC-NEW", "newcrate", [">=1.0.0"]),  # new finding
                    ("RUSTSEC-SAME", "samecrate", [">=2.0.0"]),  # unchanged
                    ("RUSTSEC-CHG", "chgcrate", [">=3.1.0"]),  # fix version changed
                    ("RUSTSEC-LOOK", "lookcrate", []),  # only an unlabeled look-alike exists
                ],
                "v2": [("RUSTSEC-SAME", "samecrate", [">=2.0.0"])],
            },
        )
        stub = StubGh(
            [
                issue(1, "RUSTSEC-SAME", "samecrate", [">=2.0.0"], ["main", "v2"]),
                issue(2, "RUSTSEC-CHG", "chgcrate", [">=3.0.0"], ["main"]),
                issue(3, "RUSTSEC-GONE", "gonecrate", [">=4.0.0"], ["main"]),  # resolved
                issue(4, "RUSTSEC-LOOK", "lookcrate", [], ["main"], labels=("bug",)),  # unlabeled look-alike
                issue(5, "RUSTSEC-OLD", "oldcrate", [], ["main"], state="CLOSED"),  # closed, still gone: stays closed
                {"number": 6, "title": "Some other tracking issue", "body": "x", "state": "OPEN", "labels": [LABEL]},
            ]
        )
        rc = sync(res, "o/r", dry_run=False, gh=stub)
        check(rc == 0, f"clean scans must exit 0, got {rc}")
        mutating = [c for c in stub.calls if c[:2] != ["issue", "list"] and c[:2] != ["label", "create"]]
        creates = [c for c in mutating if c[:2] == ["issue", "create"]]
        check(sorted(c[c.index("--title") + 1] for c in creates) == ["Dependency advisory RUSTSEC-LOOK: lookcrate", "Dependency advisory RUSTSEC-NEW: newcrate"],
              "a new finding, and the id whose only issue is unlabeled, are created")
        check(1 not in stub.touched, "the unchanged finding's issue is not touched")
        check(4 not in stub.touched, "the unlabeled look-alike is not touched")
        check(5 not in stub.touched and 6 not in stub.touched, "a closed gone issue and a non-advisory labeled issue are not touched")
        iss2 = next(i for i in stub.issues if i["number"] == 2)
        check("`>=3.1.0`" in iss2["body"] and iss2["state"] == "OPEN", "the changed fix version is written into the body")
        check(sum(1 for c in mutating if c[:3] == ["issue", "comment", "2"]) == 1, "exactly one comment on the changed issue")
        iss3 = next(i for i in stub.issues if i["number"] == 3)
        check(iss3["state"] == "CLOSED" and any(c[:3] == ["issue", "comment", "3"] for c in mutating), "the resolved finding is commented and closed")
        for c in mutating:
            check(all("agent:" not in a for a in c), "no agent marker in any call")

        # second run on the resulting state is a no-op
        before = len(stub.calls)
        sync(res, "o/r", dry_run=False, gh=stub)
        new_calls = [c for c in stub.calls[before:] if c[:2] not in (["issue", "list"], ["label", "create"])]
        check(new_calls == [], f"an unchanged state produces no mutations, got {new_calls}")

        # a reappearing advisory reopens the closed issue
        res2 = os.path.join(tmp, "res2")
        _results(res2, {"main": [("RUSTSEC-OLD", "oldcrate", [])]})
        sync(res2, "o/r", dry_run=False, gh=stub)
        check(next(i for i in stub.issues if i["number"] == 5)["state"] == "OPEN", "a returning advisory reopens its closed issue")

        # dry-run mutates nothing
        stub2 = StubGh([issue(3, "RUSTSEC-GONE", "gonecrate", [], ["main"])])
        sync(res, "o/r", dry_run=True, gh=stub2)
        check(all(c[:2] == ["issue", "list"] for c in stub2.calls), "--dry-run only reads")

        # a failed scan: exit 1 and nothing is closed
        res3 = os.path.join(tmp, "res3")
        _results(res3, {})
        os.remove(os.path.join(res3, "main", "cargo-audit.json"))
        stub3 = StubGh([issue(3, "RUSTSEC-GONE", "gonecrate", [], ["main"])])
        saved = sys.stderr
        sys.stderr = open(os.devnull, "w")
        try:
            rc3 = sync(res3, "o/r", dry_run=False, gh=stub3)
        finally:
            sys.stderr.close()
            sys.stderr = saved
        check(rc3 == 1, "a missing scan output fails the run")
        check(stub3.issues[0]["state"] == "OPEN", "no issue is closed when a scan failed")

    for f in fails:
        print(f"FAIL: {f}", file=sys.stderr)
    if fails:
        return 1
    print("audit-issues selftest: ok (normalizers, create, unchanged, changed, resolved, reopen, unlabeled look-alike, dry-run, scan failure)")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--selftest", action="store_true", help="prove the logic against planted cases, no network")
    ap.add_argument("--results", help="directory of per-branch scan outputs")
    ap.add_argument("--repo", help="OWNER/NAME")
    ap.add_argument("--dry-run", action="store_true", help="print the planned issue actions and change nothing")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.results or not args.repo:
        ap.error("--results and --repo are required")
    try:
        return sync(args.results, args.repo, args.dry_run)
    except AuditError as e:
        print(f"::error::audit-issues: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
