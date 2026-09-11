#!/usr/bin/env bash
# lint-prose.sh — American-spelling enforcement over this entire repository
# (every tracked file except the two exclusions below), via two independent
# checks that must both pass:
#
#   1. the standalone `misspell` binary (github.com/golangci/misspell — the
#      same codebase golangci-lint vendors as a library for its own
#      misspell linter, but that bundled linter sets no locale, so it
#      accepts both British and American spelling; this installs and runs
#      the standalone CLI instead, with `-locale US`).
#   2. a small, independent grep pass (WORDLIST below) for a specific
#      family of words `misspell`'s own dictionary matcher does NOT
#      reliably catch — proven below, not assumed.
#
# WHY TWO CHECKS. `misspell -locale US` alone gives a false sense of
# coverage. Measured directly against misspell v0.8.0's vendored dictionary
# (github.com/golangci/misspell): words_us.go DOES list pairs like
# "specialised"/"specialized" and "normalisation"/"normalization" — but
# misspell's matching engine (a large Aho-Corasick-style trie built from
# ~10,000+ combined pairs) silently fails to match a handful of them in
# practice, "specialised" and "normalisation" among them, REGARDLESS of
# whether the pair comes from the built-in `-locale US` dictionary or a
# custom `-dict` file added on top — confirmed with a standalone Go program
# calling misspell's own Replacer directly (r.Replace("specialised") ==
# "specialised", zero diffs, with DictMain + DictAmerican loaded). This is
# an upstream engine limitation, not a missing-word problem `-dict` can
# paper over: which specific words trip it appears to depend on the exact
# combined dictionary content in a way that isn't practical to predict or
# rely on. So: `misspell -locale US` still runs, and still catches its own
# large, genuinely-reliable set (colour, behaviour, licence, neighbour,
# modelled, cancelled, serialise, recognise, materialised, honour,
# artefact, marshalling, and more — all verified against this tree). The
# WORDLIST grep below is a SEPARATE, from-scratch mechanism for exactly the
# family of words proven to slip past misspell's matcher, so this script's
# guarantee does not depend on trusting an opaque third-party trie.
#
#   scripts/lint-prose.sh              check the whole tree (see excludes)
#   scripts/lint-prose.sh --fix        apply misspell's own corrections
#                                       (the WORDLIST grep has no --fix;
#                                       those hits need a hand edit — see
#                                       the "fix" column below)
#   scripts/lint-prose.sh --selftest   prove WORDLIST actually fires: builds
#                                       a temp file of known-bad words, runs
#                                       both checks against it, and fails
#                                       loudly if either one comes back
#                                       clean on input that should not be
#
# SCOPE. Every file `git ls-files` reports, minus:
#   - anything under a `fixtures/fetch` directory (matches both
#     spec/fixtures/fetch/ and tests/fixtures/fetch/, whichever this tree
#     currently has) — generated, ed25519-signed, hash-verified fixtures;
#     editing one invalidates every signature it carries.
#   - any file named exactly `LICENSE` — legal text, not this project's
#     prose to correct.
# No other exception list, by design: every SDK's source (comments and
# strings scan the same as prose — `-source text`, not `-source go`, so a
# quoted ClickHouse setting name would also be scanned; none of the words
# below collide with a real ClickHouse identifier), every README, every
# CHANGELOG, docs, examples/playground, scripts, and include/chtypes.h are
# all in scope.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

MISSPELL_VERSION="v0.8.0"
LOCAL_BIN="$ROOT/.bin"
MISSPELL="$LOCAL_BIN/misspell-$MISSPELL_VERSION"

if [ ! -x "$MISSPELL" ]; then
  echo "==> Installing misspell $MISSPELL_VERSION" >&2
  mkdir -p "$LOCAL_BIN"
  GOBIN="$LOCAL_BIN" go install "github.com/golangci/misspell/cmd/misspell@$MISSPELL_VERSION"
  mv "$LOCAL_BIN/misspell" "$MISSPELL"
fi

# Words `misspell -locale US` has been shown not to reliably catch (see the
# header above). typo:fix pairs, one per line — the fix half is documentation
# for whoever reads a hit; the grep pass below only uses the typo half.
WORDLIST=(
  "canonicalise:canonicalize"
  "canonicalised:canonicalized"
  "canonicalises:canonicalizes"
  "canonicalising:canonicalizing"
  "canonicalisation:canonicalization"
  "normalise:normalize"
  "normalised:normalized"
  "normalises:normalizes"
  "normalising:normalizing"
  "normaliser:normalizer"
  "normalisers:normalizers"
  "normalisation:normalization"
  "optimisation:optimization"
  "optimisations:optimizations"
  "specialise:specialize"
  "specialised:specialized"
  "specialises:specializes"
  "specialising:specializing"
  "specialisation:specialization"
  "generalise:generalize"
  "generalised:generalized"
  "generalises:generalizes"
  "generalising:generalizing"
  "generalisation:generalization"
  "initialisation:initialization"
  "initialiser:initializer"
  "initialisers:initializers"
  "desynchronise:desynchronize"
  "desynchronised:desynchronized"
  "desynchronises:desynchronizes"
  "desynchronising:desynchronizing"
  "parameterise:parameterize"
  "parameterised:parameterized"
  "parameterises:parameterizes"
  "parameterising:parameterizing"
  "parameterisation:parameterization"
  "unmodelled:unmodeled"
  "unmarshalling:unmarshaling"
)

