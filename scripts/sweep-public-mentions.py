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

EXEMPTIONS (issue #160). Some hits are the detector documenting itself —
#96 and #101 have to name the strings lint-public.sh matches in order to
explain what it matches. That is not a violation, but it used to be
recorded nowhere machine-readable, so the next sweep would report it as one.
A body can carry the exemption inline, and the sweep will honor it:

  <!-- sweep-exempt: documents the needle set (issue #96) -->

Rules, deliberately narrow:

  - The reason text after the colon is mandatory. A marker with nothing
    after "sweep-exempt:" (whitespace only) is NOT honored — the hits in
    that body are reported as ordinary findings, same as if there were no
    marker at all. The reason is the point: it is what tells a later reader
    why this one is allowed, so a marker that does not say why cannot exempt
    anything.
  - The marker is read from ONE body at a time and only ever changes the
    classification of hits found in THAT SAME body. There is no mechanism
    here that reads a marker from one item and suppresses a needle anywhere
    else — nothing in this script keeps a global "needle X is exempted" set.
    A marker in a comment cannot silence a hit in a different issue, a
    different comment, or the same needle appearing unmarked somewhere else.
  - Exempted hits are never dropped. They are counted and printed in their
    own clearly labeled EXEMPTED section, each with the reason it was
    exempted, right beside the FINDINGS section. "Exempted" is a
    classification of a real hit, not a subtraction from the total.

NOT A SHELL SCRIPT. scripts/lint-actions.sh shellchecks every tracked *.sh;
this file is *.py on purpose (the needle matching needs real regex and a
placeholder exclusion list, same reason lint-public.sh itself reaches for
python3 rather than grep for its two newest rules) and is therefore not
covered by that lint. There is no equivalent pyflakes/ruff gate wired up for
this repository at the time this script was added — read it by eye.

USAGE
  scripts/sweep-public-mentions.py                    sweep Wave-RF/chtypes
  scripts/sweep-public-mentions.py --repo OWNER/NAME   sweep a different repo
  scripts/sweep-public-mentions.py --selftest          prove the marker logic
                                                        against synthetic
                                                        items (no network)

Exit status is 1 if anything is reported as a FINDING, 0 otherwise. An
exempted-only sweep (findings == 0, one or more exempted) still exits 0: an
exemption that has a stated reason and is visibly reported is not an open
item for whoever is checking "did I just introduce something" — that is the
whole point of making the exemption travel with the text instead of living
only in institutional memory. The EXEMPTED section is there so a human can
still audit it; it just does not fail the run. Useful for a human's own
check, but again: not a gate, because nothing here can run on a schedule
against future comments by itself.

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

# The exemption marker. Deliberately item-scoped: exemption_reason() is only
# ever called with ONE item's own body, and its return value is only ever
# used to classify hits found in that SAME body (see classify()). There is
# no repo-wide table of "needle X is exempted" anywhere in this file — that
# is intentional, see the module docstring's EXEMPTIONS section.
EXEMPT_PATTERN = re.compile(r"<!--\s*sweep-exempt:\s*(.*?)\s*-->", re.IGNORECASE)


def exemption_reason(body: str) -> str | None:
    """Return the reason text of a sweep-exempt marker found in THIS body, or
    None if there is no marker, or every marker present carries no reason
    text. Never looks at any other item's body — the only input is the
    single body being classified."""
    for m in EXEMPT_PATTERN.finditer(body):
        reason = m.group(1).strip()
        if reason:
            return reason
    return None


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


def classify(label: str, items: list[dict], literals: list[str], regexes: list[re.Pattern],
             local_path: tuple[re.Pattern, set[str]] | None) -> tuple[list[dict], list[dict]]:
    """Split the hits found across a list of {url, body} items from one
    surface into findings and exempted occurrences. The exemption marker is
    read from EACH item's own body only (see exemption_reason) — a marker in
    one item can never reach a hit found in a different item, so nothing
    here can turn a needle off repository-wide from inside a single
    comment."""
    findings: list[dict] = []
    exempted: list[dict] = []
    for item in items:
        hits = find_hits(item["body"], literals, regexes, local_path)
        if not hits:
            continue
        reason = exemption_reason(item["body"])
        for needle, line in hits:
            entry = {"label": label, "url": item["url"], "needle": needle, "line": line}
            if reason is not None:
                entry["reason"] = reason
                exempted.append(entry)
            else:
                findings.append(entry)
    return findings, exempted


def print_entries(header: str, entries: list[dict]) -> None:
    print(f"sweep-public-mentions: --- {header} ({len(entries)} occurrence(s)) ---")
    for e in entries:
        print(f"sweep-public-mentions: [{e['label']}] {e['url']}")
        print(f"    {e['needle']}")
        print(f"    {e['line']}")
        if "reason" in e:
            print(f"    exempt reason: {e['reason']}")


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


def selftest() -> int:
    """Prove the marker logic — no network. Needles still come live from
    lint-public.sh --print-rules (never a second, hand-maintained list here),
    the same rule the rest of this script follows — so this function never
    spells out a needle itself: it plants whatever the live rule set hands
    back, the same way lint-public.sh's own --selftest plants its fixtures in
    a throwaway git repo rather than in its own tracked source."""
    literals, regexes, local_path = get_needles()
    if not literals:
        print(
            "sweep-public-mentions: selftest needs at least one LITERAL rule from "
            "`lint-public.sh --print-rules` to plant a hit with, and got none",
            file=sys.stderr,
        )
        return 1

    needle = literals[0]
    hit_line = f"a line that happens to mention {needle} in passing"
    label = "selftest"
    items = [
        # 1. No marker at all: must be an ordinary finding.
        {"url": "https://example.invalid/unmarked", "body": hit_line},
        # 2. A marker with a real reason: must be exempted, and the
        #    exemption must still be visible (reported, not dropped).
        {
            "url": "https://example.invalid/marked",
            "body": hit_line + "\n<!-- sweep-exempt: selftest fixture, not a real leak -->",
        },
        # 3. A marker present but with no reason text: must NOT exempt —
        #    the hit stays an ordinary finding.
        {"url": "https://example.invalid/marked-empty", "body": hit_line + "\n<!-- sweep-exempt: -->"},
        # 4. The SAME needle, in a different, unmarked item. Proves the
        #    marker on item 2 did not switch the needle off globally — if it
        #    had, this hit would wrongly disappear or turn up exempted too.
        {"url": "https://example.invalid/unmarked-again", "body": f"and a second, unrelated mention of {needle}"},
    ]

    findings, exempted = classify(label, items, literals, regexes, local_path)
    finding_urls = {e["url"] for e in findings}
    exempted_urls = {e["url"] for e in exempted}

    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        if not cond:
            print(f"SELFTEST FAILED: {msg}", file=sys.stderr)
            ok = False

    check(
        "https://example.invalid/unmarked" in finding_urls,
        "an unmarked hit was not reported as a finding",
    )
    check(
        "https://example.invalid/marked" in exempted_urls,
        "a hit with a reasoned marker was not classified as exempted",
    )
    check(
        "https://example.invalid/marked" not in finding_urls,
        "a hit with a reasoned marker was ALSO reported as a finding (should be exempted only)",
    )
    check(
        any(e["reason"] == "selftest fixture, not a real leak" for e in exempted
            if e["url"] == "https://example.invalid/marked"),
        "an exempted hit did not carry its marker's reason text (exemption must stay visible, with why)",
    )
    check(
        "https://example.invalid/marked-empty" in finding_urls,
        "a marker with no reason text wrongly exempted a hit",
    )
    check(
        "https://example.invalid/marked-empty" not in exempted_urls,
        "a marker with no reason text wrongly exempted a hit",
    )
    check(
        "https://example.invalid/unmarked-again" in finding_urls,
        "an exemption on one item suppressed the SAME needle on a different, unmarked item — "
        "that would be a global switch, not a per-item exemption",
    )
    check(
        "https://example.invalid/unmarked-again" not in exempted_urls,
        "an exemption on one item leaked into a different, unmarked item's classification",
    )

    if not ok:
        return 1
    print(
        "sweep-public-mentions: selftest ok — an unmarked hit is a finding, a reasoned marker "
        "exempts only its own item and stays visible with its reason, an empty marker exempts "
        "nothing, and no exemption reaches a different item"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default="Wave-RF/chtypes", help="owner/name (default: Wave-RF/chtypes)")
    ap.add_argument(
        "--selftest",
        action="store_true",
        help="prove the exemption-marker logic against synthetic items (no network) and exit",
    )
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    literals, regexes, local_path = get_needles()
    all_findings: list[dict] = []
    all_exempted: list[dict] = []

    for label, endpoint, jq in SURFACES:
        items = fetch(endpoint, jq, args.repo)
        findings, exempted = classify(label, items, literals, regexes, local_path)
        all_findings.extend(findings)
        all_exempted.extend(exempted)

    if all_findings:
        print_entries("FINDINGS", all_findings)
    if all_exempted:
        print_entries("EXEMPTED — not counted as findings", all_exempted)

    if not all_findings and not all_exempted:
        print(f"sweep-public-mentions: ok — clean sweep of {args.repo} issues, PRs and their comments")
        return 0

    if not all_findings:
        print(
            f"sweep-public-mentions: ok — 0 finding(s), {len(all_exempted)} occurrence(s) "
            f"exempted by marker across {args.repo}"
        )
        return 0

    print(
        f"sweep-public-mentions: {len(all_findings)} finding(s), {len(all_exempted)} occurrence(s) "
        f"exempted by marker across {args.repo}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
