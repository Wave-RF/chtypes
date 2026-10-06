#!/usr/bin/env node
"use strict";

// index.js — answers one question, report-only, never failing the
// `v1-refcheck` job: does @sigstore/verify accept this repository's
// key-only, ed25519-signed Sigstore bundle v0.3 (no certificate, no
// transparency log entry)? Independent of every binding's own hand-rolled
// verifier (docs/guides/fetch-v1.md §4.3) — a reference implementation
// nobody here wrote, checking the same bytes.
//
// Measured locally before this file was committed (lane 0B's own
// research): @sigstore/verify 4.1.2 verifies a bundle built exactly as
// scripts/fetch-v1/genfixtures/sign.go builds one, given a `Verifier`
// constructed with `tlogThreshold`/`ctlogThreshold`/`timestampThreshold`
// all 0 (this bundle carries no tlog entry, no certificate, no
// timestamp — every one of those checks would otherwise refuse it for
// reasons unrelated to the signature itself) and a `TrustMaterial.publicKey`
// hint-finder keyed by the bundle's own `hint` string verbatim (see
// genfixtures' sign.go for why that string is our own keyid, not a raw-key
// encoding).

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { bundleFromJSON } = require("@sigstore/bundle");
const { Verifier, toSignedEntity } = require("@sigstore/verify");

const REPO_ROOT = path.resolve(__dirname, "..", "..", "..", "..");
const FIXTURES_DIR = path.join(REPO_ROOT, "tests", "fixtures", "fetch-v1");

// A small, representative sample — not every fixture (this is a format
// check, not the full trust cross-check the plan's broader vision
// describes; see this file's header).
const TARGETS = [
  { name: "platform-manifest (line-ok, 26.8.15.10, linux-arm64)", tree: "basic", tag: "26.8.15.10", platform: "linux-arm64" },
  { name: "platform-manifest (line-ok, 26.8.15.10, darwin-arm64)", tree: "basic", tag: "26.8.15.10", platform: "darwin-arm64" },
];

const BUNDLE_ARTIFACT_TYPE = "application/vnd.dev.sigstore.bundle.v0.3+json";

function readJSON(p) {
  return JSON.parse(fs.readFileSync(p, "utf8"));
}

function treeBase(tree) {
  return path.join(FIXTURES_DIR, "trees", tree, "v2", "chtypes", "v1");
}

function resolvePlatformManifestDigest(tree, tag, platformKey) {
  const idx = readJSON(path.join(treeBase(tree), "manifests", tag));
  for (const d of idx.manifests) {
    if (d.platform && `${d.platform.os}-${d.platform.architecture}` === platformKey) {
      return d.digest;
    }
  }
  throw new Error(`no platform ${platformKey} in index for tag ${tag}`);
}

function findSignatureBundle(tree, subjectDigest) {
  const base = treeBase(tree);
  const refs = readJSON(path.join(base, "referrers", subjectDigest));
  for (const d of refs.manifests) {
    if (d.artifactType !== BUNDLE_ARTIFACT_TYPE) continue;
    const man = readJSON(path.join(base, "manifests", d.digest));
    const layer = man.layers[0];
    return readJSON(path.join(base, "blobs", layer.digest));
  }
  throw new Error(`no ${BUNDLE_ARTIFACT_TYPE} referrer found for ${subjectDigest}`);
}

// keyID mirrors go/internal/ocifetch/dsse.go's keyIDFor (the first 16 hex
// characters of sha256 over the raw public key) — see genfixtures' sign.go
// for why that is this bundle format's `hint` value.
function keyID(pubRawBytes) {
  return crypto.createHash("sha256").update(pubRawBytes).digest("hex").slice(0, 16);
}

function loadTestPublicKey() {
  const hex = fs.readFileSync(path.join(FIXTURES_DIR, "test-key", "public.hex"), "utf8").trim();
  const raw = Buffer.from(hex, "hex");
  const spkiPrefix = Buffer.from("302a300506032b6570032100", "hex"); // ed25519 SPKI header, fixed for a 32-byte key
  const spki = Buffer.concat([spkiPrefix, raw]);
  const keyObject = crypto.createPublicKey({ key: spki, format: "der", type: "spki" });
  return { keyObject, hint: keyID(raw) };
}

function verifyWithSigstoreJS(bundleJSON, keyObject, hint) {
  const bundle = bundleFromJSON(bundleJSON);
  const entity = toSignedEntity(bundle);

  const trustMaterial = {
    certificateAuthorities: [],
    timestampAuthorities: [],
    tlogs: [],
    ctlogs: [],
    publicKey: () => ({ publicKey: keyObject, validFor: () => true }),
  };
  const verifier = new Verifier(trustMaterial, { tlogThreshold: 0, ctlogThreshold: 0, timestampThreshold: 0 });
  try {
    verifier.verify(entity);
    return { ok: true };
  } catch (e) {
    return { ok: false, detail: String(e && e.message ? e.message : e) };
  }
}

function main() {
  const { keyObject, hint } = loadTestPublicKey();
  const results = [];
  let anyFail = false;

  for (const t of TARGETS) {
    const row = { target: t.name };
    try {
      const manifestDigest = resolvePlatformManifestDigest(t.tree, t.tag, t.platform);
      const bundleJSON = findSignatureBundle(t.tree, manifestDigest);
      const v = verifyWithSigstoreJS(bundleJSON, keyObject, hint);
      row.sigstore_js_accepts_ed25519_key_only_bundle = v.ok;
      if (!v.ok) {
        row.detail = v.detail;
        anyFail = true;
      }
    } catch (e) {
      row.error = String(e && e.message ? e.message : e);
      anyFail = true;
    }
    results.push(row);
  }

  console.log(JSON.stringify({ schema: 1, tool: "@sigstore/verify", report_only: true, results }, null, 2));

  // Report-only (docs/guides/fetch-v1.md §10): v1-refcheck reads this exit
  // code but does not gate v1-parity on it.
  process.exit(anyFail ? 1 : 0);
}

main();