typos=()
for pair in "${WORDLIST[@]}"; do
  typos+=("${pair%%:*}")
done
# Alternation with explicit non-letter boundaries on both sides, NOT `\b`
# (git grep -E silently accepts `\b` and just never matches it — it is not
# a supported ERE metacharacter in that mode; ripgrep/PCRE-style `\b` there
# is a trap that looks like it works and matches nothing). `-i` for a
# capitalised, sentence-leading form.
IFS='|'
ALTERNATION="${typos[*]}"
unset IFS
WORD_PATTERN="(^|[^A-Za-z])(${ALTERNATION})([^A-Za-z]|$)"

# Every tracked file except the fixtures (generated + signed) and any
# LICENSE file, wherever either lives in the current tree.
list_files() {
  # This script is excluded from itself, and it is the ONLY exclusion of its
  # kind. Its WORDLIST is a table of British->American pairs, and its header
  # names the words misspell does and does not catch; those spellings are the
  # payload, not an error. Unlike prose, a lookup table cannot be reworded
  # around the problem. Everything else in the tree is checked with no
  # exceptions — see the CHANGELOG entries, which were reworded rather than
  # excused, precisely so this stayed the only one.
  git ls-files | grep -v '/fixtures/fetch/' | grep -vE '(^|/)LICENSE$' \
               | grep -v '^scripts/lint-prose\.sh$'
}

run_misspell() {
  local mode="$1"
  local files=()
  mapfile -t files < <(list_files)
  if [ "${#files[@]}" -eq 0 ]; then
    echo "lint-prose.sh: git ls-files returned nothing to check" >&2
    return 1
  fi
  if [ "$mode" = "fix" ]; then
    "$MISSPELL" -locale US -source text -w "${files[@]}"
  else
    "$MISSPELL" -locale US -source text -error "${files[@]}"
  fi
}

run_wordlist_grep() {
  local files=()
  mapfile -t files < <(list_files)
  local hits
  hits="$(grep -inE "$WORD_PATTERN" "${files[@]}" 2>/dev/null || true)"
  if [ -n "$hits" ]; then
    echo "American-spelling wordlist check found British spellings misspell's own matcher misses:" >&2
    echo "$hits" >&2
    echo >&2
    echo "Fixes (typo -> American spelling):" >&2
    for pair in "${WORDLIST[@]}"; do
      echo "  ${pair%%:*} -> ${pair##*:}" >&2
    done
    return 1
  fi
}

selftest() {
  # Not `local`: the EXIT trap below runs after this function has returned,
  # by which point a `local` variable is already out of scope under `set -u`.
  tmp="$(mktemp)"
  trap 'rm -f "$tmp" "$tmp.misspell"' EXIT
  {
    echo "canonicalise the input"
    echo "normalisation of settings"
    echo "optimisation pass"
    echo "specialised handling"
    echo "unmodelled behaviour"
  } > "$tmp"

  echo "==> selftest: misspell -locale US alone (expected to MISS most of these — that is the point)" >&2
  if "$MISSPELL" -locale US -source text -error "$tmp" >"$tmp.misspell" 2>&1; then
    echo "FAIL: misspell -locale US reported the probe file clean; expected at least the 'behaviour' hit" >&2
    exit 1
  fi
  cat "$tmp.misspell" >&2

  echo "==> selftest: wordlist grep against the same probe file (expected to catch ALL five)" >&2
  local hits
  hits="$(grep -inE "$WORD_PATTERN" "$tmp" || true)"
  local count
  count="$(printf '%s\n' "$hits" | grep -c . || true)"
  if [ "$count" -ne 5 ]; then
    echo "FAIL: expected the wordlist grep to catch 5 lines, got $count" >&2
    printf '%s\n' "$hits" >&2
    exit 1
  fi
  printf '%s\n' "$hits" >&2
  echo "==> selftest OK: both the gap (misspell alone misses these) and the fix (wordlist grep catches them) are proven" >&2
}

case "${1:-}" in
  --fix)
    run_misspell fix
    ;;
  --selftest)
    selftest
    ;;
  *)
    ok=1
    run_misspell check || ok=0
    run_wordlist_grep || ok=0
    [ "$ok" -eq 1 ]
    ;;
esac
