#!/usr/bin/env python3
"""regen-automerge-check.py — may a pull request that only regenerates docs/support.md merge itself?

    scripts/regen-automerge-check.py --selftest         every refusal below fires, and an all-good input passes
    scripts/regen-automerge-check.py --check-ci-names   REQUIRED_CHECKS agrees with ci.yml's blocking jobs (no network)
    scripts/regen-automerge-check.py gate  --repo OWNER/NAME --run-head-sha SHA --run-head-repo OWNER/NAME
    scripts/regen-automerge-check.py gate  --repo OWNER/NAME --pr N
    scripts/regen-automerge-check.py merge --repo OWNER/NAME --pr N --head-sha SHA
                                           --regenerated docs/support.md=FILE
                                           [--run-head-repo OWNER/NAME] [--dry-run]

Run by .github/workflows/regen-automerge.yml, whose header says why the
workflow exists and what it may touch; read that first.

WHY THIS EXISTS. Every number in docs/support.md's generated block is read by
scripts/support-matrix.sh from this tree's manifests, its release tags and the
live index.json. A pull request that only regenerates that block is therefore
a claim a machine can check completely: run the same script from main against
the same index and compare bytes. When the claim holds, a human's clearance
adds nothing, so the pull request merges itself.

================================================================================
THE CONDITIONS
================================================================================

Evaluated in this order; the first that fails is the one reported. A refusal is
not an error: the pull request is left for a human and the exit status is 0.
The numbers are the four conditions the workflow's header states.

  fork          (4) The head is a branch of this repository: the head repository
                    the ci run reports AND the pull request's own head repository.
  pull-request      Exactly one open pull request against main carries the
                    commit, and it is not a draft.
  stale             The pull request's head is still the commit the ci run
                    judged. A push after that run means the run judged
                    something that is no longer what would merge.
  files         (1) The pull request changes exactly ALLOWED_FILES, each with
                    status `modified`: no addition, deletion or rename, and
                    nothing else riding along. This is also what makes
                    `checks` mean anything: a pull_request run of `ci` uses the
                    pull request's own ci.yml, which a pull request that
                    changed it could make pass trivially.
  checks        (3) Every context in REQUIRED_CHECKS has a GitHub Actions check
                    run on the head, and every such run is `completed` with
                    conclusion `success`. Checks outside that list are ignored:
                    ci.yml's `divergences` job can be red by design.
  mergeable         GitHub has not already reported a merge conflict
                    (`mergeable: false`). A merge known to fail is not tried.
  review            No reviewer's latest review requests changes, and no
                    review conversation exists. Branch protection requires
                    every conversation resolved, and the REST API cannot tell a
                    resolved thread from an open one, so any review comment
                    leaves the pull request for a human.
  bytes         (2) main's own scripts/support-matrix.sh, run against the live
                    index, reproduces the head's copy of every ALLOWED_FILES
                    entry byte for byte.

`gate` checks everything except `bytes`, which needs the regeneration; the
workflow regenerates only when the gate passes, so an ordinary pull request
costs a handful of API reads. `merge` reads every fact again, checks all of
them including `bytes`, and merges with `sha=<head>` so GitHub itself refuses
if the head moved in between (HTTP 409, reported as `stale`). It then reads
the generated file back at the merge commit and fails the run if main does not
now hold the regeneration: "merged" and "main says what the script says" are
two claims, and only the second is the goal.

================================================================================
THE PWN-REQUEST RULE
================================================================================

The workflow runs on `workflow_run` with a token that can write to this
repository, so nothing from a pull request's head may run there. This script
never reads a head file from disk and never executes, sources, imports or
parses anything of the head: the head's copy of each allowed file arrives as
bytes from the contents API and is only compared with other bytes. The
workflow checks out main, and main's code is all that runs.

================================================================================
REQUIRED_CHECKS, AND WHY IT IS PINNED HERE
================================================================================

Branch protection's list of required contexts cannot be read with the
workflow's built-in token (that endpoint needs admin rights), so it is pinned
below, once. `--check-ci-names` derives, from .github/workflows/ci.yml itself,
the check-run name of every job that does NOT carry job-level
`continue-on-error: true`, and fails if that set and REQUIRED_CHECKS differ in
either direction. ci.yml marks its deliberately non-blocking jobs exactly that
way, and every other job in it is meant to be required. Branch protection
still has the final word at merge time; this list only stops the workflow from
attempting a merge it already knows would fail.

Exit status: 0 for a merge, a dry run or a refusal; 1 for a failed selftest, a
REQUIRED_CHECKS disagreement, or a merge whose result is not the regeneration;
2 for an API failure or anything else unexpected, so it can never be mistaken
for a quiet refusal.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CI_YML = os.path.join(ROOT, ".github", "workflows", "ci.yml")

BASE_BRANCH = "main"

# The files a regeneration pull request may change: all of them, and nothing
# else (condition 1). One list, on purpose. Adding a generated file here is a
# deliberate act with a second half: the workflow must regenerate it too and
# pass it to `merge` as --regenerated PATH=FILE, and `merge` refuses to run
# (exit 2) until every entry here has a regenerated copy.
ALLOWED_FILES = ("docs/support.md",)

# The check-run names branch protection requires on `main`, copied from it.
# --check-ci-names fails when this and ci.yml's blocking jobs disagree; see the
# header for why the list lives here rather than being read at run time.
REQUIRED_CHECKS = (
    "abi — the header and every binding agree",
    "go — build, vet, standalone check (no artifacts)",
    "python — ruff, import, suite (no artifacts)",
    "ts — build, typecheck, suite (no artifacts)",
    "rust — build, clippy, fmt, suite (no artifacts)",
    "abi-fixtures — the ABI-revision refusal, all four bindings, no published artifact",
    "artifacts — published lines, linux-amd64, every suite runs the golden set",
    "lint-go — golangci-lint (go/.golangci.yml)",
    "lint-ts — biome (ts/biome.json)",
    "lint-actions — actionlint + shellcheck",
    "misspell — American spelling, whole tree (scripts/lint-spelling.sh)",
    "public — no pointers into the private repository",
    "prose — markdownlint + no-hard-wrap (dprint textWrap never)",
)

# Branch protection pins every required context to the GitHub Actions app, so
# a check run of the same name from any other app satisfies nothing.
REQUIRED_CHECK_APP = "github-actions"

CONDITIONS = {
    "fork": "condition 4, the head is a branch of this repository",
    "pull-request": "one open, non-draft pull request against main carries the commit",
    "stale": "the head is still the commit ci judged",
    "files": "condition 1, only the generated file changed",
    "checks": "condition 3, every required check passed",
    "mergeable": "GitHub reports no merge conflict",
    "review": "no requested changes and no review conversation",
    "bytes": "condition 2, main's own regeneration reproduces the file byte for byte",
}


@dataclass(frozen=True)
class Refusal:
    condition: str
    detail: str

    def __post_init__(self) -> None:
        if self.condition not in CONDITIONS:
            raise ValueError(f"unknown condition {self.condition!r}")

    def text(self) -> str:
        return f"{self.condition} ({CONDITIONS[self.condition]}): {self.detail}"


# ------------------------------------------------------------ pure decisions


def select_pr(repo: str, run_head_repo: str, associated: list[dict]) -> tuple[int | None, Refusal | None]:
    """Which pull request a ci run's head commit belongs to, from the list
    GET /repos/{repo}/commits/{sha}/pulls returns."""
    if run_head_repo != repo:
        return None, Refusal("fork", f"the ci run's head is in {run_head_repo or '<no repository>'}, not {repo}")
    candidates = [p for p in associated
                  if p.get("state") == "open" and (p.get("base") or {}).get("ref") == BASE_BRANCH]
    if not candidates:
        return None, Refusal("pull-request", f"no open pull request against {BASE_BRANCH} carries this commit")
    if len(candidates) > 1:
        numbers = ", ".join(f"#{p.get('number')}" for p in candidates)
        return None, Refusal("pull-request", f"more than one open pull request against {BASE_BRANCH} carries "
                                             f"this commit ({numbers})")
    return candidates[0]["number"], None


def check_run_problems(check_runs: list[dict], required: tuple[str, ...]) -> list[str]:
    """Every required context that is not a completed, successful GitHub Actions
    check run. Several runs of one name (a re-run) must ALL be successful."""
    problems = []
    for name in required:
        runs = [r for r in check_runs
                if r.get("name") == name and (r.get("app") or {}).get("slug") == REQUIRED_CHECK_APP]
        if not runs:
            problems.append(f"{name!r} has no check run")
            continue
        for r in runs:
            if r.get("status") != "completed":
                problems.append(f"{name!r} is {r.get('status')}")
            elif r.get("conclusion") != "success":
                problems.append(f"{name!r} concluded {r.get('conclusion')}")
    return problems


def first_difference(a: bytes, b: bytes) -> tuple[int, int]:
    """(byte offset, 1-based line) of the first difference between two byte strings."""
    n = min(len(a), len(b))
    i = next((k for k in range(n) if a[k] != b[k]), n)
    return i, a[:i].count(b"\n") + 1


def decide(*, repo: str, expected_head_sha: str, run_head_repo: str | None, pr: dict,
           files: list[dict], check_runs: list[dict], reviews: list[dict],
           review_comments: list[dict], head_bytes: dict[str, bytes] | None = None,
           regenerated: dict[str, bytes] | None = None,
           required: tuple[str, ...] = REQUIRED_CHECKS,
           allowed: tuple[str, ...] = ALLOWED_FILES) -> Refusal | None:
    """The first condition that fails, or None when every condition holds.
    With `regenerated` None (the gate), the byte comparison is not made."""
    # fork (condition 4)
    if run_head_repo is not None and run_head_repo != repo:
        return Refusal("fork", f"the ci run's head is in {run_head_repo or '<no repository>'}, not {repo}")
    head = pr.get("head") or {}
    head_repo = (head.get("repo") or {}).get("full_name")
    if head_repo != repo:
        return Refusal("fork", f"the pull request's head is in {head_repo or '<a deleted repository>'}, not {repo}")

    # pull-request
    if pr.get("state") != "open":
        return Refusal("pull-request", f"the pull request is {pr.get('state')}")
    base_ref = (pr.get("base") or {}).get("ref")
    if base_ref != BASE_BRANCH:
        return Refusal("pull-request", f"the pull request targets {base_ref}, not {BASE_BRANCH}")
    if pr.get("draft"):
        return Refusal("pull-request", "the pull request is a draft")

    # stale
    if head.get("sha") != expected_head_sha:
        return Refusal("stale", f"the head is now {head.get('sha')}, not the judged {expected_head_sha}")

    # files (condition 1)
    names = [f.get("filename") for f in files]
    if pr.get("changed_files") != len(names):
        return Refusal("files", f"the pull request reports {pr.get('changed_files')} changed file(s) but the "
                                f"file list has {len(names)}")
    if sorted(names) != sorted(allowed):
        extra = sorted(set(names) - set(allowed))
        missing = sorted(set(allowed) - set(names))
        parts = []
        if extra:
            more = f" and {len(extra) - 5} more" if len(extra) > 5 else ""
            parts.append("also changes " + ", ".join(extra[:5]) + more)
        if missing:
            parts.append("does not change " + ", ".join(missing))
        if not parts:
            parts.append("lists a file more than once")
        return Refusal("files", "the pull request " + "; ".join(parts))
    for f in files:
        if f.get("status") != "modified":
            renamed = f" (from {f.get('previous_filename')})" if f.get("previous_filename") else ""
            return Refusal("files", f"{f.get('filename')} is {f.get('status')}{renamed}, not modified")

    # checks (condition 3)
    problems = check_run_problems(check_runs, required)
    if problems:
        more = f"; and {len(problems) - 3} more" if len(problems) > 3 else ""
        return Refusal("checks", "; ".join(problems[:3]) + more)

    # mergeable
    if pr.get("mergeable") is False:
        return Refusal("mergeable", "GitHub reports the pull request cannot merge cleanly")

    # review
    if review_comments:
        return Refusal("review", f"{len(review_comments)} review comment(s) exist; a human is in the conversation")
    latest: dict[str, str] = {}
    for r in reviews:
        state = r.get("state")
        if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[(r.get("user") or {}).get("login") or "<unknown>"] = state
    blockers = sorted(user for user, state in latest.items() if state == "CHANGES_REQUESTED")
    if blockers:
        return Refusal("review", "changes requested by " + ", ".join(blockers))

    # bytes (condition 2)
    if regenerated is not None:
        if head_bytes is None:
            raise ValueError("a byte comparison needs the head's bytes")
        for path in allowed:
            want, got = regenerated[path], head_bytes[path]
            if want != got:
                offset, line = first_difference(want, got)
                return Refusal("bytes", f"{path} at the head differs from main's regeneration at byte {offset} "
                                        f"(line {line}); the head has {len(got)} bytes, the regeneration "
                                        f"{len(want)}")
    return None


def decode_contents(payload: dict, path: str) -> bytes:
    """A file's bytes from a GET /repos/{repo}/contents/{path} response."""
    if payload.get("type") != "file":
        raise ValueError(f"{path} is a {payload.get('type')}, not a file")
    if payload.get("encoding") != "base64":
        raise ValueError(f"{path} came back with encoding {payload.get('encoding')!r}, not base64 "
                         "(the contents API stops inlining files over 1 MB)")
    data = base64.b64decode(payload.get("content") or "", validate=False)
    if len(data) != payload.get("size"):
        raise ValueError(f"{path} decoded to {len(data)} bytes but the API reports {payload.get('size')}")
    return data


