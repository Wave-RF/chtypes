package ocifetch

import (
	"crypto/ed25519"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"os"
	"testing"
)

// testKeyFixture is this repository's own fixture test key
// (tests/fixtures/fetch-v1/test-key/, written by lane 0A). It is test-only
// and never in the default trust list — see that directory's README.md.
const testKeyFixtureDir = "../../../tests/fixtures/fetch-v1/test-key"

func loadTestKey(t *testing.T) (ed25519.PrivateKey, ed25519.PublicKey) {
	t.Helper()
	pemBytes, err := os.ReadFile(testKeyFixtureDir + "/private.pem")
	if err != nil {
		t.Skipf("test-key fixture not available: %v", err)
	}
	block, _ := pem.Decode(pemBytes)
	if block == nil {
		t.Fatalf("test-key private.pem has no PEM block")
	}
	key, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		t.Fatalf("parsing test-key PKCS#8: %v", err)
	}
	priv, ok := key.(ed25519.PrivateKey)
	if !ok {
		t.Fatalf("test-key is not ed25519: %T", key)
	}
	hexBytes, err := os.ReadFile(testKeyFixtureDir + "/public.hex")
	if err != nil {
		t.Fatalf("reading public.hex: %v", err)
	}
	pubHex := string(trimNewline(hexBytes))
	pubBytes, err := hex.DecodeString(pubHex)
	if err != nil {
		t.Fatalf("public.hex is not hex: %v", err)
	}
	pub := priv.Public().(ed25519.PublicKey)
	if hex.EncodeToString(pub) != hex.EncodeToString(pubBytes) {
		t.Fatalf("private.pem's public half does not match public.hex")
	}
	return priv, pub
}

func trimNewline(b []byte) []byte {
	for len(b) > 0 && (b[len(b)-1] == '\n' || b[len(b)-1] == '\r' || b[len(b)-1] == ' ') {
		b = b[:len(b)-1]
	}
	return b
}

func TestPAEKnownVector(t *testing.T) {
	// DSSE's PAE, applied to a fixed, hand-countable type and body:
	// len("http://example.com/HelloWorld")==29, len("hello world")==11.
	got := pae("http://example.com/HelloWorld", []byte("hello world"))
	want := "DSSEv1 29 http://example.com/HelloWorld 11 hello world"
	if string(got) != want {
		t.Fatalf("pae() = %q, want %q", got, want)
	}
}

// signBundle builds a minimal but complete Sigstore bundle, DSSE-signing
// statement with priv, for tests to feed to verifyBundle.
func signBundle(t *testing.T, priv ed25519.PrivateKey, statement any) []byte {
	t.Helper()
	payload, err := json.Marshal(statement)
	if err != nil {
		t.Fatalf("marshal statement: %v", err)
	}
	sig := ed25519.Sign(priv, pae(DSSEPayloadType, payload))
	bundle := map[string]any{
		"mediaType": MediaTypeBundle,
		"verificationMaterial": map[string]any{
			"publicKey": map[string]any{"hint": "unrelated-untrusted-hint"},
		},
		"dsseEnvelope": map[string]any{
			"payload":     base64.StdEncoding.EncodeToString(payload),
			"payloadType": DSSEPayloadType,
			"signatures": []map[string]any{
				{"sig": base64.StdEncoding.EncodeToString(sig)},
			},
		},
	}
	b, err := json.Marshal(bundle)
	if err != nil {
		t.Fatalf("marshal bundle: %v", err)
	}
	return b
}

func sampleArtifactStatement(layerDigestHex string) map[string]any {
	return map[string]any{
		"_type": StatementType,
		"subject": []map[string]any{
			{"name": "chtypes-26.8.15.10-linux-arm64.tar.zst", "digest": map[string]string{"sha256": layerDigestHex}},
		},
		"predicateType": PredicateTypeArtifact,
		"predicate": map[string]any{
			// JSON numbers decode to float64, which is the shape every real
			// predicate arrives in through strictUnmarshal; these are
			// written as float64 literals here too, rather than through an
			// actual JSON round trip, so this fixture matches that shape.
			"abi":                1.0,
			"clickhouse_version": "26.8.15.10",
			"channel":            "lts",
			"os":                 "linux",
			"arch":               "arm64",
			"build":              "20261001.183455",
			"library":            "libchtypes.so",
			"library_sha256":     hex64('d'),
			"library_bytes":      241000000.0,
		},
	}
}

