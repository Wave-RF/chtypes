#!/usr/bin/env python3
r"""check-selftests-wired.py — every script under scripts/ that DECLARES a
`--selftest` mode is actually invoked with `--selftest` somewhere in CI
(chtypes#268).

WHY THIS EXISTS. A checker nobody has seen fail is not a checker — the house
rule scripts/check-abi-decls.py, scripts/lint-public.sh and every other
selftest-first gate in this repository already follow. Three scripts kept the
letter of that rule (each has a `--selftest` mode, each passes it locally) and
broke its spirit: scripts/check-suite.sh, scripts/lib/provenance.py and
scripts/published-lines.sh all declared a `--selftest` that no workflow step
ever ran, so a regression in any of the three would have shipped silently.
scripts/index-diff.sh was in the identical state until #267 wired it in, and
the first CI run of it found a latent SIGPIPE race that had never shown
locally — proof this class of gap is real, not hypothetical.

This script closes the gap generally rather than for those three scripts by
name: it finds every script that DECLARES a `--selftest` mode (a real argument
branch — see DECLARATION FORMS below — never a mere mention in a comment or a
usage string) and requires each one to be reachable from `--selftest`, either
directly (some `.github/workflows/*.yml` step invokes it with that flag) or
through exactly one named caller (CALLED_BY, below) whose own source is read
and shown to actually forward `--selftest` to it. A script that fails both is
UNWIRED, and that is this script's whole verdict.

    scripts/check-selftests-wired.py             check the tree
    scripts/check-selftests-wired.py --selftest  prove the logic above on a
                                                  fabricated tree — never the
                                                  real one

DECLARATION FORMS. Detection is a strict, repo-derived pattern per language —
not a search for the substring "--selftest" — because that substring appears
constantly in comments, docstrings and usage lines that declare nothing. Every
form below is the exact shape an existing script in this tree uses today
(confirmed by reading each one; see the PR that added this file):

  .sh   `if [ "${1:-}" = "--selftest" ]; then`   (the majority: abi-channel.sh,
        check-linked-build.sh, check-standalone.sh, check-suite.sh,
        lint-cited-paths.sh, lint-public.sh, published-lines.sh)
        or a `case` arm spelled exactly `--selftest)` (index-diff.sh,
        lint-spelling.sh)
  .py   `argparse`'s `.add_argument("--selftest", ...)` (one-line or spread
        across several, as sweep-public-mentions.py does — matched with `\s`,
        which spans newlines) — or a hand-rolled argv check:
        `"--selftest" in argv`, `argv == ["--selftest"]`, or
        `argv[:1] == ["--selftest"]` (three different scripts, three
        different spellings, all real)
  .go   `arg == "--selftest"` inside a loop over os.Args
        (check-reserved-test-names.go)

Before matching, every FULL-LINE comment (a line whose stripped text starts
with `#` for .sh/.py, `//` for .go) is dropped from the file's text first —
so a usage line like `#   scripts/foo.sh --selftest   prove ...` never counts
as a declaration, no matter how closely it resembles one. A trailing comment
on an otherwise-real code line is not stripped, but none of the patterns above
can be satisfied by comment text alone, so this is enough without a real
parser for any of the three languages.

CALLED_BY. A script whose OWN `--selftest` is never invoked directly by a
workflow can still be covered when its caller's `--selftest` run already
covers it — scripts/check-suite.sh's own selftest calls
`scripts/lib/provenance.py --selftest` as part of proving itself, so wiring
just the caller in CI is enough. This is a small, explicit map (never
inferred) and every entry is VERIFIED by reading the caller's own text, never
trusted on the map's word alone: the caller must actually invoke the callee
by name together with either the literal string `--selftest` on the same
line, or a full-argument-forwarding construct (`"$@"`, `os.Args[1:]`,
`sys.argv[1:]`, `argv[1:]`) on a line naming the callee — the second shape
covers scripts/check-reserved-test-names.sh, which has no `--selftest`
branch of its own and instead `exec`s straight into
scripts/check-reserved-test-names.go with its arguments forwarded unchanged,
so whatever wires the wrapper's own invocation with `--selftest` (ci.yml does)
reaches the callee too. A CALLED_BY caller must itself be wired directly by a
workflow — this script does not chase a caller-of-a-caller.

Known entries today, each confirmed by reading both files:

  scripts/lib/provenance.py         via scripts/check-suite.sh
  scripts/lib/standalone_census.py  via scripts/check-standalone.sh
  scripts/check-reserved-test-names.go via scripts/check-reserved-test-names.sh

Prints one line per script that declares a `--selftest` mode — `wired: ci.yml`,
`wired: via <caller>`, or `UNWIRED` (with the reason) — and exits 1 if any
script is UNWIRED.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# One or more patterns per extension; a file matches if ANY pattern searches
# true against its comment-stripped text. Each is the exact shape measured in
# this repository today (see the module docstring) — not a loose grep for the
# substring "--selftest", which every usage line and doc comment also carries.
SELFTEST_PATTERNS: dict[str, list[re.Pattern]] = {
    ".sh": [
        # if [ "${1:-}" = "--selftest" ]; then   (also tolerates a bare "$1")
        re.compile(r'"\$\{?1(:-)?\}?"\s*=\s*"--selftest"'),
        # a `case ... in` arm spelled exactly --selftest)
        re.compile(r"(?m)^\s*--selftest\)\s*$"),
    ],
    ".py": [
        # argparse: ap.add_argument("--selftest", action="store_true") — \s
        # matches newlines too, so the multi-line form (sweep-public-mentions.py)
        # is caught the same as the one-line form.
        re.compile(r'\.add_argument\(\s*["\']--selftest["\']'),
        # "--selftest" in argv
        re.compile(r'["\']--selftest["\']\s+in\s+argv\b'),
        # argv == ["--selftest"]  or  argv[:1] == ["--selftest"]
        re.compile(r'argv(\[:1\])?\s*==\s*\[\s*["\']--selftest["\']\s*\]'),
    ],
    ".go": [
        # for _, arg := range os.Args { if arg == "--selftest" { ... } }
        re.compile(r'\barg\s*==\s*["\']--selftest["\']'),
    ],
}

# callee (relative to repo root) -> caller (relative to repo root). Every
# entry is verified against the caller's actual text below — never trusted
# on the map's word alone (see the module docstring's CALLED_BY section).
CALLED_BY: dict[str, str] = {
    "scripts/lib/provenance.py": "scripts/check-suite.sh",
    "scripts/lib/standalone_census.py": "scripts/check-standalone.sh",
    "scripts/check-reserved-test-names.go": "scripts/check-reserved-test-names.sh",
}

# A caller invoking the callee with its arguments forwarded unchanged reaches
# it with --selftest whenever the CALLER itself was invoked that way — this is
# the shape scripts/check-reserved-test-names.sh uses (`exec ... "$@"`), which
# carries no literal "--selftest" text of its own.
FORWARDING_RE = re.compile(r'"\$@"|\$@|os\.Args\[1:\]|sys\.argv\[1:\]|\bargv\[1:\]')


def _comment_marker(ext: str) -> str | None:
    if ext in (".sh", ".py"):
        return "#"
    if ext == ".go":
        return "//"
    return None


def strip_full_comment_lines(text: str, ext: str) -> str:
    """Drop every line whose stripped text is ENTIRELY a comment. A trailing
    comment on a real code line is left alone — none of SELFTEST_PATTERNS can
    be satisfied by comment text alone, so this is enough without a real
    parser, and it is what keeps a usage-doc line like
    `#   scripts/foo.sh --selftest   prove ...` from ever counting."""
    marker = _comment_marker(ext)
    if marker is None:
        return text
    kept = [line for line in text.splitlines() if not line.lstrip().startswith(marker)]
    return "\n".join(kept)


def declares_selftest(path: Path) -> bool:
    ext = path.suffix
    patterns = SELFTEST_PATTERNS.get(ext)
    if not patterns:
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    stripped = strip_full_comment_lines(text, ext)
    return any(p.search(stripped) for p in patterns)


def find_declaring_scripts(root: Path) -> list[str]:
    """Every file under <root>/scripts (recursively) that declares its own
    --selftest mode, as a repo-relative posix path, sorted."""
    scripts_dir = root / "scripts"
    if not scripts_dir.is_dir():
        return []
    found = []
    for path in scripts_dir.rglob("*"):
        if not path.is_file() or path.suffix not in SELFTEST_PATTERNS:
            continue
        if declares_selftest(path):
            found.append(path.relative_to(root).as_posix())
    return sorted(found)


def wired_scripts_in_workflows(root: Path) -> set[str]:
    """Every scripts/... path invoked with --selftest on some non-comment line
    of some .github/workflows/*.yml file. A YAML `#` comment (the line's
    stripped text starts with it) is skipped first — ci.yml itself carries two
    such lines, prose pointing a reader at a selftest rather than running one,
    and counting them would silently satisfy this check without CI ever
    running anything."""
    wf_dir = root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return set()
    wired: set[str] = set()
    path_re = re.compile(r"(scripts/[\w./-]+)")
    for wf in sorted(wf_dir.glob("*.yml")):
        try:
            text = wf.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            if "--selftest" not in line:
                continue
            for m in path_re.finditer(line):
                wired.add(m.group(1))
    return wired


def verify_called_by(root: Path, callee: str, caller: str) -> tuple[bool, str]:
    """Read the CALLER's own text and confirm it actually invokes the CALLEE
    with --selftest — either a literal `--selftest` on the same line as the
    callee's name, or a full-argument-forwarding construct on that line
    (which reaches --selftest whenever the caller itself is invoked with it).
    Never trusts the CALLED_BY map's word alone."""
    caller_path = root / caller
    if not caller_path.is_file():
        return False, f"caller {caller} does not exist"
    ext = caller_path.suffix
    try:
        text = caller_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return False, f"caller {caller} unreadable ({e})"
    stripped = strip_full_comment_lines(text, ext)
    callee_name = Path(callee).name
    for line in stripped.splitlines():
        if callee_name not in line:
            continue
        if "--selftest" in line or FORWARDING_RE.search(line):
            return True, ""
    return (
        False,
        f"{caller} does not invoke {callee_name} with a literal --selftest "
        f"or full-argument forwarding on the same line",
    )


def run_check(root: Path, called_by: dict[str, str]) -> tuple[list[tuple[str, str, str]], bool]:
    """Returns (results, any_unwired). results is one (script, status, detail)
    triple per script that declares --selftest, in sorted order — status is
    exactly 'wired: ci.yml', 'wired: via <caller>', or 'UNWIRED', and detail is
    the reason for an UNWIRED verdict (empty otherwise)."""
    declared = find_declaring_scripts(root)
    wired_by_ci = wired_scripts_in_workflows(root)

    results: list[tuple[str, str, str]] = []
    any_unwired = False
    for script in declared:
        if script in wired_by_ci:
            results.append((script, "wired: ci.yml", ""))
            continue
        caller = called_by.get(script)
        if caller is not None:
            ok, reason = verify_called_by(root, script, caller)
            if ok and caller in wired_by_ci:
                results.append((script, f"wired: via {caller}", ""))
                continue
            if ok and caller not in wired_by_ci:
                results.append(
                    (script, "UNWIRED", f"CALLED_BY caller {caller} is itself not wired by any workflow")
                )
                any_unwired = True
                continue
            results.append((script, "UNWIRED", reason))
            any_unwired = True
            continue
        results.append(
            (script, "UNWIRED", "no workflow invokes it with --selftest, and it is not in CALLED_BY")
        )
        any_unwired = True
    return results, any_unwired


# --------------------------------------------------------------- selftest


def selftest() -> int:
    import tempfile

    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # 1. A script with a real --selftest branch and no workflow call at all
    #    is reported UNWIRED.
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        (tmp / "scripts").mkdir(parents=True)
        (tmp / ".github" / "workflows").mkdir(parents=True)
        (tmp / "scripts" / "foo.sh").write_text(
            '#!/usr/bin/env bash\nif [ "${1:-}" = "--selftest" ]; then\n  echo ok\n  exit 0\nfi\necho "real run"\n'
        )
        (tmp / ".github" / "workflows" / "ci.yml").write_text(
            "name: ci\njobs:\n  x:\n    steps:\n      - run: scripts/foo.sh --not-selftest\n"
        )
        results, any_unwired = run_check(tmp, {})
        check(any_unwired, "an unwired script with a real --selftest branch was not flagged")
        statuses = {s: st for s, st, _ in results}
        check(
            statuses.get("scripts/foo.sh") == "UNWIRED",
            f"scripts/foo.sh should be UNWIRED, got {statuses.get('scripts/foo.sh')!r}",
        )

    # 2. A comment-only mention of --selftest declares NOTHING — it must not
    #    even appear in the declaring-scripts list, whatever forms it takes
    #    (sh #, and a python # usage line, and a go // one).
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        (tmp / "scripts").mkdir(parents=True)
        (tmp / "scripts" / "bar.sh").write_text(
            "#!/usr/bin/env bash\n"
            "#   scripts/bar.sh --selftest   prove the thing below actually fires\n"
            'echo "bar, no selftest branch at all"\n'
        )
        (tmp / "scripts" / "baz.py").write_text(
            "#!/usr/bin/env python3\n"
            '# scripts/baz.py --selftest    (usage doc only; argv is never checked)\n'
            "print('baz')\n"
        )
        (tmp / "scripts" / "qux.go").write_text(
            "package main\n\n// scripts/qux.go --selftest  (mentioned, never checked)\n"
            'func main() { println("qux") }\n'
        )
        declared = find_declaring_scripts(tmp)
        check(
            declared == [],
            f"comment-only --selftest mentions were read as declarations: {declared}",
        )

    # 3. A CALLED_BY entry whose caller does not actually invoke the callee's
    #    --selftest (not by name, not by forwarding) is rejected.
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        (tmp / "scripts" / "lib").mkdir(parents=True)
        (tmp / ".github" / "workflows").mkdir(parents=True)
        (tmp / "scripts" / "lib" / "helper.py").write_text(
            "#!/usr/bin/env python3\nimport sys\n\n\ndef main(argv):\n"
            '    if "--selftest" in argv:\n        return 0\n    return 1\n\n\n'
            'if __name__ == "__main__":\n    sys.exit(main(sys.argv[1:]))\n'
        )
        (tmp / "scripts" / "wrapper.sh").write_text(
            '#!/usr/bin/env bash\necho "wrapper never mentions helper.py at all"\n'
        )
        (tmp / ".github" / "workflows" / "ci.yml").write_text(
            "name: ci\njobs:\n  x:\n    steps:\n      - run: scripts/wrapper.sh --selftest\n"
        )
        called_by = {"scripts/lib/helper.py": "scripts/wrapper.sh"}
        ok, reason = verify_called_by(tmp, "scripts/lib/helper.py", "scripts/wrapper.sh")
        check(not ok, "verify_called_by accepted a caller that never mentions its callee")
        check(bool(reason), "a rejected CALLED_BY entry gave no reason")
        results, any_unwired = run_check(tmp, called_by)
        statuses = {s: st for s, st, _ in results}
        check(any_unwired, "a bad CALLED_BY mapping was not flagged as unwired")
        check(
            statuses.get("scripts/lib/helper.py") == "UNWIRED",
            f"scripts/lib/helper.py should be UNWIRED via a bad CALLED_BY entry, "
            f"got {statuses.get('scripts/lib/helper.py')!r}",
        )

    # 4. An all-wired tree passes: one script wired directly, one wired only
    #    through a CALLED_BY caller that genuinely forwards to it (and is
    #    itself directly wired) — the exact shape
    #    scripts/check-reserved-test-names.go / .sh is in today.
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        (tmp / "scripts" / "lib").mkdir(parents=True)
        (tmp / ".github" / "workflows").mkdir(parents=True)
        (tmp / "scripts" / "a.sh").write_text(
            '#!/usr/bin/env bash\nif [ "${1:-}" = "--selftest" ]; then\n  echo ok\n  exit 0\nfi\n'
        )
        (tmp / "scripts" / "lib" / "b.py").write_text(
            "#!/usr/bin/env python3\nimport sys\n\n\ndef main(argv):\n"
            '    if argv == ["--selftest"]:\n        return 0\n    return 1\n\n\n'
            'if __name__ == "__main__":\n    sys.exit(main(sys.argv[1:]))\n'
        )
        (tmp / "scripts" / "c.sh").write_text(
            '#!/usr/bin/env bash\n# forwards every argument, including --selftest, unchanged\n'
            'exec python3 "$(dirname "$0")/lib/b.py" "$@"\n'
        )
        (tmp / ".github" / "workflows" / "ci.yml").write_text(
            "name: ci\njobs:\n  x:\n    steps:\n"
            "      - run: scripts/a.sh --selftest\n"
            "      - run: scripts/c.sh --selftest\n"
        )
        called_by = {"scripts/lib/b.py": "scripts/c.sh"}
        results, any_unwired = run_check(tmp, called_by)
        statuses = {s: st for s, st, _ in results}
        check(not any_unwired, f"an all-wired tree was flagged unwired: {results}")
        check(
            statuses.get("scripts/a.sh") == "wired: ci.yml",
            f"scripts/a.sh should be wired: ci.yml, got {statuses.get('scripts/a.sh')!r}",
        )
        check(
            statuses.get("scripts/lib/b.py") == "wired: via scripts/c.sh",
            f"scripts/lib/b.py should be wired via scripts/c.sh, got {statuses.get('scripts/lib/b.py')!r}",
        )

    # 5. This script's own real CALLED_BY map, run against the real
    #    repository root, must find every known entry's caller actually
    #    forwarding --selftest to its callee — proving the map is not merely
    #    plausible-looking but verified against today's text.
    for callee, caller in CALLED_BY.items():
        ok, reason = verify_called_by(ROOT, callee, caller)
        check(ok, f"CALLED_BY[{callee!r}] = {caller!r} did not verify: {reason}")

    if failures:
        for f in failures:
            print(f"SELFTEST FAILED: {f}", file=sys.stderr)
        return 1
    print(
        "check-selftests-wired: selftest ok — an unwired --selftest branch is caught, a comment-only "
        "mention declares nothing in sh/py/go alike, a CALLED_BY entry whose caller does not actually "
        "forward --selftest is rejected with a reason, an all-wired tree (direct plus CALLED_BY) passes, "
        "and this repository's own CALLED_BY map verifies against today's real files"
    )
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true", help="run this script's own correctness gate")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    results, any_unwired = run_check(ROOT, CALLED_BY)
    for script, status, detail in results:
        if status == "UNWIRED":
            print(f"{status}: {script} — {detail}")
        else:
            print(f"{status}: {script}")
    if any_unwired:
        print("check-selftests-wired: at least one script above declares --selftest but nothing in CI runs it", file=sys.stderr)
        return 1
    print(f"check-selftests-wired: all {len(results)} script(s) with a --selftest mode are wired")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