def http_status(stderr: str) -> int | None:
    """The HTTP status `gh api` names in its error line: 'gh: <message> (HTTP 409)'."""
    m = re.search(r"\(HTTP (\d{3})\)", stderr)
    return int(m.group(1)) if m else None


# --------------------------------------------------- ci.yml's blocking jobs


class CiShapeError(Exception):
    pass


@dataclass
class CiJob:
    job_id: str
    name: str
    blocking: bool


def _yaml_scalar(value: str, lineno: int) -> str:
    """A single-line YAML scalar, in only the shapes ci.yml uses. Anything else
    is refused rather than guessed, so a changed shape fails loudly here."""
    if value.startswith('"'):
        m = re.match(r'^"((?:[^"\\]|\\.)*)"\s*(#.*)?$', value)
        if not m:
            raise CiShapeError(f"ci.yml line {lineno}: unterminated double-quoted value {value!r}")
        if "\\" in m.group(1):
            raise CiShapeError(f"ci.yml line {lineno}: an escape sequence in {value!r}; extend this parser")
        return m.group(1)
    if value.startswith("'"):
        m = re.match(r"^'((?:[^']|'')*)'\s*(#.*)?$", value)
        if not m:
            raise CiShapeError(f"ci.yml line {lineno}: unterminated single-quoted value {value!r}")
        return m.group(1).replace("''", "'")
    plain = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    if not plain or plain[0] in "&*!|>{[%@`" or ": " in plain:
        raise CiShapeError(f"ci.yml line {lineno}: a value this parser does not read: {value!r}")
    return plain


