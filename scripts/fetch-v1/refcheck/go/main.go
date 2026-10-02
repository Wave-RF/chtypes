// Command refcheck-go answers one question, report-only, never failing the
// `v1-refcheck` job: does sigstore-go accept this repository's key-only,
// ed25519-signed Sigstore bundle v0.3 (no certificate, no transparency log
// entry)? It is deliberately independent of every binding's own
// hand-rolled verifier (docs/guides/fetch-v1.md §4.3) — the whole point is
// a reference implementation nobody here wrote checking the same bytes.
//
// Measured locally before this file was committed (lane 0B's own
// research): sigstore-go v1.2.2 (the newest published version; the
// delivery-side design doc's "1.3.0" does not exist on the module proxy as
// of 2026-10-01 and is recorded as a discrepancy, not followed — see
// docs/guides/fetch-v1.md §10) verifies a bundle built exactly as
// scripts/fetch-v1/genfixtures/sign.go builds one, given
// `verify.WithNoObserverTimestamps()` (no tlog, no TSA — matches this
// bundle's `verificationMaterial`, which carries nothing else) and
// `verify.WithKey()` (no certificate-identity policy), with the trust
// mapping keyed by the bundle's own `hint` string verbatim.
package main

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	sigstoreSig "github.com/sigstore/sigstore/pkg/signature"

	"github.com/sigstore/sigstore-go/pkg/bundle"
	"github.com/sigstore/sigstore-go/pkg/root"
	"github.com/sigstore/sigstore-go/pkg/verify"
)

// target names one fixture to check: a platform manifest's own signature
// referrer, resolved by tag. A small, representative sample — not every
// fixture (this is a format check, not the full trust cross-check the
// plan's broader vision describes; see this file's header).
type target struct {
	name     string
	tree     string
	tag      string
	platform string // "os-arch"
}

var targets = []target{
	{name: "platform-manifest (line-ok, 26.8.15.10, linux-arm64)", tree: "basic", tag: "26.8.15.10", platform: "linux-arm64"},
	{name: "platform-manifest (line-ok, 26.8.15.10, darwin-arm64)", tree: "basic", tag: "26.8.15.10", platform: "darwin-arm64"},
}

type descriptor struct {
	MediaType    string `json:"mediaType"`
	Digest       string `json:"digest"`
	Size         int64  `json:"size"`
	ArtifactType string `json:"artifactType"`
	Platform     *struct {
		OS           string `json:"os"`
		Architecture string `json:"architecture"`
	} `json:"platform"`
}

type imageIndex struct {
	Manifests []descriptor `json:"manifests"`
}

type imageManifest struct {
	ArtifactType string       `json:"artifactType"`
	Layers       []descriptor `json:"layers"`
}

const bundleArtifactType = "application/vnd.dev.sigstore.bundle.v0.3+json"

func repoRoot() string {
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		panic("refcheck-go: runtime.Caller failed")
	}
	// this file: scripts/fetch-v1/refcheck/go/main.go -> repo root is four up.
	return filepath.Dir(filepath.Dir(filepath.Dir(filepath.Dir(filepath.Dir(file)))))
}

func findSignatureBundle(fixturesDir, tree, subjectDigest string) ([]byte, error) {
	base := filepath.Join(fixturesDir, "trees", tree, "v2", "chtypes", "v1")
	raw, err := os.ReadFile(filepath.Join(base, "referrers", subjectDigest))
	if err != nil {
		return nil, fmt.Errorf("reading referrers for %s: %w", subjectDigest, err)
	}
	var refs imageIndex
	if err := json.Unmarshal(raw, &refs); err != nil {
		return nil, err
	}
	for _, d := range refs.Manifests {
		if d.ArtifactType != bundleArtifactType {
			continue
		}
		manBytes, err := os.ReadFile(filepath.Join(base, "manifests", d.Digest))
		if err != nil {
			return nil, err
		}
		var man imageManifest
		if err := json.Unmarshal(manBytes, &man); err != nil {
			return nil, err
		}
		if len(man.Layers) == 0 {
			continue
		}
		return os.ReadFile(filepath.Join(base, "blobs", man.Layers[0].Digest))
	}
	return nil, fmt.Errorf("no %s referrer found for %s", bundleArtifactType, subjectDigest)
}

