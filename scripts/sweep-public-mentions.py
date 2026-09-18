#!/usr/bin/env python3
"""sweep-public-mentions.py — apply lint-public.sh's needles to every GitHub
issue and pull request body and comment in this repository.

WHY THIS EXISTS (issue #96). scripts/lint-public.sh only ever sees tracked
files, and only at the moment something runs it — it cannot be a CI gate for
GitHub conversation text, because a comment can be posted at any time, long
after a PR that added it has already gone green. This script is the other
half: the exact same needles (fetched live from
`scripts/lint-public.sh --print-rules`, so this can never drift into a
second, hand-copied definition the way a 2026-09 sweep drifted from "gone"),
applied to:

  - every issue body and every pull request body,
  - every issue/PR conversation comment (the "Conversation" tab), and
  - every pull request review (inline diff) comment.

Pull request review SUMMARIES — the text at the top of a submitted review,
separate from its inline comments — are not covered: GitHub has no
repository-wide endpoint for them, only a per-pull-request one, and this
script deliberately avoids per-issue/per-PR fan-out (same reasoning as the
shared cross-repo watcher: one call per surface, not one call per item). If
that surface turns out to matter, add a fourth, explicitly fanned-out pass —
don't fold it into the three below silently.

WHEN AND HOW OFTEN. Not a gate, so nothing runs this automatically:

  - run it by hand after posting anything that quotes cross-repo shorthand,
    pastes a local stack trace or shell transcript, or cites another
    repository's path from memory;
  - run it as a periodic backstop otherwise — monthly is reasonable for this
    repository's current comment volume;
  - run it before scrubbing a thread suspected of carrying a leak, to get
    every hit in one pass instead of re-reading by eye.

It sweeps the full history every run — no `--since` bookmark — because this
is a periodic sweep, not a watcher: a full paginated run is cheap, and a
bookmark would just be one more place a leak could hide (an old comment
posted before the bookmark existed).

NOT A SHELL SCRIPT. scripts/lint-actions.sh shellchecks every tracked *.sh;
this file is *.py on purpose (the needle matching needs real regex and a
placeholder exclusion list, same reason lint-public.sh itself reaches for
python3 rather than grep for its two newest rules) and is therefore not
covered by that lint. There is no equivalent pyflakes/ruff gate wired up for
this repository at the time this script was added — read it by eye.

USAGE
  scripts/sweep-public-mentions.py                    sweep Wave-RF/chtypes
  scripts/sweep-public-mentions.py --repo OWNER/NAME   sweep a different repo

Exit status is 1 if anything matched, 0 if the sweep is clean — useful for a
human's own "did I just introduce something" check, but again: not a gate,
because nothing here can run on a schedule against future comments by itself.

Requires `gh`, authenticated (scripts/fetch.sh already assumes the same
tool is on PATH, as a fallback download path).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
LINT_PUBLIC = HERE / "lint-public.sh"

# The three bulk, non-fanned-out surfaces. Each is a (label, endpoint, jq)
# triple; jq keeps the payload down to exactly what is needed to report a
# hit: a URL to open and a body to scan.
SURFACES = [
    (
        "issue/PR body",
        "repos/{repo}/issues?state=all",
        '.[] | select(.body != null) | {url: .html_url, body: .body}',
    ),
    (
        "issue/PR comment",
        "repos/{repo}/issues/comments",
        '.[] | select(.body != null) | {url: .html_url, body: .body}',
    ),
    (
        "PR review comment",
        "repos/{repo}/pulls/comments",
        '.[] | select(.body != null) | {url: .html_url, body: .body}',
    ),
]


def get_needles() -> tuple[list[str], list[re.Pattern], tuple[re.Pattern, set[str]] | None]:
    """Read the needle set straight from lint-public.sh --print-rules, so this
    script can never carry a second, drifted copy of what counts as a leak."""
    out = subprocess.run(
        [str(LINT_PUBLIC), "--print-rules"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    literals: list[str] = []
    regexes: list[re.Pattern] = []
    local_path: tuple[re.Pattern, set[str]] | None = None
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        kind = parts[0]
        if kind == "LITERAL":
            literals.append(parts[1])
        elif kind == "REGEX":
            regexes.append(re.compile(parts[1], re.IGNORECASE))
        elif kind == "LOCALPATH":
            pattern, placeholders_csv = parts[1], parts[2]
            placeholders = {p for p in placeholders_csv.split(",") if p}
            local_path = (re.compile(pattern), placeholders)
        else:
            print(f"sweep-public-mentions: unrecognized rule kind {kind!r} from --print-rules", file=sys.stderr)
    return literals, regexes, local_path


def find_hits(body: str, literals: list[str], regexes: list[re.Pattern],
              local_path: tuple[re.Pattern, set[str]] | None) -> list[tuple[str, str]]:
    """Return (needle-description, matching-line) pairs for one body of text."""
    hits: list[tuple[str, str]] = []
    for line in body.splitlines():
        low = line.lower()
        for lit in literals:
            if lit.lower() in low:
                hits.append((f"literal: {lit}", line.strip()))
        for pat in regexes:
            for m in pat.finditer(line):
                hits.append((f"pattern: {pat.pattern}", line.strip()))
        if local_path is not None:
            pat, placeholders = local_path
            for m in pat.finditer(line):
                if m.group(1).lower() in placeholders:
                    continue
                hits.append(("local absolute path", line.strip()))
    return hits


def fetch(endpoint: str, jq: str, repo: str) -> list[dict]:
    proc = subprocess.run(
        ["gh", "api", "--paginate", endpoint.format(repo=repo), "-q", jq],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # A failed API call must never look like a quiet repo (the Wave RF
        # convention this project's peers already learned the hard way) —
        # announce it loudly and refuse to report "clean".
        print(f"sweep-public-mentions: gh api {endpoint} FAILED (exit {proc.returncode})", file=sys.stderr)
        print(proc.stderr.strip(), file=sys.stderr)
        raise SystemExit(2)
    items = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        items.append(json.loads(line))
    return items


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default="Wave-RF/chtypes", help="owner/name (default: Wave-RF/chtypes)")
    args = ap.parse_args()

    literals, regexes, local_path = get_needles()
    total_hits = 0

    for label, endpoint, jq in SURFACES:
        items = fetch(endpoint, jq, args.repo)
        for item in items:
            hits = find_hits(item["body"], literals, regexes, local_path)
            for needle, line in hits:
                total_hits += 1
                print(f"sweep-public-mentions: [{label}] {item['url']}")
                print(f"    {needle}")
                print(f"    {line}")

    if total_hits == 0:
        print(f"sweep-public-mentions: ok — clean sweep of {args.repo} issues, PRs and their comments")
        return 0
    print(f"sweep-public-mentions: {total_hits} occurrence(s) across {args.repo}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