def ci_jobs(text: str) -> list[CiJob]:
    """Every job under ci.yml's top-level `jobs:` with the name its check run
    carries (`name:`, else the job id) and whether it blocks (no job-level
    `continue-on-error: true`). Only the keys at the job's own indentation are
    read: a step's `name:` or `continue-on-error:` sits deeper and is not the
    job's."""
    jobs: list[dict] = []
    in_jobs = False
    current: dict | None = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw.startswith(" "):
            in_jobs = re.match(r"^jobs:\s*(#.*)?$", raw) is not None
            current = None
            continue
        if not in_jobs:
            continue
        m = re.match(r"^  ([A-Za-z_][A-Za-z0-9_-]*):\s*(#.*)?$", raw)
        if m:
            current = {"id": m.group(1), "name": None, "coe": None, "line": lineno}
            jobs.append(current)
            continue
        if re.match(r"^ {1,3}\S", raw):
            raise CiShapeError(f"ci.yml line {lineno}: an entry under jobs: this parser does not read: {raw!r}")
        m = re.match(r"^    ([A-Za-z_][A-Za-z0-9_-]*):\s*(.*?)\s*$", raw)
        if not m or current is None:
            continue
        key, value = m.group(1), m.group(2)
        if key == "name":
            name = _yaml_scalar(value, lineno)
            if "${{" in name:
                raise CiShapeError(f"ci.yml line {lineno}: job {current['id']}'s name is an expression; "
                                   "its check-run name cannot be read statically")
            current["name"] = name
        elif key == "continue-on-error":
            flag = _yaml_scalar(value, lineno)
            if flag not in ("true", "false"):
                raise CiShapeError(f"ci.yml line {lineno}: job {current['id']}'s continue-on-error is {flag!r}, "
                                   "not a literal true or false")
            current["coe"] = flag == "true"
        elif key == "strategy":
            raise CiShapeError(f"ci.yml line {lineno}: job {current['id']} has a strategy; a matrix changes "
                               "its check-run names, so extend this parser before relying on it")
    return [CiJob(j["id"], j["name"] if j["name"] is not None else j["id"], not j["coe"]) for j in jobs]


