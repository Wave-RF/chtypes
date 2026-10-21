package chtypes

// registry_revocation_test.go — an open checks the build for a revocation
// before it loads anything (docs/guides/fetch-v1.md, "Build-level revocation",
// check_installed), and the error a program gets carries the revocation.

import (
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/wave-rf/chtypes/go/v2/internal/ocifetch"
)

// TestOpenRefusesAStoredRevocationBeforeLoad installs a build for this host by
// hand, with a stored revocation signed by the fixture test key, and opens it
// with no reachable base: the open must refuse it with
// CHTYPES_ARTIFACT_REVOKED, carrying the revocation, before the loader ever
// reads the (fake) library.
func TestOpenRefusesAStoredRevocationBeforeLoad(t *testing.T) {
	pemBytes, err := os.ReadFile(filepath.Join("..", "..", "tests", "fixtures", "fetch-v1", "test-key", "private.pem"))
	if err != nil {
		t.Skipf("SKIPPED: the fixture test key is not beside this checkout (%v)", err)
	}
	blk, _ := pem.Decode(pemBytes)
	k, err := x509.ParsePKCS8PrivateKey(blk.Bytes)
	if err != nil {
		t.Fatal(err)
	}
	priv := k.(ed25519.PrivateKey)
	pub := priv.Public().(ed25519.PublicKey)

	defer ocifetch.UseProdV2ForTests()()
	ocifetch.ResetRevocationMemoForTests()
	defer ocifetch.ResetRevocationMemoForTests()

	digest := func(s string) string { sum := sha256.Sum256([]byte(s)); return "sha256:" + hex.EncodeToString(sum[:]) }
	manifest, layer := digest("revoked manifest"), digest("revoked layer")
	lib := []byte("not a real library")
	libSum := sha256.Sum256(lib)
	pred := map[string]any{
		"abi": ocifetch.ProdV2ABIGeneration, "abi_fingerprint": digest("fingerprint"), "os": runtime.GOOS, "arch": runtime.GOARCH,
		"clickhouse_version": "26.13.2.1", "build": "20261013.000002", "channel": "lts",
		"library": "libchtypes.so", "library_sha256": hex.EncodeToString(libSum[:]), "library_bytes": len(lib),
	}
	record := map[string]any{
		"schema": ocifetch.ProdV2RecordSchema, "platform": runtime.GOOS + "-" + runtime.GOARCH, "version": "26.13.2.1", "channel": "lts",
		"build": "20261013.000002", "library": "libchtypes.so", "library_sha256": hex.EncodeToString(libSum[:]), "library_bytes": len(lib),
		"digests":   map[string]any{"index": nil, "manifest": manifest, "layer": layer, "bundle": nil, "bundle_manifest": nil},
		"signed_by": nil, "predicate": pred,
	}
	statement := map[string]any{
		"_type": ocifetch.StatementType, "predicateType": ocifetch.PredicateTypeRevocation,
		"subject": []any{map[string]any{"name": "libchtypes.so", "digest": map[string]any{"sha256": layer[len("sha256:"):]}}},
		"predicate": map[string]any{
			"manifest": manifest, "abi": pred["abi"], "abi_fingerprint": pred["abi_fingerprint"], "os": pred["os"], "arch": pred["arch"],
			"clickhouse_version": pred["clickhouse_version"], "build": pred["build"],
			"advisory": map[string]any{"id": "GHSA-fx01-test-0900", "url": "https://github.com/Wave-RF/chtypes/security/advisories/GHSA-fx01-test-0900"},
			"class":    "crash", "summary": "a registry test's revocation", "superseded_by": "20261019.000002", "revoked_at": "2026-10-19T00:00:00Z",
		},
	}
	payload, _ := json.Marshal(statement)
	pae := fmt.Sprintf("DSSEv1 %d %s %d %s", len(ocifetch.DSSEPayloadType), ocifetch.DSSEPayloadType, len(payload), payload)
	bundle, _ := json.Marshal(map[string]any{
		"mediaType":            ocifetch.MediaTypeBundle,
		"verificationMaterial": map[string]any{"publicKey": map[string]any{"hint": "test"}},
		"dsseEnvelope": map[string]any{"payload": base64.StdEncoding.EncodeToString(payload), "payloadType": ocifetch.DSSEPayloadType,
			"signatures": []any{map[string]any{"sig": base64.StdEncoding.EncodeToString(ed25519.Sign(priv, []byte(pae)))}}},
	})

	cache := t.TempDir()
	entry := filepath.Join(cache, ocifetch.ProdV2CacheDir, ocifetch.UnpackedDirName(), manifest[len("sha256:"):])
	if err := os.MkdirAll(entry, 0o755); err != nil {
		t.Fatal(err)
	}
	recordBytes, _ := json.Marshal(record)
	for name, b := range map[string][]byte{"libchtypes.so": lib, ocifetch.CacheVerifiedRecord: recordBytes, ocifetch.CacheRevocationRecord: bundle} {
		if err := os.WriteFile(filepath.Join(entry, name), b, 0o644); err != nil {
			t.Fatal(err)
		}
	}

	// No reachable base: an open that reached the network, or the loader, would say so.
	reg, err := NewRegistry(WithFetchOptions(FetchOptions{CacheDir: cache, SystemDirs: []string{}, TrustedKeys: []string{hex.EncodeToString(pub)},
		Bases: []string{"http://127.0.0.1:1/chtypes/v2"}}), WithAutoFetch(false))
	if err != nil {
		t.Fatal(err)
	}
	_, err = reg.For("26.13.2.1")
	var ae *ArtifactError
	if !errors.As(err, &ae) || ae.Code != CodeArtifactRevoked || !errors.Is(err, ErrArtifactRevoked) {
		t.Fatalf("open = %v; want CHTYPES_ARTIFACT_REVOKED", err)
	}
	if ae.Revocation == nil || !ae.Revocation.Signed || len(ae.Revocation.Advisories) != 1 ||
		ae.Revocation.Advisories[0].ID != "GHSA-fx01-test-0900" || ae.Revocation.Advisories[0].RevokedAt != "2026-10-19T00:00:00Z" ||
		ae.Revocation.Advisories[0].SupersededBy != "20261019.000002" || ae.Revocation.Advisories[0].Class != "crash" {
		t.Fatalf("the error carries %+v; want the signed revocation's one advisory", ae.Revocation)
	}
}
