package main

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/pem"
	"fmt"
	"os"
	"strconv"
	"strings"
)

// sign.go — the in-toto Statement, the DSSE pre-authentication encoding
// (PAE), and the Sigstore bundle v0.3 shape this generator signs fixtures
// with. Measured against real reference verifiers before this file was
// written (lane 0B's own research, not a fixture): a bundle built exactly
// this way is accepted by cosign 3.1.3 (`verify-blob-attestation --key
// ... --insecure-ignore-tlog --digest <hex> --digestAlg sha256 --type
// <predicateType>`), by sigstore-go v1.2.2
// (`verify.NewSignedEntityVerifier(tm, verify.WithNoObserverTimestamps())`
// plus `verify.NewPolicy(verify.WithoutArtifactUnsafe(), verify.WithKey())`,
// with the trust mapping keyed by the bundle's own `hint` string), and by
// @sigstore/verify 4.1.2 (`toSignedEntity` plus a `Verifier` constructed
// with `tlogThreshold`/`ctlogThreshold`/`timestampThreshold` all 0 and a
// `publicKey` hint-finder). See scripts/fetch-v1/refcheck/.

const dssePayloadType = "application/vnd.in-toto+json"                  // dsse.payload_type, constants.json
const statementType = "https://in-toto.io/Statement/v1"                 // statement_type, constants.json
const bundleMediaType = "application/vnd.dev.sigstore.bundle.v0.3+json" // media_types.bundle

// Decided here, lane 0B: the bundle's `verificationMaterial.publicKey.hint`
// carries our OWN keyid (the sha256-first16hex scheme every binding already
// computes for v0, spec/fetch-v1/constants.json's `trust.keyid_algorithm`),
// not a raw-key encoding of any kind. Nothing in layout-v2 spec §4.2 or
// §0's media-type table fixes the hint's content beyond "a public-key
// hint" (no certificate, no log) — a format every one of this repository's
// four hand-rolled verifiers is free to choose, since none of them selects
// a key BY the hint (docs/guides/fetch-v1.md §4: "always filters referrers
// ... itself"; the hint is informational only, exactly as a key comment is
// today). Keying by our own keyid also means a fixture's bad-hint cases
// read as plainly as a hex key id should.

// KeyID is go/internal/ocifetch/dsse.go's keyIDFor: the first 16 hex characters of
// sha256 over the raw 32-byte ed25519 public key. Kept in sync by hand
// (never generated) because this one algorithm is a single line, pinned in
// spec/fetch-v1/constants.json's trust.keyid_algorithm, and copied here only
// so the generator needs no dependency on the bindings it fixtures.
func KeyID(pub ed25519.PublicKey) string {
	sum := sha256.Sum256(pub)
	return hex.EncodeToString(sum[:8])
}

// SigningKey is one loaded ed25519 key pair plus its derived key id.
type SigningKey struct {
	KeyID   string
	Private ed25519.PrivateKey
	Public  ed25519.PublicKey
}

func loadSigningKey(pemPath string) SigningKey {
	raw, err := os.ReadFile(pemPath)
	if err != nil {
		panic(fmt.Sprintf("genfixtures: reading %s: %v", pemPath, err))
	}
	blk, _ := pem.Decode(raw)
	if blk == nil {
		panic(fmt.Sprintf("genfixtures: %s has no PEM block", pemPath))
	}
	k, err := x509.ParsePKCS8PrivateKey(blk.Bytes)
	if err != nil {
		panic(fmt.Sprintf("genfixtures: %s: %v", pemPath, err))
	}
	priv, ok := k.(ed25519.PrivateKey)
	if !ok {
		panic(fmt.Sprintf("genfixtures: %s is not an ed25519 key", pemPath))
	}
	pub := priv.Public().(ed25519.PublicKey)
	return SigningKey{KeyID: KeyID(pub), Private: priv, Public: pub}
}

// pae is the DSSE pre-authentication encoding:
// "DSSEv1" SP LEN(type) SP type SP LEN(body) SP body.
func pae(payloadType string, payload []byte) []byte {
	var buf bytes.Buffer
	buf.WriteString("DSSEv1 ")
	buf.WriteString(strconv.Itoa(len(payloadType)))
	buf.WriteByte(' ')
	buf.WriteString(payloadType)
	buf.WriteByte(' ')
	buf.WriteString(strconv.Itoa(len(payload)))
	buf.WriteByte(' ')
	buf.Write(payload)
	return buf.Bytes()
}

// Subject is one in-toto Statement subject entry.
type Subject struct {
	Name   string            `json:"name"`
	Digest map[string]string `json:"digest"`
}