def ci_name_problems(ci_text: str, required: tuple[str, ...]) -> list[str]:
    """Every way REQUIRED_CHECKS and ci.yml's blocking jobs disagree."""
    jobs = ci_jobs(ci_text)
    if not jobs:
        return ["ci.yml has no jobs this parser could read"]
    problems = []
    seen: dict[str, str] = {}
    for j in jobs:
        if j.name in seen:
            problems.append(f"jobs {seen[j.name]} and {j.job_id} share the check-run name {j.name!r}")
        seen[j.name] = j.job_id
    by_name = {j.name: j for j in jobs}
    for name in required:
        if name not in by_name:
            problems.append(f"REQUIRED_CHECKS names {name!r}, which no ci.yml job carries")
        elif not by_name[name].blocking:
            problems.append(f"REQUIRED_CHECKS names {name!r}, but ci.yml's job {by_name[name].job_id} is "
                            "continue-on-error (non-blocking)")
    if len(set(required)) != len(required):
        problems.append("REQUIRED_CHECKS lists a name more than once")
    for j in jobs:
        if j.blocking and j.name not in required:
            problems.append(f"ci.yml's job {j.job_id} ({j.name!r}) is blocking but not in REQUIRED_CHECKS")
    return problems


# ------------------------------------------------------------------- the API


class ApiError(Exception):
    def __init__(self, what: str, status: int | None, stderr: str):
        super().__init__(f"{what} failed (HTTP {status if status is not None else '?'}): {stderr}")
        self.status = status


def gh(args: list[str], what: str) -> str:
    proc = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        # gh prints an API error body on stdout; none of it is data.
        raise ApiError(what, http_status(proc.stderr), proc.stderr.strip())
    return proc.stdout


def gh_object(endpoint: str) -> dict:
    return json.loads(gh([endpoint], f"GET {endpoint}"))


def gh_items(endpoint: str, jq: str = ".[]") -> list[dict]:
    """Every item of a paginated list, one compact JSON value per output line."""
    out = gh(["--paginate", endpoint, "-q", jq], f"GET {endpoint}")
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def head_file(repo: str, path: str, sha: str) -> bytes:
    return decode_contents(gh_object(f"repos/{repo}/contents/{path}?ref={sha}"), path)


@dataclass
class Facts:
    pr: dict
    files: list[dict]
    check_runs: list[dict]
    reviews: list[dict]
    review_comments: list[dict]


def gather(repo: str, number: int, sha: str) -> Facts:
    return Facts(
        pr=gh_object(f"repos/{repo}/pulls/{number}"),
        files=gh_items(f"repos/{repo}/pulls/{number}/files?per_page=100"),
        check_runs=gh_items(f"repos/{repo}/commits/{sha}/check-runs?filter=latest&per_page=100", ".check_runs[]"),
        reviews=gh_items(f"repos/{repo}/pulls/{number}/reviews?per_page=100"),
        review_comments=gh_items(f"repos/{repo}/pulls/{number}/comments?per_page=100"),
    )


# ------------------------------------------------------- reporting, outputs


def summary(line: str) -> None:
    """One line to the log and, in Actions, to the job summary."""
    print(line)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def output(**values: object) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    for k, v in values.items():
        print(f"output {k}={v}")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in values.items():
                f.write(f"{k}={v}\n")


def refuse(refusal: Refusal, where: str) -> int:
    summary(f"regen-automerge: left for a human. {where}: {refusal.text()}")
    return 0


