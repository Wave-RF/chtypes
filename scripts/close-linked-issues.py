#!/usr/bin/env python3
"""close-linked-issues.py — close every issue a just-merged pull request's own
`closingIssuesReferences` names, with a comment naming the merge.

WHY THIS EXISTS (chtypes#362). GitHub's own keyword auto-close (`Closes #N`)
does not fire for a merge whose actor is the policy-merge App's installation
token — measured 2026-10-01: 3 of 3 linked issues closed when a maintainer
enqueued the pull request, 0 of 2 when the `chtypes` App did (#353, #361).
The cause is not this App minting a broader token: it carries no `issues`
permission at all (`actions:write`, `contents:write`, `metadata:read`,
`pull_requests:write`, `workflows:write` — measured), so it could not close
an issue even if GitHub's auto-close did fire for it. Every self-merged fix
otherwise leaves its own issue open until someone notices by hand.

WHAT THIS RUNS AS. `.github/workflows/close-linked-issues.yml` runs this on
`push` to main, with the workflow's own built-in `GITHUB_TOKEN`, scoped at
job level to exactly `issues: write` (close + comment) and
`pull-requests: read` (the GraphQL read below) — no App, no secret, because
none of the App's permissions would help here anyway.

THE CHAIN, PER PUSHED COMMIT:

  1. `GET repos/{repo}/commits/{sha}/pulls` (resolve_merged_pr) — the merged
     pull request that commit is the merge commit of, if any. A commit that
     is not a merged pull request's own merge commit (should not happen on
     this repository's branch-protected, PR-only main, but is not this
     script's business to assume) resolves to None and is skipped, quietly:
     not every push is a merge, and that is not an error. More than one
     merged pull request reported for the same commit is refused loudly
     (ValueError) — there would be no principled way to choose whose
     `closingIssuesReferences` to act on, and guessing could close the wrong
     issues.
  2. `closingIssuesReferences` for that pull request, read via GraphQL
     (fetch_pr_closing_issues) — the REST pulls API does not expose this
     field. Only `closingIssuesReferences` is read; a `refs #N` mention is
     never in that list, so it can never close anything here, by
     construction — this script does not special-case it.
  3. issues_to_close (pure, no network): close an entry iff the pull request
     was merged, the entry's own repository is THIS repository, and its own
     state is still OPEN. Never reopens anything; never touches an
     already-closed issue.

IDEMPOTENT BY CONSTRUCTION — THE ENQUEUER NEVER NEEDS READING. A
maintainer-enqueued pull request's linked issues are already closed by
GitHub's own auto-close by the time this workflow's `push` event fires, so
step 3's OPEN check already skips them; there is no separate "was this
enqueued by a person" case to detect; #361 (App-enqueued, measured) and
#357 (a `refs`-only pull request) are each proven by real captured data —
see tests/fixtures/close-linked-issues/README.md.

FAIL CLOSED. Any `gh api` error (non-2xx, or output this script cannot
parse) raises ApiError, which main() turns into a message on stderr and
exit 2 — never a silent pass. The workflow wraps that in a loud `::error::`
and lets the step itself end the run red.

USAGE
  scripts/close-linked-issues.py --repo OWNER/NAME --sha SHA [SHA ...] [--dry-run]
  scripts/close-linked-issues.py --selftest    the pure decision logic, offline,
                                                from captured real API data plus
                                                fabricated edge cases

Requires `gh`, authenticated (`GH_TOKEN` in the environment when run from the
workflow).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES_DIR = HERE.parent / "tests" / "fixtures" / "close-linked-issues"

# first: 50 is comfortably above any pull request this repository has ever
# linked (#361 and #357 each carry 0 or 1); fetch_pr_closing_issues refuses
# loudly rather than silently acting on a truncated page if it is ever not
# enough (totalCount > len(nodes)).
CLOSING_ISSUES_QUERY = """
query($owner: String!, $name: String!, $num: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $num) {
      number
      merged
      mergeCommit { oid }
      closingIssuesReferences(first: 50) {
        totalCount
        nodes {
          number
          state
          repository { nameWithOwner }
        }
      }
    }
  }
}
"""


class ApiError(Exception):
    """A `gh api` call failed, or returned something this script cannot
    make sense of. Always fatal — never caught and silently worked around."""


# ------------------------------------------------------------- pure logic


def resolve_merged_pr(pulls: list[dict]) -> int | None:
    """Pick the single merged pull request from a
    `commits/{sha}/pulls` response. None means no merged pull request is
    associated with this commit — not every commit on main need be one, and
    that is not an error, just nothing for this script to do. More than one
    merged pull request raises: this script would then have no principled
    way to choose whose closingIssuesReferences to act on, and guessing
    could close the wrong issues."""
    merged = [p for p in pulls if p.get("merged_at")]
    if not merged:
        return None
    if len(merged) > 1:
        numbers = [p.get("number") for p in merged]
        raise ValueError(
            f"{len(merged)} merged pull requests reported for one commit ({numbers}) -- "
            "no principled way to choose whose closingIssuesReferences to act on"
        )
    return merged[0]["number"]


def issues_to_close(repo: str, merged: bool, closing_issues: list[dict]) -> list[int]:
    """Which issue numbers to close, from one pull request's own `merged`
    flag and its own closingIssuesReferences nodes (each a dict with
    `number`, `state` and `repository.nameWithOwner`). An entry closes iff
    the pull request was merged, the entry's repository is THIS repository,
    and the entry's own state is still OPEN -- never reopens, never touches
    an entry already closed. A maintainer-enqueued pull request's issues are
    already closed by GitHub's own keyword auto-close by the time this ever
    runs, so that case needs no separate detection: it reads exactly like
    "already closed" here."""
    if not merged:
        return []
    out: list[int] = []
    for ref in closing_issues:
        if ref["repository"]["nameWithOwner"] != repo:
            continue
        if ref["state"] != "OPEN":
            continue
        out.append(ref["number"])
    return out


def comment_body(pr_number: int, merge_sha: str) -> str:
    """The comment posted on every issue this script closes. First line is
    a machine-category marker (this repository's own convention, the
    `<!-- word: … -->` shape the since-retired v0 index read-back's comments
    also used) naming the pull request, so a reader or a
    watcher can tell at a glance which close this comment belongs to without
    parsing the sentence. Closes with `<!-- agent:chtypes-sdk -->`, this
    repository's identity marker for SDK-side automation."""
    return (
        f"<!-- close-linked-issues: pr {pr_number} -->\n\n"
        f"Closed by #{pr_number} (merged as {merge_sha}); this repository's policy merge "
        f"closes linked issues itself because a merge by its App does not trigger GitHub's "
        f"keyword auto-close (#362).\n\n"
        f"<!-- agent:chtypes-sdk -->\n"
    )


