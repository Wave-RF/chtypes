#!/usr/bin/env bash
# check-examples-v1.sh — the examples-tour gate for v1: every binding's tour
# must run every section, and a binding whose tour is not yet on the v1 API
# is SKIPPED LOUDLY by name, never silently.
#
#   scripts/check-examples-v1.sh               run the gate
#   scripts/check-examples-v1.sh --selftest    prove both verdicts on a fabricated tree
#
# WHY. The v0 `artifacts` job ran `examples/chplay.sh --require-all --locked`
# and was the only required check that did; retiring it left the tours gating
# nothing, and a tour that was broken (0 of 17 sections) went unnoticed. This
# is its v1 successor.
#
# THE ENROLLMENT RULE. A tour is written against its binding's public API.
# Until a binding's v1 API lands, its tour cannot run, and a v1 library needs a
# fetched artifact the producer has not published for every platform. So each
# binding opts in by adding the file examples/<lang>/V1_READY in the push where
# its tour runs against v1 (the same rule as the conformance enrollment
# markers). An un-enrolled binding prints `SKIPPED examples/<lang>: ...` and
# does not fail; an ENROLLED binding runs `examples/chplay.sh --require-all
# --locked <lang>` and the gate fails if that fails. When every binding is
# enrolled there are no skips left; until then the job's own summary says how
# many bindings were skipped. $CHTYPES_REGISTRY, if the tours need one, is
# whatever the calling job exported.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LANGS=(go python ts rust)

gate() {
  local root="$1" chplay="$2" lang ran=0 skipped=0 failed=0
  for lang in "${LANGS[@]}"; do
    if [ ! -e "$root/examples/$lang/V1_READY" ]; then
      echo "SKIPPED examples/$lang: its tour is not enrolled on the v1 API yet (add examples/$lang/V1_READY in the push where the tour runs against v1)"
      skipped=$((skipped + 1))
      continue
    fi
    if "$chplay" --require-all --locked "$lang"; then
      echo "RAN examples/$lang: every section ran"
      ran=$((ran + 1))
    else
      echo "FAILED examples/$lang: the tour did not run all its sections" >&2
      failed=$((failed + 1))
    fi
  done
  echo "examples: $ran ran, $skipped skipped, $failed failed"
  [ "$failed" -eq 0 ]
}

selftest() {
  local rc
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/check-examples-selftest.XXXXXX")"
  trap 'rm -rf "${tmp:-}"' EXIT
  mkdir -p "$tmp/examples/go" "$tmp/examples/python" "$tmp/examples/ts" "$tmp/examples/rust"
  printf '#!/usr/bin/env bash\nexit 0\n' > "$tmp/ok.sh"
  printf '#!/usr/bin/env bash\nexit 1\n' > "$tmp/bad.sh"
  chmod +x "$tmp/ok.sh" "$tmp/bad.sh"
  gate "$tmp" "$tmp/bad.sh" > "$tmp/out" 2>&1 || { echo "SELFTEST FAILED: all-skipped read as a failure" >&2; return 1; }
  [ "$(grep -c '^SKIPPED examples/' "$tmp/out")" -eq 4 ] || { echo "SELFTEST FAILED: a skip was not named" >&2; return 1; }
  : > "$tmp/examples/ts/V1_READY"
  gate "$tmp" "$tmp/ok.sh" > "$tmp/out" 2>&1 || { echo "SELFTEST FAILED: an enrolled passing tour failed" >&2; return 1; }
  grep -q '^RAN examples/ts' "$tmp/out" || { echo "SELFTEST FAILED: the run was not named" >&2; return 1; }
  gate "$tmp" "$tmp/bad.sh" > "$tmp/out" 2>&1 && rc=0 || rc=1
  [ "$rc" -eq 1 ] || { echo "SELFTEST FAILED: an enrolled failing tour passed" >&2; return 1; }
  echo "check-examples-v1: selftest ok — unenrolled tours skip by name, an enrolled passing tour runs, an enrolled failing tour fails"
}

if [ "${1:-}" = "--selftest" ]; then
  selftest
  exit $?
fi
gate "$ROOT" "$ROOT/examples/chplay.sh"