# ----------------------------------------------------------- the subcommands


def cmd_gate(args: argparse.Namespace) -> int:
    if args.pr is not None:
        number = args.pr
        sha = gh_object(f"repos/{args.repo}/pulls/{number}").get("head", {}).get("sha") or ""
        run_head_repo = None
    else:
        sha = args.run_head_sha
        run_head_repo = args.run_head_repo
        associated = gh_items(f"repos/{args.repo}/commits/{sha}/pulls?per_page=100") \
            if run_head_repo == args.repo else []
        number, refusal = select_pr(args.repo, run_head_repo, associated)
        if refusal:
            output(candidate=0)
            return refuse(refusal, f"commit {sha[:12]}")
    facts = gather(args.repo, number, sha)
    refusal = decide(repo=args.repo, expected_head_sha=sha, run_head_repo=run_head_repo, pr=facts.pr,
                     files=facts.files, check_runs=facts.check_runs, reviews=facts.reviews,
                     review_comments=facts.review_comments)
    if refusal:
        output(candidate=0)
        return refuse(refusal, f"PR #{number} at {sha[:12]}")
    print(f"regen-automerge: PR #{number} at {sha} passes every condition but the byte comparison; regenerating")
    output(candidate=1, pr=number, head_sha=sha)
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    regenerated: dict[str, bytes] = {}
    for spec in args.regenerated:
        path, sep, local = spec.partition("=")
        if not sep:
            print(f"regen-automerge-check: --regenerated needs PATH=FILE, got {spec!r}", file=sys.stderr)
            return 2
        with open(local, "rb") as f:
            regenerated[path] = f.read()
    if sorted(regenerated) != sorted(ALLOWED_FILES):
        print(f"regen-automerge-check: the workflow regenerated {sorted(regenerated)} but ALLOWED_FILES is "
              f"{sorted(ALLOWED_FILES)}; every allowed file needs a regenerated copy", file=sys.stderr)
        return 2
    number, sha = args.pr, args.head_sha
    facts = gather(args.repo, number, sha)
    judged = dict(repo=args.repo, expected_head_sha=sha, run_head_repo=args.run_head_repo or None, pr=facts.pr,
                  files=facts.files, check_runs=facts.check_runs, reviews=facts.reviews,
                  review_comments=facts.review_comments)
    # Every other condition first, fresh: a head that moved or vanished since
    # the gate is `stale`, never a failed read of its bytes.
    refusal = decide(**judged)
    if refusal is None:
        head_bytes = {path: head_file(args.repo, path, sha) for path in ALLOWED_FILES}
        refusal = decide(**judged, head_bytes=head_bytes, regenerated=regenerated)
    where = f"PR #{number} at {sha[:12]}"
    if refusal:
        return refuse(refusal, where)
    main_sha = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    if args.dry_run:
        summary(f"regen-automerge: DRY RUN. {where}: every condition holds (regenerated from main at "
                f"{main_sha[:12]}); the merge call was not made")
        return 0
    try:
        result = json.loads(gh(["-X", "PUT", f"repos/{args.repo}/pulls/{number}/merge",
                                "-f", f"sha={sha}", "-f", "merge_method=merge"],
                               f"PUT repos/{args.repo}/pulls/{number}/merge"))
    except ApiError as e:
        if e.status == 409:
            return refuse(Refusal("stale", "the head moved before the merge call (HTTP 409)"), where)
        raise
    merge_sha = result.get("sha")
    if result.get("merged") is not True or not merge_sha:
        print(f"regen-automerge-check: the merge call returned {result!r}", file=sys.stderr)
        return 2
    for path in ALLOWED_FILES:
        after = head_file(args.repo, path, merge_sha)
        if after != regenerated[path]:
            offset, line = first_difference(regenerated[path], after)
            summary(f"regen-automerge: MERGED PR #{number} (head {sha}) as {merge_sha}, but {path} at the merge "
                    f"commit differs from main's regeneration at byte {offset} (line {line}); a human must look")
            return 1
    summary(f"regen-automerge: merged PR #{number} (head {sha}) as {merge_sha}; {', '.join(ALLOWED_FILES)} at "
            f"the merge commit is main's regeneration (main at {main_sha[:12]}), byte for byte")
    return 0


def cmd_check_ci_names() -> int:
    with open(CI_YML, encoding="utf-8") as f:
        text = f.read()
    try:
        problems = ci_name_problems(text, REQUIRED_CHECKS)
    except CiShapeError as e:
        print(f"regen-automerge-check: {e}", file=sys.stderr)
        return 1
    if problems:
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print(f"regen-automerge-check: REQUIRED_CHECKS and ci.yml's blocking jobs disagree ({len(problems)}).\n"
              "  Update REQUIRED_CHECKS in scripts/regen-automerge-check.py in the same change, and ask an\n"
              "  admin to update branch protection's required checks to match.", file=sys.stderr)
        return 1
    blocking = sum(1 for j in ci_jobs(text) if j.blocking)
    print(f"regen-automerge-check: ok, REQUIRED_CHECKS names exactly ci.yml's {blocking} blocking job(s)")
    return 0


# ------------------------------------------------------------------ selftest