// Statement is the in-toto Statement v1 envelope payload. Predicate is
// typed `any` so both a well-formed Predicate and a deliberately malformed
// map[string]any (predicate-abi-revision-not-abi, predicate-type-wrong)
// marshal through the same path.
type Statement struct {
	Type          string    `json:"_type"`
	Subject       []Subject `json:"subject"`
	PredicateType string    `json:"predicateType"`
	Predicate     any       `json:"predicate"`
}

func buildStatement(subjectName, subjectDigestHex, predicateType string, predicate any) []byte {
	st := Statement{
		Type: statementType,
		Subject: []Subject{
			{Name: subjectName, Digest: map[string]string{"sha256": subjectDigestHex}},
		},
		PredicateType: predicateType,
		Predicate:     predicate,
	}
	// Statements are not indented: the exact bytes become the DSSE payload,
	// and indentation would only inflate fixtures for no benefit (nothing
	// reads a statement.json file directly off disk the way a manifest or
	// index is read by a route tree).
	b, err := marshalCompact(st)
	if err != nil {
		panic(err)
	}
	return b
}

// injectDuplicateTopLevelKey returns statementJSON with one extra
// "_type": "<junk>" member spliced in right after the opening brace, so the
// byte stream — not any Go value — carries a genuine duplicate key. This
// is statement-duplicate-key's whole fixture: every binding's JSON parser
// must refuse a duplicate key rather than silently keeping the last value.
func injectDuplicateTopLevelKey(statementJSON []byte) []byte {
	s := string(statementJSON)
	idx := strings.IndexByte(s, '{')
	if idx < 0 {
		panic("genfixtures: statement JSON has no opening brace")
	}
	return []byte(s[:idx+1] + `"_type":"https://in-toto.io/Statement/v0-duplicate-key-probe",` + s[idx+1:])
}

type dsseSignature struct {
	Sig string `json:"sig"`
}

type dsseEnvelope struct {
	Payload     string          `json:"payload"`
	PayloadType string          `json:"payloadType"`
	Signatures  []dsseSignature `json:"signatures"`
}

type publicKeyHint struct {
	Hint string `json:"hint"`
}

type verificationMaterial struct {
	PublicKey publicKeyHint `json:"publicKey"`
}

// SigstoreBundle is the v0.3 bundle this generator emits: DSSE plus a
// key-only publicKey hint, no certificate, no transparency log entry —
// exactly layout-v2 spec §4.2's "the bundle carries only a public-key
// hint".
type SigstoreBundle struct {
	MediaType            string               `json:"mediaType"`
	VerificationMaterial verificationMaterial `json:"verificationMaterial"`
	DsseEnvelope         dsseEnvelope         `json:"dsseEnvelope"`
}

// signBundle signs statementJSON with priv and hints hint. Used directly
// only by the trust-fixture cases that need an off-model hint or signer;
// every normal case goes through buildSignedArtifact/buildSignedReferrer
// below.
func signBundle(hint string, priv ed25519.PrivateKey, statementJSON []byte) []byte {
	sig := ed25519.Sign(priv, pae(dssePayloadType, statementJSON))
	b := SigstoreBundle{
		MediaType: bundleMediaType,
		VerificationMaterial: verificationMaterial{
			PublicKey: publicKeyHint{Hint: hint},
		},
		DsseEnvelope: dsseEnvelope{
			Payload:     base64.StdEncoding.EncodeToString(statementJSON),
			PayloadType: dssePayloadType,
			Signatures:  []dsseSignature{{Sig: base64.StdEncoding.EncodeToString(sig)}},
		},
	}
	return canonicalJSON(b)
}

// signBundleCorruptSignature signs normally, then flips one bit of the
// signature — a bundle whose hint names a real, trusted key but whose
// signature does not verify under it (bad-signature: UNTRUSTED, same
// bucket as a signature from nobody we trust, because "signed, but the
// bytes do not check out" and "not signed by anyone we trust" are the same
// failure from a verifier's point of view).
func signBundleCorruptSignature(hint string, priv ed25519.PrivateKey, statementJSON []byte) []byte {
	sig := ed25519.Sign(priv, pae(dssePayloadType, statementJSON))
	corrupt := append([]byte(nil), sig...)
	corrupt[0] ^= 0xFF
	b := SigstoreBundle{
		MediaType: bundleMediaType,
		VerificationMaterial: verificationMaterial{
			PublicKey: publicKeyHint{Hint: hint},
		},
		DsseEnvelope: dsseEnvelope{
			Payload:     base64.StdEncoding.EncodeToString(statementJSON),
			PayloadType: dssePayloadType,
			Signatures:  []dsseSignature{{Sig: base64.StdEncoding.EncodeToString(corrupt)}},
		},
	}
	return canonicalJSON(b)
}