func resolvePlatformManifestDigest(fixturesDir, tree, tag, platformKey string) (string, error) {
	base := filepath.Join(fixturesDir, "trees", tree, "v2", "chtypes", "v1")
	raw, err := os.ReadFile(filepath.Join(base, "manifests", tag))
	if err != nil {
		return "", err
	}
	var idx imageIndex
	if err := json.Unmarshal(raw, &idx); err != nil {
		return "", err
	}
	for _, d := range idx.Manifests {
		if d.Platform == nil {
			continue
		}
		if d.Platform.OS+"-"+d.Platform.Architecture == platformKey {
			return d.Digest, nil
		}
	}
	return "", fmt.Errorf("no platform %s in index for tag %s", platformKey, tag)
}

// keyID mirrors go/chtypes/fetch_sign.go's KeyID (the first 16 hex
// characters of sha256 over the raw public key) — see genfixtures'
// sign.go for why that is this bundle format's `hint` value.
func keyID(pub ed25519.PublicKey) string {
	sum := sha256.Sum256(pub)
	return hex.EncodeToString(sum[:8])
}

func loadTestPublicKey(root string) (ed25519.PublicKey, string, error) {
	raw, err := os.ReadFile(filepath.Join(root, "tests", "fixtures", "fetch-v1", "test-key", "public.hex"))
	if err != nil {
		return nil, "", err
	}
	h := strings.TrimSpace(string(raw))
	b, err := hex.DecodeString(h)
	if err != nil {
		return nil, "", err
	}
	pub := ed25519.PublicKey(b)
	return pub, keyID(pub), nil
}

func verifyWithSigstoreGo(bundleJSON []byte, pub ed25519.PublicKey, keyIDHex string) (bool, string) {
	tmp, err := os.CreateTemp("", "refcheck-bundle-*.json")
	if err != nil {
		return false, err.Error()
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(bundleJSON); err != nil {
		return false, err.Error()
	}
	if err := tmp.Close(); err != nil {
		return false, err.Error()
	}

	b, err := bundle.LoadJSONFromPath(tmp.Name())
	if err != nil {
		return false, "loading bundle: " + err.Error()
	}

	verifier, err := sigstoreSig.LoadVerifier(pub, 0)
	if err != nil {
		return false, "loading verifier: " + err.Error()
	}
	ek := root.NewExpiringKey(verifier, time.Time{}, time.Time{})
	tm := root.NewTrustedPublicKeyMaterialFromMapping(map[string]*root.ExpiringKey{keyIDHex: ek})

	sev, err := verify.NewSignedEntityVerifier(tm, verify.WithNoObserverTimestamps())
	if err != nil {
		return false, "configuring verifier: " + err.Error()
	}
	policy := verify.NewPolicy(verify.WithoutArtifactUnsafe(), verify.WithKey())
	if _, err := sev.Verify(b, policy); err != nil {
		return false, err.Error()
	}
	return true, ""
}

func main() {
	root_ := repoRoot()
	fixturesDir := filepath.Join(root_, "tests", "fixtures", "fetch-v1")

	pub, keyIDHex, err := loadTestPublicKey(root_)
	if err != nil {
		fatalf("loading test key: %v", err)
	}

	var results []map[string]any
	anyFail := false
	for _, t := range targets {
		row := map[string]any{"target": t.name}
		manifestDigest, err := resolvePlatformManifestDigest(fixturesDir, t.tree, t.tag, t.platform)
		if err != nil {
			row["error"] = err.Error()
			results = append(results, row)
			anyFail = true
			continue
		}
		bundleJSON, err := findSignatureBundle(fixturesDir, t.tree, manifestDigest)
		if err != nil {
			row["error"] = err.Error()
			results = append(results, row)
			anyFail = true
			continue
		}
		ok, detail := verifyWithSigstoreGo(bundleJSON, pub, keyIDHex)
		row["sigstore_go_accepts_ed25519_key_only_bundle"] = ok
		if detail != "" {
			row["detail"] = detail
		}
		if !ok {
			anyFail = true
		}
		results = append(results, row)
	}

	out, _ := json.MarshalIndent(map[string]any{
		"schema": 1, "tool": "sigstore-go", "report_only": true, "results": results,
	}, "", "  ")
	fmt.Println(string(out))

	// Report-only (docs/guides/fetch-v1.md §10): the v1-refcheck job reads
	// this exit code but does not gate `v1-parity` on it. A nonzero exit
	// here is informational, not a merge blocker.
	if anyFail {
		os.Exit(1)
	}
}

func fatalf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "refcheck-go: "+format+"\n", args...)
	os.Exit(1)
}