REPO = "example/chtypes"
SHA = "a" * 40
OTHER_SHA = "b" * 40
GOOD = b"# Support\n\n| Line | Platforms |\n|---|---|\n| `25.8` | all |\n"


def _good_pr() -> dict:
    return {"number": 7, "state": "open", "draft": False, "mergeable": None, "changed_files": 1,
            "base": {"ref": "main", "repo": {"full_name": REPO}},
            "head": {"sha": SHA, "ref": "support-matrix-b1", "repo": {"full_name": REPO}}}


def _run(name: str, status: str = "completed", conclusion: str | None = "success",
         app: str = REQUIRED_CHECK_APP) -> dict:
    return {"name": name, "status": status, "conclusion": conclusion, "app": {"slug": app}}


def _good() -> dict:
    return dict(
        repo=REPO, expected_head_sha=SHA, run_head_repo=REPO, pr=_good_pr(),
        files=[{"filename": "docs/support.md", "status": "modified"}],
        check_runs=[_run(n) for n in REQUIRED_CHECKS]
        + [_run("divergences — docs/limitations.md's Known-divergences claims", conclusion="failure")],
        reviews=[], review_comments=[],
        head_bytes={"docs/support.md": GOOD}, regenerated={"docs/support.md": GOOD},
    )


def _with(**changes: object) -> dict:
    d = _good()
    d.update(changes)
    return d


def _pr(**changes: object) -> dict:
    p = _good_pr()
    for k, v in changes.items():
        if k == "head_sha":
            p["head"]["sha"] = v
        elif k == "head_repo":
            p["head"]["repo"] = v
        elif k == "base_ref":
            p["base"]["ref"] = v
        else:
            p[k] = v
    return p


def _checks(drop: str | None = None, replace: dict | None = None, extra: list | None = None) -> list[dict]:
    runs = [_run(n) for n in REQUIRED_CHECKS if n != drop]
    if replace:
        runs = [replace if r["name"] == replace["name"] else r for r in runs]
    return runs + (extra or [])


CI_GOOD = """\
name: ci
on:
  pull_request:
jobs:
  alpha:
    name: "alpha — the first"
    runs-on: ubuntu-latest
    steps:
      - name: a step's name is not the job's
        continue-on-error: true
        run: echo
  beta:
    # name: a commented-out name is not the job's
    name: 'beta — it''s quoted'
    continue-on-error: false
    steps:
      - run: echo
  gamma:
    runs-on: ubuntu-latest
  delta:
    name: "delta — report only"
    continue-on-error: true
    steps:
      - run: echo
"""
CI_GOOD_REQUIRED = ("alpha — the first", "beta — it's quoted", "gamma")


