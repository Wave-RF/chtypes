package chtypes

// devfingerprint_test.go — rule r6's fingerprint refusal after the lock
// (spec/abi-v2/docs.md): this SDK's description is locked, so a library with
// any other fingerprint, a 2.0.0-dev build's included, is still refused as
// CHTYPES_ARTIFACT_INCOMPATIBLE naming both fingerprints, with the loader's own
// message. The dev message ("... — update your dev SDK") is a 2.0.0-dev SDK's
// alone; go/internal/abi2/devfingerprint_test.go proves its exact text with the
// unstable side selected explicitly. The expected text is spelled out here, not
// taken from the code under test.

import (
	"errors"
	"strings"
	"testing"

	"github.com/wave-rf/chtypes/go/v2/internal/abi2"
)

func requireLocked(t *testing.T) {
	t.Helper()
	if abi2.ChsAbiStability != "locked" {
		t.Fatalf("the description is %q; this test pins the locked SDK's refusal", abi2.ChsAbiStability)
	}
}

func TestDevFingerprintRefusalAfterTheLock(t *testing.T) {
	requireLocked(t)
	other := "sha256:" + strings.Repeat("0", 64)
	want := `chtypes: /p refused: fingerprint (want "` + abi2.ChsAbiFingerprint + `", got "` + other + `") [CHTYPES_ARTIFACT_INCOMPATIBLE]`

	// The mapping alone, with no library.
	err := loadError(&abi2.LoadError{Reason: "fingerprint", Path: "/p", Want: abi2.ChsAbiFingerprint, Got: other})
	var ae *ArtifactError
	if !errors.As(err, &ae) || ae.Code != CodeArtifactIncompatible || ae.Reason != "fingerprint" ||
		ae.Want != abi2.ChsAbiFingerprint || ae.Got != other || ae.Error() != want || err.Error() != want ||
		!errors.Is(err, ErrArtifactIncompatible) {
		t.Errorf("a fingerprint refusal = %v (%+v); want CHTYPES_ARTIFACT_INCOMPATIBLE with exactly %q", err, ae, want)
	}
}

// The same through a real load: the stub built to report another fingerprint
// (scripts/abi-v1/emit/_stubshared.py, "fingerprint-other").
func TestDevFingerprintRefusalThroughALoad(t *testing.T) {
	requireLocked(t)
	other := "sha256:" + strings.Repeat("0", 64)
	var ae *ArtifactError
	resetSetup(t)
	t.Setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
	path := stubFile(t, "fingerprint-other")
	_, err := OpenUnverified(path, true)
	if !errors.As(err, &ae) || ae.Code != CodeArtifactIncompatible || ae.Reason != "fingerprint" ||
		ae.Want != abi2.ChsAbiFingerprint || ae.Got != other {
		t.Fatalf("opening a library with another fingerprint = %v (%+v); want CHTYPES_ARTIFACT_INCOMPATIBLE, fingerprint, want %s, got %s",
			err, ae, abi2.ChsAbiFingerprint, other)
	}
	want := `chtypes: ` + path + ` refused: fingerprint (want "` + abi2.ChsAbiFingerprint + `", got "` + other + `") [CHTYPES_ARTIFACT_INCOMPATIBLE]`
	if ae.Error() != want || strings.Contains(ae.Error(), "dev fingerprint") {
		t.Errorf("opening a library with another fingerprint = %q; want exactly %q", ae.Error(), want)
	}
}