func TestVerifyBundleAcceptsTrustedKey(t *testing.T) {
	priv, pub := loadTestKey(t)
	layerDigest := Digest("sha256:" + hex64('a'))
	bundle := signBundle(t, priv, sampleArtifactStatement(layerDigest.Hex()))

	vr, err := verifyBundle(bundle, []ed25519.PublicKey{pub})
	if err != nil {
		t.Fatalf("verifyBundle: %v", err)
	}
	if !vr.verified {
		t.Fatalf("verifyBundle did not verify a correctly signed bundle under its own key")
	}
	if vr.keyID != keyIDFor(pub) {
		t.Fatalf("verifyBundle keyID = %q, want %q", vr.keyID, keyIDFor(pub))
	}

	platform := Platform{Key: "linux-arm64", OS: "linux", Architecture: "arm64"}
	if err := validateStatement(vr.statement, PredicateTypeArtifact, layerDigest, &platform, "26.8"); err != nil {
		t.Fatalf("validateStatement on a matching statement: %v", err)
	}
}

func TestVerifyBundleRejectsUntrustedKey(t *testing.T) {
	priv, _ := loadTestKey(t)
	// A different key than the one in the trust list: the "other-key"
	// fixture plays this role in the real conformance suite; here any
	// freshly generated key does the same job.
	_, otherPub, err := ed25519GenerateForTest(t)
	if err != nil {
		t.Fatalf("generating a key: %v", err)
	}
	bundle := signBundle(t, priv, sampleArtifactStatement(hex64('a')))
	vr, err := verifyBundle(bundle, []ed25519.PublicKey{otherPub})
	if err != nil {
		t.Fatalf("verifyBundle returned an error for a bundle that simply does not verify: %v", err)
	}
	if vr.verified {
		t.Fatalf("verifyBundle verified a bundle under a key that did not sign it")
	}
}

func TestVerifyBundleIgnoresHint(t *testing.T) {
	// The bundle's own publicKey.hint names an unrelated, untrusted id
	// (signBundle always sets one); verification must succeed anyway,
	// because the hint is never used to select which key to try.
	priv, pub := loadTestKey(t)
	bundle := signBundle(t, priv, sampleArtifactStatement(hex64('a')))
	vr, err := verifyBundle(bundle, []ed25519.PublicKey{pub})
	if err != nil || !vr.verified {
		t.Fatalf("verifyBundle should ignore the bundle's hint and still verify: ok=%v err=%v", vr.verified, err)
	}
}

func TestValidateStatementRejectsWrongPredicateType(t *testing.T) {
	priv, pub := loadTestKey(t)
	layerDigest := Digest("sha256:" + hex64('a'))
	bundle := signBundle(t, priv, sampleArtifactStatement(layerDigest.Hex()))
	vr, _ := verifyBundle(bundle, []ed25519.PublicKey{pub})
	if err := validateStatement(vr.statement, PredicateTypeGoldens, layerDigest, nil, ""); err == nil {
		t.Fatalf("validateStatement accepted the wrong predicateType")
	}
}

func TestValidateStatementRejectsSubjectMismatch(t *testing.T) {
	priv, pub := loadTestKey(t)
	bundle := signBundle(t, priv, sampleArtifactStatement(hex64('a')))
	vr, _ := verifyBundle(bundle, []ed25519.PublicKey{pub})
	wrongLayer := Digest("sha256:" + hex64('b'))
	platform := Platform{Key: "linux-arm64", OS: "linux", Architecture: "arm64"}
	if err := validateStatement(vr.statement, PredicateTypeArtifact, wrongLayer, &platform, "26.8"); err == nil {
		t.Fatalf("validateStatement accepted a subject digest that does not match the layer")
	}
}

func TestValidateArtifactPredicateChecksABIOSArchVersion(t *testing.T) {
	platform := Platform{Key: "linux-arm64", OS: "linux", Architecture: "arm64"}
	good := sampleArtifactStatement(hex64('a'))["predicate"].(map[string]any)

	if err := validateArtifactPredicate(good, platform, "26.8"); err != nil {
		t.Fatalf("validateArtifactPredicate on a matching predicate: %v", err)
	}
	if err := validateArtifactPredicate(good, platform, "26.7"); err == nil {
		t.Fatalf("validateArtifactPredicate accepted a version outside the request")
	}
	if err := validateArtifactPredicate(good, Platform{Key: "darwin-arm64", OS: "darwin", Architecture: "arm64"}, "26.8"); err == nil {
		t.Fatalf("validateArtifactPredicate accepted a mismatched platform")
	}

	noAbi := map[string]any{"os": "linux", "arch": "arm64", "clickhouse_version": "26.8.15.10"}
	if err := validateArtifactPredicate(noAbi, platform, "26.8"); err == nil {
		t.Fatalf("validateArtifactPredicate accepted a predicate with no \"abi\" field")
	}

	abiRevisionOnly := map[string]any{"abi_revision": 1.0, "os": "linux", "arch": "arm64", "clickhouse_version": "26.8.15.10"}
	if err := validateArtifactPredicate(abiRevisionOnly, platform, "26.8"); err == nil {
		t.Fatalf(`validateArtifactPredicate must not read "abi_revision" as "abi"`)
	}
}

func ed25519GenerateForTest(t *testing.T) (ed25519.PrivateKey, ed25519.PublicKey, error) {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(nil)
	return priv, pub, err
}