# ------------------------------------------------------------- gh api i/o


def gh_api(args: list[str]) -> str:
    proc = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise ApiError(f"gh api {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def fetch_commit_pulls(repo: str, sha: str) -> list[dict]:
    out = gh_api(["-X", "GET", f"repos/{repo}/commits/{sha}/pulls"])
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        raise ApiError(f"repos/{repo}/commits/{sha}/pulls returned unparseable JSON: {e}") from e
    if not isinstance(data, list):
        raise ApiError(f"repos/{repo}/commits/{sha}/pulls returned {type(data).__name__}, expected a list")
    return data


def fetch_pr_closing_issues(repo: str, pr_number: int) -> dict:
    owner, _, name = repo.partition("/")
    out = gh_api([
        "graphql",
        "-f", f"query={CLOSING_ISSUES_QUERY}",
        "-f", f"owner={owner}",
        "-f", f"name={name}",
        "-F", f"num={pr_number}",
    ])
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        raise ApiError(f"closingIssuesReferences query for {repo}#{pr_number} returned unparseable JSON: {e}") from e
    repository = (data.get("data") or {}).get("repository") or {}
    pr = repository.get("pullRequest")
    if pr is None:
        raise ApiError(f"closingIssuesReferences query for {repo}#{pr_number} returned no pullRequest: {out.strip()}")
    refs = pr["closingIssuesReferences"]
    if refs["totalCount"] > len(refs["nodes"]):
        raise ApiError(
            f"{repo}#{pr_number} closes {refs['totalCount']} issue(s) but the query only read back "
            f"{len(refs['nodes'])} -- refusing to act on a possibly truncated list"
        )
    return {"merged": pr["merged"], "closing_issues": refs["nodes"]}


def close_issue(repo: str, issue_number: int, pr_number: int, merge_sha: str, *, dry_run: bool) -> None:
    if dry_run:
        print(
            f"close-linked-issues: DRY RUN would close {repo}#{issue_number} "
            f"(closed by #{pr_number}, merged as {merge_sha})"
        )
        return
    gh_api([
        "-X", "PATCH", f"repos/{repo}/issues/{issue_number}",
        "-f", "state=closed",
        "-f", "state_reason=completed",
    ])
    gh_api([
        "-X", "POST", f"repos/{repo}/issues/{issue_number}/comments",
        "-f", f"body={comment_body(pr_number, merge_sha)}",
    ])
    print(f"close-linked-issues: closed {repo}#{issue_number} (closed by #{pr_number}, merged as {merge_sha})")


def process_commit(repo: str, sha: str, *, dry_run: bool) -> None:
    pulls = fetch_commit_pulls(repo, sha)
    pr_number = resolve_merged_pr(pulls)
    if pr_number is None:
        print(f"close-linked-issues: {sha} is not a merged pull request's own merge commit -- nothing to do")
        return
    result = fetch_pr_closing_issues(repo, pr_number)
    to_close = issues_to_close(repo, result["merged"], result["closing_issues"])
    if not to_close:
        print(
            f"close-linked-issues: #{pr_number} ({sha}) -- nothing to close "
            "(0 open, same-repository closingIssuesReferences)"
        )
        return
    for issue_number in to_close:
        close_issue(repo, issue_number, pr_number, sha, dry_run=dry_run)


# ------------------------------------------------------------------ selftest


def load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def selftest() -> int:
    """Prove the pure decision logic offline. The primary cases are captured
    real API data (tests/fixtures/close-linked-issues/README.md has the
    `gh api` calls and the date), not hand-set expectations: a checker whose
    test also supplies the answer the checker is supposed to compute proves
    nothing about the computation. Fabricated cases fill in edge shapes the
    two captured pull requests do not happen to exercise."""
    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        if not cond:
            print(f"SELFTEST FAILED: {msg}", file=sys.stderr)
            ok = False

    REPO = "Wave-RF/chtypes"

    # --- resolve_merged_pr, against the REAL commits/{sha}/pulls responses
    # captured for #361's and #357's own merge commits (2026-10-01).
    pulls_361 = load_fixture("pr-361-commits-pulls.json")
    check(resolve_merged_pr(pulls_361) == 361, "captured #361 commits/pulls response did not resolve to PR 361")

    pulls_357 = load_fixture("pr-357-commits-pulls.json")
    check(resolve_merged_pr(pulls_357) == 357, "captured #357 commits/pulls response did not resolve to PR 357")

    check(resolve_merged_pr([]) is None, "an empty commits/pulls response must resolve to no pull request, not an error")

    try:
        resolve_merged_pr([
            {"number": 1, "merged_at": "2026-01-01T00:00:00Z"},
            {"number": 2, "merged_at": "2026-01-01T00:00:00Z"},
        ])
        check(False, "two merged pull requests reported for one commit must raise, never pick one silently")
    except ValueError:
        pass

    # --- issues_to_close, PRIMARY CASE: PR #361's own real
    # closingIssuesReferences (captured 2026-10-01). #361 closes #306, and
    # #306 is CLOSED in the captured response -- this stands in for a
    # maintainer-enqueued pull request too, since GitHub's own auto-close
    # already closed #306 before this script could ever run against it: the
    # correct answer is the same "nothing to do" either way, which is why
    # this script never reads who enqueued anything.
    pr_361 = load_fixture("pr-361-closing-issues.json")
    check(
        issues_to_close(REPO, pr_361["merged"], pr_361["closing_issues"]) == [],
        "PR #361's captured closingIssuesReferences names #306, already CLOSED -- must close nothing",
    )

    # --- a captured refs-only pull request (#357): closes nothing at all.
    pr_357 = load_fixture("pr-357-closing-issues.json")
    check(
        issues_to_close(REPO, pr_357["merged"], pr_357["closing_issues"]) == [],
        "PR #357 (a refs-only pull request) carries no closingIssuesReferences -- must close nothing",
    )

    # --- fabricated: the happy path neither captured pull request above
    # exercises (both close zero open issues) -- without this, nothing would
    # prove issues_to_close can ever return anything at all.
    check(
        issues_to_close(REPO, True, [{"number": 42, "state": "OPEN", "repository": {"nameWithOwner": REPO}}]) == [42],
        "a merged pull request with one OPEN, same-repository closingIssuesReferences entry must close it",
    )

    # --- fabricated: not merged -> never close, whatever closingIssuesReferences says.
    check(
        issues_to_close(REPO, False, [{"number": 5, "state": "OPEN", "repository": {"nameWithOwner": REPO}}]) == [],
        "an unmerged pull request must close nothing, even with an OPEN same-repository reference",
    )

    # --- fabricated: cross-repository reference -> never close.
    check(
        issues_to_close(
            REPO, True, [{"number": 9, "state": "OPEN", "repository": {"nameWithOwner": "Wave-RF/unrelated-repo"}}]
        ) == [],
        "a closingIssuesReferences entry in a different repository must never be closed",
    )

    # --- fabricated: mixed list -- proves each entry is judged on its own,
    # not the whole list at once.
    mixed = [
        {"number": 10, "state": "OPEN", "repository": {"nameWithOwner": REPO}},
        {"number": 11, "state": "CLOSED", "repository": {"nameWithOwner": REPO}},
        {"number": 12, "state": "OPEN", "repository": {"nameWithOwner": "Wave-RF/other"}},
    ]
    check(
        issues_to_close(REPO, True, mixed) == [10],
        "a mixed closingIssuesReferences list must close only the OPEN, same-repository entries, one at a time",
    )

    # --- zero refs.
    check(issues_to_close(REPO, True, []) == [], "no closingIssuesReferences at all must close nothing")

    if not ok:
        return 1
    print(
        "close-linked-issues: selftest ok -- resolve_merged_pr picks the single merged pull request from a "
        "commits/pulls response (captured #361, #357) and refuses to guess among several; issues_to_close "
        "closes only OPEN, same-repository closingIssuesReferences of a merged pull request, proven against "
        "#361's own captured response (already closed, so nothing to do), #357's (no references at all), "
        "and fabricated merge/state/repository edge cases"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", help="owner/name")
    ap.add_argument("--sha", nargs="+", metavar="SHA", help="one or more commit shas pushed to main")
    ap.add_argument("--dry-run", action="store_true", help="resolve and print what would close, without writing")
    ap.add_argument(
        "--selftest",
        action="store_true",
        help="prove the pure decision logic offline, from captured and fabricated fixtures, and exit",
    )
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.repo or not args.sha:
        ap.error("--repo and --sha are required outside --selftest")

    try:
        for sha in args.sha:
            process_commit(args.repo, sha, dry_run=args.dry_run)
    except (ApiError, ValueError) as e:
        print(f"close-linked-issues: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
