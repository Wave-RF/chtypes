package chtypes

// devfingerprint_test.go — rule r6's fingerprint refusal (spec/abi-v2/docs.md):
// a 2.0.0-dev SDK pins its dev fingerprint and refuses a library with any
// other as CHTYPES_ARTIFACT_INCOMPATIBLE, with exactly the rule's message. The
// message is spelled out here, not taken from the code under test.

import (
	"errors"
	"strings"
	"testing"

	"github.com/wave-rf/chtypes/go/v2/internal/abi2"
)

func r6Message(sdk, library string) string {
	return "this SDK speaks dev fingerprint " + sdk + "; the library has " + library + " — update your dev SDK"
}

func TestDevFingerprintMessageIsRuleR6Exactly(t *testing.T) {
	if abi2.ChsAbiStability != "unstable" {
		t.Skipf("SKIPPED: the description is %q; the dev message applies only while it is unstable", abi2.ChsAbiStability)
	}
	other := "sha256:" + strings.Repeat("0", 64)
	want := r6Message(abi2.ChsAbiFingerprint, other)

	// The mapping alone, with no library.
	err := loadError(&abi2.LoadError{Reason: "fingerprint", Path: "/p", Want: abi2.ChsAbiFingerprint, Got: other})
	var ae *ArtifactError
	if !errors.As(err, &ae) || ae.Code != CodeArtifactIncompatible || ae.Error() != want || err.Error() != want ||
		!errors.Is(err, ErrArtifactIncompatible) {
		t.Errorf("a fingerprint refusal = %v (%+v); want CHTYPES_ARTIFACT_INCOMPATIBLE with exactly %q", err, ae, want)
	}

}

// The same through a real load: the stub built to report another fingerprint
// (scripts/abi-v1/emit/_stubshared.py, "fingerprint-other").
func TestDevFingerprintRefusalThroughALoad(t *testing.T) {
	if abi2.ChsAbiStability != "unstable" {
		t.Skipf("SKIPPED: the description is %q; the dev message applies only while it is unstable", abi2.ChsAbiStability)
	}
	want := r6Message(abi2.ChsAbiFingerprint, "sha256:"+strings.Repeat("0", 64))
	var ae *ArtifactError
	resetSetup(t)
	t.Setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
	_, err := OpenUnverified(stubFile(t, "fingerprint-other"), true)
	if !errors.As(err, &ae) || ae.Code != CodeArtifactIncompatible || ae.Reason != "fingerprint" || ae.Error() != want {
		t.Errorf("opening a library with another fingerprint = %v; want exactly %q", err, want)
	}
}
