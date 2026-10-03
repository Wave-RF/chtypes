#!/usr/bin/env bash
# scripts/fetch-v1/sandbox.sh — run a command with NO network except loopback,
# as the calling (unprivileged) user, for the v1 conformance suite.
#
#   scripts/fetch-v1/sandbox.sh -- <command> [args...]   run <command> sandboxed
#   scripts/fetch-v1/sandbox.sh --selftest               prove the sandbox (Linux, passwordless sudo)
#
# Mechanism: `sudo unshare --net` gives the command a fresh network namespace
# whose only interface is its own loopback; `setpriv` then drops back to the
# caller's own uid and gid before exec. No iptables, no dedicated group, and
# nothing on the runner's own connectivity changes — the namespace exists only
# for this one process tree. It does not depend on sudo's run-as-GROUP
# permission: a previous attempt used `sudo -g noegress`, which the hosted
# runner's sudoers refuses (measured: the step failed in about one second with
# "cannot reach 127.0.0.1" before any curl ran), and whose "unreachable" proof
# counted any nonzero result — a refused wrapper included — as success.
#
# Environment: `sudo -E` keeps the caller's variables (the CHTYPES_V1_* set,
# GOPATH, CARGO_HOME, UV_*, …); PATH and HOME are passed again explicitly,
# because sudo may rewrite them (secure_path, home reset). --selftest proves
# a marker variable, PATH and HOME all survive.
#
# Proofs (each prints its exact exit code; a wrapper failure never counts as a
# pass because every proof also needs a positive result from inside):
#   1. `sandbox true` exits 0, and `sandbox id -u` is the caller's uid, not 0.
#   2. https://github.com from inside: curl's own exit code must be 6
#      (could not resolve) or 7 (could not connect); anything else is an error.
#   3. loopback from inside: a listener started INSIDE the same namespace
#      (a new netns has its own loopback) answers a curl, and serves a real
#      fixture path through scripts/fetch-v1/server.py.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SELF="$HERE/sandbox.sh"

die() { echo "sandbox.sh: $*" >&2; exit 1; }

sandbox() {
  local unshare_bin ip_bin setpriv_bin env_bin
  unshare_bin="$(command -v unshare)" || die "unshare not found"
  ip_bin="$(command -v ip)" || die "ip not found"
  setpriv_bin="$(command -v setpriv)" || die "setpriv not found"
  env_bin="$(command -v env)" || die "env not found"
  sudo -E "$unshare_bin" --net -- bash -c '
    set -e
    ip_bin=$1 setpriv_bin=$2 env_bin=$3 uid=$4 gid=$5 path=$6 home=$7
    shift 7
    "$ip_bin" link set lo up
    exec "$setpriv_bin" --reuid="$uid" --regid="$gid" --init-groups \
      "$env_bin" PATH="$path" HOME="$home" "$@"
  ' sandbox-inner "$ip_bin" "$setpriv_bin" "$env_bin" "$(id -u)" "$(id -g)" "$PATH" "${HOME:-/}" "$@"
}

# Runs INSIDE the sandbox: server.py and its client share one loopback.
inner_loopback() {
  local tmp out pid port
  tmp="$(mktemp -d)"
  out="$tmp/server.out"
  python3 "$HERE/server.py" --fixtures "$ROOT/tests/fixtures/fetch-v1" --port 0 >"$out" 2>"$tmp/server.err" &
  pid=$!
  # shellcheck disable=SC2064
  trap "kill $pid 2>/dev/null || true; rm -rf '$tmp'" EXIT
  port=""
  for _ in $(seq 1 50); do
    port="$(awk '$1 == "LISTENING" { print $2; exit }' "$out" 2>/dev/null || true)"
    [ -n "$port" ] && break
    sleep 0.2
  done
  [ -n "$port" ] || { cat "$tmp/server.err" >&2; die "server.py did not report a port inside the sandbox"; }
  # A real fixture path: the offline-frozen case, tree lock-offline-frozen.
  local want="$ROOT/tests/fixtures/fetch-v1/trees/lock-offline-frozen/v2/chtypes/v1/tags/list"
  local rc=0
  curl -fsS --max-time 5 -A "chtypes-ts/0.0.0-dev" -o "$tmp/got" "http://127.0.0.1:$port/v2/s-offline-frozen/chtypes/v1/tags/list" || rc=$?
  echo "proof 3 (loopback round trip through server.py, inside the sandbox): curl exit code $rc"
  [ "$rc" -eq 0 ] || die "loopback round trip failed inside the sandbox (curl exit $rc)"
  cmp -s "$tmp/got" "$want" || die "the served bytes differ from the fixture file"
  echo "proof 3: the fixture bytes served over loopback match $want"
}

selftest() {
  [ "$(uname -s)" = "Linux" ] || die "--selftest needs Linux (network namespaces)"
  local rc uid out

  rc=0; sandbox true || rc=$?
  echo "proof 1a (positive control, sandbox true): exit code $rc"
  [ "$rc" -eq 0 ] || die "the sandbox wrapper itself failed (exit $rc)"

  uid="$(id -u)"
  rc=0; out="$(sandbox id -u)" || rc=$?
  echo "proof 1b (sandbox id -u): exit code $rc, printed uid '$out', runner uid $uid"
  [ "$rc" -eq 0 ] || die "sandbox id -u failed (exit $rc)"
  [ "$out" = "$uid" ] || die "the sandboxed uid '$out' is not the runner's uid '$uid'"
  [ "$out" != "0" ] || die "the sandboxed process is root"

  # shellcheck disable=SC2016
  rc=0; out="$(SANDBOX_SELFTEST_MARKER=kept sandbox sh -c "echo \"\$SANDBOX_SELFTEST_MARKER|\$PATH|\$HOME\"")" || rc=$?
  [ "$rc" -eq 0 ] || die "environment probe failed (exit $rc)"
  [ "$out" = "kept|$PATH|${HOME:-/}" ] || die "environment did not survive the sandbox: '$out'"
  echo "proof 1c: a marker variable, PATH and HOME survive the sandbox"

  rc=0; sandbox curl -sS --max-time 8 -o /dev/null https://github.com 2>/dev/null || rc=$?
  echo "proof 2 (https://github.com from inside the sandbox): curl exit code $rc (6 = could not resolve, 7 = could not connect)"
  case "$rc" in
    6|7) ;;
    *) die "expected curl exit 6 or 7 from inside the sandbox, got $rc (0 = reachable, anything else = a wrapper or curl failure)" ;;
  esac

  rc=0; sandbox bash "$SELF" --inner-loopback || rc=$?
  echo "proof 3 (wrapper around the loopback proof): exit code $rc"
  [ "$rc" -eq 0 ] || die "the loopback proof failed (exit $rc)"

  echo "sandbox.sh --selftest: OK"
}

case "${1:-}" in
  --selftest)
    selftest
    ;;
  --inner-loopback)
    inner_loopback
    ;;
  --)
    shift
    [ "$#" -gt 0 ] || die "usage: sandbox.sh -- <command> [args...]"
    sandbox "$@"
    ;;
  *) die "usage: sandbox.sh --selftest | sandbox.sh -- <command> [args...]" ;;
esac