def selftest() -> int:
    failures: list[str] = []

    def expect(label: str, got: Refusal | None, condition: str | None) -> None:
        if condition is None:
            if got is not None:
                failures.append(f"{label}: expected a pass, got {got.text()}")
        elif got is None:
            failures.append(f"{label}: expected a {condition} refusal, got a pass")
        elif got.condition != condition:
            failures.append(f"{label}: expected a {condition} refusal, got {got.text()}")

    # An all-good input passes, at the gate and at the merge.
    gate_good = _good()
    gate_good.pop("head_bytes")
    gate_good.pop("regenerated")
    expect("all good, gate", decide(**gate_good), None)
    expect("all good, merge", decide(**_good()), None)
    expect("mergeable unknown (null) is not a refusal", decide(**_with(pr=_pr(mergeable=None))), None)
    expect("mergeable true", decide(**_with(pr=_pr(mergeable=True))), None)
    expect("a manual re-check has no ci-run head repository", decide(**_with(run_head_repo=None)), None)
    expect("an approval after a change request clears it",
           decide(**_with(reviews=[{"state": "CHANGES_REQUESTED", "user": {"login": "r"}},
                                   {"state": "APPROVED", "user": {"login": "r"}}])), None)
    expect("a plain COMMENTED review with no review comment",
           decide(**_with(reviews=[{"state": "COMMENTED", "user": {"login": "r"}}])), None)

    # condition 1 — files
    expect("a second file changed",
           decide(**_with(pr=_pr(changed_files=2),
                          files=[{"filename": "docs/support.md", "status": "modified"},
                                 {"filename": ".github/workflows/ci.yml", "status": "modified"}])), "files")
    expect("a different single file",
           decide(**_with(files=[{"filename": "README.md", "status": "modified"}])), "files")
    expect("the generated file added, not modified",
           decide(**_with(files=[{"filename": "docs/support.md", "status": "added"}])), "files")
    expect("the generated file renamed into place",
           decide(**_with(files=[{"filename": "docs/support.md", "status": "renamed",
                                  "previous_filename": "README.md"}])), "files")
    expect("a file list shorter than the pull request's own count",
           decide(**_with(pr=_pr(changed_files=3))), "files")
    expect("no file at all", decide(**_with(pr=_pr(changed_files=0), files=[])), "files")

    # condition 2 — bytes
    one_byte = bytearray(GOOD)
    one_byte[-3] ^= 0x01
    expect("a one-byte difference",
           decide(**_with(head_bytes={"docs/support.md": bytes(one_byte)})), "bytes")
    expect("a missing trailing newline",
           decide(**_with(head_bytes={"docs/support.md": GOOD[:-1]})), "bytes")
    expect("one extra byte at the end",
           decide(**_with(head_bytes={"docs/support.md": GOOD + b"\n"})), "bytes")
    if first_difference(GOOD, bytes(one_byte)) != (len(GOOD) - 3, GOOD.count(b"\n")):
        failures.append(f"first_difference located the planted byte wrongly: {first_difference(GOOD, bytes(one_byte))}")

    # condition 3 — required checks
    required_one = REQUIRED_CHECKS[3]
    expect("a missing required check", decide(**_with(check_runs=_checks(drop=required_one))), "checks")
    expect("a failing required check",
           decide(**_with(check_runs=_checks(replace=_run(required_one, conclusion="failure")))), "checks")
    expect("a pending required check",
           decide(**_with(check_runs=_checks(replace=_run(required_one, status="in_progress", conclusion=None)))),
           "checks")
    expect("a queued required check",
           decide(**_with(check_runs=_checks(replace=_run(required_one, status="queued", conclusion=None)))),
           "checks")
    expect("a run not completed is not a pass, whatever its conclusion field says",
           decide(**_with(check_runs=_checks(replace=_run(required_one, status="in_progress")))), "checks")
    expect("a skipped required check is not a pass",
           decide(**_with(check_runs=_checks(replace=_run(required_one, conclusion="skipped")))), "checks")
    expect("a timed-out required check",
           decide(**_with(check_runs=_checks(replace=_run(required_one, conclusion="timed_out")))), "checks")
    expect("a required name from another app satisfies nothing",
           decide(**_with(check_runs=_checks(drop=required_one, extra=[_run(required_one, app="someone-else")]))),
           "checks")
    expect("two runs of one required check, one failed",
           decide(**_with(check_runs=_checks(extra=[_run(required_one, conclusion="failure")]))), "checks")
    expect("no check runs at all", decide(**_with(check_runs=[])), "checks")

    # condition 4 — fork
    expect("the ci run's head is a fork", decide(**_with(run_head_repo="someone/chtypes")), "fork")
    expect("the pull request's head is a fork",
           decide(**_with(pr=_pr(head_repo={"full_name": "someone/chtypes"}))), "fork")
    expect("the pull request's head repository was deleted", decide(**_with(pr=_pr(head_repo=None))), "fork")

    # stale head
    expect("a stale head", decide(**_with(pr=_pr(head_sha=OTHER_SHA))), "stale")

    # pull-request state
    expect("a closed pull request", decide(**_with(pr=_pr(state="closed"))), "pull-request")
    expect("a pull request against another branch", decide(**_with(pr=_pr(base_ref="release"))), "pull-request")
    expect("a draft", decide(**_with(pr=_pr(draft=True))), "pull-request")

    # mergeable, review
    expect("a known merge conflict", decide(**_with(pr=_pr(mergeable=False))), "mergeable")
    expect("a review comment", decide(**_with(review_comments=[{"id": 1}])), "review")
    expect("changes requested",
           decide(**_with(reviews=[{"state": "APPROVED", "user": {"login": "r"}},
                                   {"state": "CHANGES_REQUESTED", "user": {"login": "r"}}])), "review")

    # The ORDER is part of the contract: a fork is reported as a fork even
    # when every other condition also fails.
    everything_wrong = _with(run_head_repo="someone/chtypes", pr=_pr(state="closed", head_sha=OTHER_SHA),
                             check_runs=[], head_bytes={"docs/support.md": b""})
    expect("a fork outranks everything else", decide(**everything_wrong), "fork")

    # select_pr — which pull request a ci run belongs to
    def sel(run_head_repo: str, prs: list[dict]) -> Refusal | None:
        return select_pr(REPO, run_head_repo, prs)[1]
    open_main = {"number": 7, "state": "open", "base": {"ref": "main"}}
    expect("select: one open pull request against main", sel(REPO, [open_main]), None)
    if select_pr(REPO, REPO, [open_main])[0] != 7:
        failures.append("select: did not return the pull request's number")
    expect("select: a fork", sel("someone/chtypes", [open_main]), "fork")
    expect("select: none", sel(REPO, []), "pull-request")
    expect("select: only a closed one", sel(REPO, [dict(open_main, state="closed")]), "pull-request")
    expect("select: only one against another branch",
           sel(REPO, [dict(open_main, base={"ref": "release"})]), "pull-request")
    expect("select: two open ones", sel(REPO, [open_main, dict(open_main, number=8)]), "pull-request")
    expect("select: a closed one beside the open one",
           sel(REPO, [dict(open_main, number=6, state="closed"), open_main]), None)

    # the contents API and gh's error line
    if decode_contents({"type": "file", "encoding": "base64", "size": len(GOOD),
                        "content": base64.encodebytes(GOOD).decode()}, "p") != GOOD:
        failures.append("decode_contents: a line-wrapped base64 payload did not round-trip")
    for bad, label in (({"type": "file", "encoding": "none", "size": 5, "content": ""}, "a file over 1 MB"),
                       ({"type": "dir"}, "a directory"),
                       ({"type": "file", "encoding": "base64", "size": 99,
                         "content": base64.b64encode(GOOD).decode()}, "a size mismatch")):
        try:
            decode_contents(bad, "p")
            failures.append(f"decode_contents: {label} was accepted")
        except ValueError:
            pass
    if http_status("gh: Head branch was modified. Review and try the merge again. (HTTP 409)") != 409:
        failures.append("http_status: did not read 409 from gh's error line")
    if http_status("error connecting to api.github.com") is not None:
        failures.append("http_status: invented a status for a network error")

    # ci.yml's blocking jobs: the derivation itself, driven from text, never a
    # hand-set answer about the real file.
    jobs = {j.job_id: j for j in ci_jobs(CI_GOOD)}
    if sorted(jobs) != ["alpha", "beta", "delta", "gamma"]:
        failures.append(f"ci_jobs: read jobs {sorted(jobs)}")
    else:
        if jobs["alpha"].name != "alpha — the first" or not jobs["alpha"].blocking:
            failures.append("ci_jobs: a STEP's name or continue-on-error was read as the job's")
        if jobs["beta"].name != "beta — it's quoted":
            failures.append(f"ci_jobs: a commented name or a single-quoted one was misread: {jobs['beta'].name!r}")
        if jobs["gamma"].name != "gamma":
            failures.append("ci_jobs: a job without a name must carry its id as its check-run name")
        if jobs["delta"].blocking:
            failures.append("ci_jobs: a job-level continue-on-error: true was not read as non-blocking")
    agreeing = ci_name_problems(CI_GOOD, CI_GOOD_REQUIRED)
    if agreeing:
        failures.append(f"ci_name_problems: an agreeing list was refused: {agreeing}")
    for label, required in (
        ("a renamed job", ("alpha — renamed", "beta — it's quoted", "gamma")),
        ("a blocking job missing from the list", ("alpha — the first", "beta — it's quoted")),
        ("a non-blocking job in the list", CI_GOOD_REQUIRED + ("delta — report only",)),
        ("a name listed twice", CI_GOOD_REQUIRED + ("gamma",)),
    ):
        if not ci_name_problems(CI_GOOD, required):
            failures.append(f"ci_name_problems: {label} was not caught")
    renamed_in_ci = CI_GOOD.replace('"alpha — the first"', '"alpha — the first, renamed"')
    if not ci_name_problems(renamed_in_ci, CI_GOOD_REQUIRED):
        failures.append("ci_name_problems: a job renamed in ci.yml was not caught")
    new_job = CI_GOOD + "  epsilon:\n    name: \"epsilon — new and blocking\"\n    runs-on: ubuntu-latest\n"
    if not ci_name_problems(new_job, CI_GOOD_REQUIRED):
        failures.append("ci_name_problems: a new blocking job in ci.yml was not caught")
    for label, text in (
        ("an expression as a job name", CI_GOOD.replace('"alpha — the first"', '"alpha ${{ matrix.os }}"')),
        ("a matrix", CI_GOOD.replace("  gamma:\n", "  gamma:\n    strategy:\n      matrix: {os: [a, b]}\n")),
        ("an expression as continue-on-error",
         CI_GOOD.replace("continue-on-error: false", "continue-on-error: ${{ github.event_name == 'push' }}")),
    ):
        try:
            ci_jobs(text)
            failures.append(f"ci_jobs: {label} was read instead of refused")
        except CiShapeError:
            pass

    if failures:
        for f in failures:
            print(f"SELFTEST FAILED: {f}", file=sys.stderr)
        return 1
    print("regen-automerge-check: selftest ok — a second file, a one-byte difference, a missing, failing or "
          "pending required check, a fork, a stale head, a draft, a conflict and a review each refuse; an "
          "all-good input passes; ci.yml's blocking jobs are derived from its text")
    return 0


