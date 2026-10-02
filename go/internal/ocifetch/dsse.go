package ocifetch

// dsse.go — trust (docs/guides/fetch-v1.md §4): the Sigstore bundle v0.3
// shape, DSSE's pre-authentication encoding, ed25519 verification against
// the trusted key list using crypto/ed25519 (never a new Sigstore client
// library — a key-only bundle is outside what sigstore-go's Go counterpart
// would need here, and this package has no such dependency), and the
// in-toto Statement v1 checks against the request.

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"strconv"
)

// sigstoreBundle is the subset of a Sigstore bundle v0.3
// (application/vnd.dev.sigstore.bundle.v0.3+json) this package reads: a
// DSSE envelope, key-based (no certificate, no transparency log entry).
type sigstoreBundle struct {
	MediaType            string `json:"mediaType"`
	VerificationMaterial struct {
		PublicKey *struct {
			Hint string `json:"hint"`
		} `json:"publicKey"`
	} `json:"verificationMaterial"`
	DSSEEnvelope struct {
		Payload     string `json:"payload"`
		PayloadType string `json:"payloadType"`
		Signatures  []struct {
			Sig   string `json:"sig"`
			KeyID string `json:"keyid,omitempty"`
		} `json:"signatures"`
	} `json:"dsseEnvelope"`
}

// Statement is the in-toto Statement v1 payload every bundle's DSSE envelope
// carries. Predicate is kept as a generic map so its fields pass through to
// Resolved.Predicate verbatim (docs/guides/fetch-v1.md §9: "abi_fingerprint,
// library_sha256, library_bytes and glibc_floor are carried through … opaque").
type Statement struct {
	Type          string             `json:"_type"`
	Subject       []StatementSubject `json:"subject"`
	PredicateType string             `json:"predicateType"`
	Predicate     map[string]any     `json:"predicate"`
}

// StatementSubject is one in-toto subject entry.
type StatementSubject struct {
	Name   string            `json:"name"`
	Digest map[string]string `json:"digest"`
}

// pae computes DSSE's pre-authentication encoding over (payloadType, payload):
// "DSSEv1" SP LEN(payloadType) SP payloadType SP LEN(payload) SP payload.
func pae(payloadType string, payload []byte) []byte {
	var buf bytes.Buffer
	buf.WriteString("DSSEv1")
	buf.WriteByte(' ')
	buf.WriteString(strconv.Itoa(len(payloadType)))
	buf.WriteByte(' ')
	buf.WriteString(payloadType)
	buf.WriteByte(' ')
	buf.WriteString(strconv.Itoa(len(payload)))
	buf.WriteByte(' ')
	buf.Write(payload)
	return buf.Bytes()
}

// keyIDFor computes a public key's key id: the first 16 hex characters of
// sha256 over the raw 32-byte key (constants_gen.go's KeyIDAlgorithm,
// "sha256-first16hex" — the same derivation v0 uses, copied independently
// per binding rather than shared, per the v1 fetch-layer plan).
func keyIDFor(pub ed25519.PublicKey) string {
	sum := sha256.Sum256(pub)
	return hex.EncodeToString(sum[:8])
}

// bundleVerifyResult is the outcome of checking one candidate bundle's
// cryptographic signature, before any statement-content check.
type bundleVerifyResult struct {
	verified  bool
	keyID     string
	statement *Statement
}

// verifyBundle parses body as a Sigstore bundle and tries every trusted key
// against every signature in its DSSE envelope (never picking a key from the
// bundle's own `publicKey.hint`, which is untrusted by name — the same rule
// v0's SHA256SUMS.sig comment line follows). A bundle that is malformed, or
// whose payloadType is wrong, or whose signature count exceeds
// DSSEMaxSignatures, simply does not verify (verified=false, err=nil) so the
// caller can try another referrer; an error is returned only once a
// signature DID verify but the payload underneath it fails to parse, which
// is CHTYPES_ARTIFACT_CORRUPT rather than CHTYPES_ARTIFACT_UNTRUSTED,
// because a trusted key signed it.
func verifyBundle(body []byte, trustedKeys []ed25519.PublicKey) (bundleVerifyResult, error) {
	var bundle sigstoreBundle
	if err := strictUnmarshal(body, &bundle); err != nil {
		return bundleVerifyResult{}, nil
	}
	if bundle.DSSEEnvelope.PayloadType != DSSEPayloadType {
		return bundleVerifyResult{}, nil
	}
	sigs := bundle.DSSEEnvelope.Signatures
	if len(sigs) == 0 || len(sigs) > DSSEMaxSignatures {
		return bundleVerifyResult{}, nil
	}
	payload, err := base64.StdEncoding.DecodeString(bundle.DSSEEnvelope.Payload)
	if err != nil {
		return bundleVerifyResult{}, nil
	}
	message := pae(bundle.DSSEEnvelope.PayloadType, payload)

	var matched ed25519.PublicKey
	for _, sig := range sigs {
		raw, err := base64.StdEncoding.DecodeString(sig.Sig)
		if err != nil || len(raw) != ed25519.SignatureSize {
			continue
		}
		for _, k := range trustedKeys {
			if ed25519.Verify(k, message, raw) {
				matched = k
				break
			}
		}
		if matched != nil {
			break
		}
	}
	if matched == nil {
		return bundleVerifyResult{}, nil
	}

	var stmt Statement
	if err := strictUnmarshal(payload, &stmt); err != nil {
		return bundleVerifyResult{}, fmt.Errorf("signed statement payload is not valid JSON: %w", err)
	}
	return bundleVerifyResult{verified: true, keyID: keyIDFor(matched), statement: &stmt}, nil
}

