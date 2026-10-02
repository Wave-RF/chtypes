#!/usr/bin/env bash
# scripts/fetch-v1/refcheck/cosign-check.sh — answers one question,
# report-only, never failing the v1-refcheck job: does
# `cosign verify-blob-attestation` accept this repository's key-only,
# ed25519-signed Sigstore bundle v0.3 (no certificate, no transparency log
# entry)? docs/guides/fetch-v1.md §10, plan §4.3.
#
# Measured locally before this script was committed (lane 0B's own
# research, cosign 3.1.3 — the version the delivery side's design doc also
# measured with): a bundle built exactly as
# scripts/fetch-v1/genfixtures/sign.go builds one verifies with
#   cosign verify-blob-attestation --bundle <file> --key <pubkey.pem> \
#     --insecure-ignore-tlog --digest <layer digest hex> --digestAlg sha256 \
#     --type <the exact predicateType string>
# `--insecure-ignore-tlog` is required (this bundle carries no tlog entry
# by design — key-based, no public log, layout-v2 spec §4.2); `--type` must
# be the literal predicateType URI, not `custom` (cosign's own default
# value for that flag, which it then compares literally against the
# statement's predicateType and refuses on a mismatch).
#
# Usage: scripts/fetch-v1/refcheck/cosign-check.sh
# Requires: cosign, jq, openssl, python3 (stdlib json) on PATH.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
FIXTURES="$ROOT/tests/fixtures/fetch-v1"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PREDICATE_TYPE_ARTIFACT="$(python3 -c "import json;print(json.load(open('$ROOT/spec/fetch-v1/constants.json'))['predicate_types']['artifact'])")"

openssl pkey -in "$FIXTURES/test-key/private.pem" -pubout -out "$WORK/test-key-pub.pem"

any_fail=0
results="[]"

check_target() {
  local label="$1" tree="$2" tag="$3" platform="$4"
  local base="$FIXTURES/trees/$tree/v2/chtypes/v1"

  local manifest_digest
  manifest_digest="$(jq -r --arg plat "$platform" '
    .manifests[] | select((.platform.os + "-" + .platform.architecture) == $plat) | .digest
  ' "$base/manifests/$tag")"
  if [ -z "$manifest_digest" ]; then
    echo "cosign-check: no platform $platform in index for tag $tag" >&2
    any_fail=1
    results="$(echo "$results" | jq --arg label "$label" '. + [{"target":$label,"error":"platform not found"}]')"
    return
  fi

  local bundle_manifest_digest layer_digest
  bundle_manifest_digest="$(jq -r '.manifests[] | select(.artifactType == "application/vnd.dev.sigstore.bundle.v0.3+json") | .digest' "$base/referrers/$manifest_digest" | head -1)"
  layer_digest="$(jq -r '.layers[0].digest' "$base/manifests/$bundle_manifest_digest")"
  cp "$base/blobs/$layer_digest" "$WORK/bundle.json"

  local subject_digest_hex
  subject_digest_hex="$(jq -r '.layers[0].digest' "$base/manifests/$manifest_digest" | sed 's/^sha256://')"

  if cosign verify-blob-attestation \
    --bundle "$WORK/bundle.json" \
    --key "$WORK/test-key-pub.pem" \
    --insecure-ignore-tlog \
    --digest "$subject_digest_hex" \
    --digestAlg sha256 \
    --type "$PREDICATE_TYPE_ARTIFACT" >"$WORK/cosign-out.txt" 2>&1; then
    results="$(echo "$results" | jq --arg label "$label" '. + [{"target":$label,"cosign_accepts_ed25519_key_only_bundle":true}]')"
  else
    any_fail=1
    local detail
    detail="$(tail -1 "$WORK/cosign-out.txt")"
    results="$(echo "$results" | jq --arg label "$label" --arg detail "$detail" '. + [{"target":$label,"cosign_accepts_ed25519_key_only_bundle":false,"detail":$detail}]')"
  fi
}

# The same small, representative sample refcheck/go and refcheck/js use.
check_target "platform-manifest (line-ok, 26.8.15.10, linux-arm64)" basic 26.8.15.10 linux-arm64
check_target "platform-manifest (line-ok, 26.8.15.10, darwin-arm64)" basic 26.8.15.10 darwin-arm64

jq -n --argjson results "$results" '{schema: 1, tool: "cosign", report_only: true, results: $results}'

# Report-only (docs/guides/fetch-v1.md §10): v1-refcheck reads this exit
# code but does not gate v1-parity on it.
exit "$any_fail"