# ----------------------------------------------------------------------- main


def main(argv: list[str]) -> int:
    if argv == ["--selftest"]:
        return selftest()
    if argv == ["--check-ci-names"]:
        return cmd_check_ci_names()
    parser = argparse.ArgumentParser(prog="regen-automerge-check.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gate", help="every condition but the byte comparison")
    g.add_argument("--repo", required=True)
    g.add_argument("--pr", type=int)
    g.add_argument("--run-head-sha")
    g.add_argument("--run-head-repo")
    m = sub.add_parser("merge", help="every condition, then the merge")
    m.add_argument("--repo", required=True)
    m.add_argument("--pr", type=int, required=True)
    m.add_argument("--head-sha", required=True)
    m.add_argument("--run-head-repo", default="")
    m.add_argument("--regenerated", action="append", default=[], metavar="PATH=FILE")
    m.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "gate":
        if (args.pr is None) == (args.run_head_sha is None):
            parser.error("gate needs exactly one of --pr and --run-head-sha")
        if args.run_head_sha is not None and args.run_head_repo is None:
            parser.error("--run-head-sha needs --run-head-repo")
    try:
        return cmd_gate(args) if args.cmd == "gate" else cmd_merge(args)
    except Exception as e:  # noqa: BLE001 — every unexpected failure must read as one, never as a refusal
        print(f"regen-automerge-check: {type(e).__name__}: {e}", file=sys.stderr)
        summary(f"regen-automerge: ERROR ({type(e).__name__}), nothing was merged: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