// validateStatement checks a verified statement against the request
// (docs/guides/fetch-v1.md §4): the statement type, the predicate type, the
// subject digest against the signed object's own layer digest, and — only
// when platform is non-nil (the platform-artifact case; goldens and
// fixtures carry no os/arch/version to check) — the predicate's abi, os,
// arch and clickhouse_version against platform and request.
func validateStatement(stmt *Statement, expectedPredicateType string, layerDigest Digest, platform *Platform, request string) error {
	if stmt.Type != StatementType {
		return fmt.Errorf("statement _type %q, expected %q", stmt.Type, StatementType)
	}
	if stmt.PredicateType != expectedPredicateType {
		return fmt.Errorf("predicateType %q, expected %q", stmt.PredicateType, expectedPredicateType)
	}
	if !subjectMatchesLayer(stmt.Subject, layerDigest) {
		return fmt.Errorf("no subject digest matches the layer %s", layerDigest)
	}
	if platform != nil {
		return validateArtifactPredicate(stmt.Predicate, *platform, request)
	}
	return nil
}

func subjectMatchesLayer(subjects []StatementSubject, layerDigest Digest) bool {
	want := layerDigest.Hex()
	if want == "" {
		return false
	}
	for _, s := range subjects {
		if s.Digest["sha256"] == want {
			return true
		}
	}
	return false
}

// validateArtifactPredicate checks the platform-artifact predicate fields
// (docs/guides/fetch-v1.md §4): abi equals ABIGeneration (never a stray
// "abi_revision" — v0's key name, which must not be read as this one), os
// and arch equal platform, and clickhouse_version lies within request.
func validateArtifactPredicate(pred map[string]any, platform Platform, request string) error {
	abiVal, ok := pred["abi"]
	if !ok {
		return fmt.Errorf(`predicate carries no "abi" field`)
	}
	abiNum, ok := asInt(abiVal)
	if !ok || abiNum != ABIGeneration {
		return fmt.Errorf("predicate abi %v, expected %d", abiVal, ABIGeneration)
	}
	osVal, _ := pred["os"].(string)
	if osVal != platform.OS {
		return fmt.Errorf("predicate os %q, expected %q", osVal, platform.OS)
	}
	archVal, _ := pred["arch"].(string)
	if archVal != platform.Architecture {
		return fmt.Errorf("predicate arch %q, expected %q", archVal, platform.Architecture)
	}
	version, _ := pred["clickhouse_version"].(string)
	if version == "" {
		return fmt.Errorf("predicate carries no clickhouse_version")
	}
	// The version-within-request check (§4/§9: "equal an exact four-part
	// request, or lie within a floating one") is a comparison this package
	// performs on the two numeric spellings spelling.regex defines. A
	// request that is not itself one of those shapes carries no
	// version-within obligation to check it against — the conformance
	// suite's own trust-group cases resolve by an opaque per-scenario tag
	// (e.g. "t-wrong-platform"), never a real version spelling, precisely
	// so each scenario's referrer set cannot collide with another's; their
	// predicates carry real, independent versions that a tag-literal
	// comparison was never meant to constrain.
	if spellingRegex.MatchString(request) && !versionWithin(request, version) {
		return fmt.Errorf("predicate clickhouse_version %q does not satisfy the request %q", version, request)
	}
	return nil
}

// asInt reports whether v (decoded from JSON into an `any`, so a number is a
// float64) is a whole number, and its int value.
func asInt(v any) (int, bool) {
	f, ok := v.(float64)
	if !ok {
		return 0, false
	}
	i := int(f)
	return i, float64(i) == f
}
