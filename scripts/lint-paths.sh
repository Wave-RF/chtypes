#!/usr/bin/env bash
# lint-paths.sh — no tracked file may cite a repository path that does not exist.
#
# WHY THIS EXISTS. `docs/proposals/rows-export.md` was cited six times — in
# include/chtypes.h and in all four bindings' source, every one of which ships
# inside the crate, the sdist, the module and the npm tarball — for a file that
# is not in this repository. Four more dead citations were found the same way
# (`docs/defaults-matrix.md`, `docs/type-coverage.md`, `docs/fetch.md` after it
# moved under guides/, and a `scripts/check-parity-doc.sh` that has never
# existed while the check it named was real and living in the Python suite).
#
# Each was a pointer a public reader cannot follow. lint-public.sh catches the
# private repository by NAME; this catches the other half of the same failure —
# a path that simply is not there. Between them a sweep that "removed" a
# pointer by changing its shape cannot pass quietly again.
#
#   scripts/lint-paths.sh             check every tracked file
#   scripts/lint-paths.sh --selftest  prove the rule fires, and does not overfire
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

scan() {
  ( cd "$1" && git ls-files -z | python3 -c '
import os, re, sys

# Top-level directories whose names are unambiguous repository paths.
TOP = r"(?:docs|examples|scripts|include|goldens|spec|tests)"
# A real extension is required: a bare word after a slash is a glob or prose.
PAT = re.compile(r"(?<![A-Za-z0-9_./-])(" + TOP + r"/[A-Za-z0-9_./-]*\.[A-Za-z][A-Za-z0-9]{1,4})\b")
# Skipped, each for a reason rather than to make the check pass:
#   fixtures      generated upstream and verified byte-identical there
#   .gitignore    holds patterns, not paths
#   Cargo.toml    its paths are crate-relative by definition
#   CHANGELOG.md  deliberately names things that were removed
#   this script   it quotes the shapes it looks for
def skipped(f):
    b = os.path.basename(f)
    return (f.startswith("tests/fixtures/fetch/") or b in
            (".gitignore", "Cargo.toml", "CHANGELOG.md", "lint-paths.sh"))

# This repository is four sibling packages, and a cited path may be relative to
# any of their roots rather than to the repository — a doc-comment inside rust/
# naming tests/integration.rs, or a script that cds into python/ before naming
# tests/test_parity.py. Accept a path that resolves under any of the four.
ROOTS = ("go", "python", "ts", "rust")
bad = []
for f in sys.stdin.read().split("\0"):
    if not f or skipped(f): continue
    try: text = open(f, encoding="utf-8", errors="ignore").read()
    except OSError: continue
    for i, line in enumerate(text.splitlines(), 1):
        for m in PAT.finditer(line):
            p = m.group(1).rstrip(".,;:)")
            if os.path.exists(p): continue
            if any(os.path.exists(os.path.join(r, p)) for r in ROOTS): continue
            bad.append((f, i, p))
for f, i, p in bad:
    print(f"    {f}:{i}: {p}", file=sys.stderr)
if bad:
    print(f"lint-paths: {len(bad)} citation(s) of a path that is not in this repository", file=sys.stderr)
    print("  A reader cannot follow it. Cite something that exists, or say what the thing IS.", file=sys.stderr)
    sys.exit(1)
' )
}

if [ "${1:-}" = "--selftest" ]; then
  # The rule must FIRE on a dead path and must NOT fire on a live one or on a
  # language-relative one. A check that silently matches nothing is the failure
  # this script exists to prevent, so prove all three.
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  git init -q "$tmp"
  mkdir -p "$tmp/docs/guides" "$tmp/rust/tests" "$tmp/go"
  printf 'ok\n'                       > "$tmp/docs/guides/fetch.md"
  printf 'ok\n'                       > "$tmp/rust/tests/integration.rs"
  printf 'see docs/gone/missing.md\n' > "$tmp/planted.md"
  printf 'see docs/guides/fetch.md\n' > "$tmp/live.md"
  printf '// see tests/integration.rs\n' > "$tmp/rust/lib.rs"
  printf 'run pytest tests/integration.rs from rust/\n' > "$tmp/sibling.md"
  git -C "$tmp" add -A
  out="$(scan "$tmp" 2>&1)" && { echo "SELFTEST FAILED: the planted dead path was not caught" >&2; exit 1; }
  printf '%s\n' "$out" | grep -q 'planted.md' || { echo "SELFTEST FAILED: rule did not fire on planted.md" >&2; exit 1; }
  printf '%s\n' "$out" | grep -q 'live.md'    && { echo "SELFTEST FAILED: a path that exists was flagged" >&2; exit 1; }
  printf '%s\n' "$out" | grep -q 'rust/lib.rs' && { echo "SELFTEST FAILED: a crate-relative path was flagged" >&2; exit 1; }
  echo "lint-paths: selftest ok — fires on a dead path, silent on live and crate-relative ones"
  exit 0
fi

scan "$HERE"
echo "lint-paths: ok — every cited repository path exists"
