package abi2

// devfingerprint_test.go — rule r6's dev message (spec/abi-v2/docs.md), the
// mapping alone, at each stability selected explicitly: while the description
// is unstable (a 2.0.0-dev SDK) a fingerprint refusal reads exactly as the rule
// spells it; once it is locked there is no dev message, and the refusal keeps
// the loader's own. The message is spelled out here, not taken from the code
// under test. The locked SDK's refusal through the public API is
// go/chtypes/devfingerprint_test.go.

import (
	"strings"
	"testing"
)

func TestDevFingerprintMessageAtEachStability(t *testing.T) {
	other := "sha256:" + strings.Repeat("0", 64)
	fp := &LoadError{Reason: "fingerprint", Path: "/p", Want: ChsAbiFingerprint, Got: other}
	dev := "this SDK speaks dev fingerprint " + ChsAbiFingerprint + "; the library has " + other + " — update your dev SDK"
	if got, ok := devFingerprintMessageAt("unstable", fp); !ok || got != dev {
		t.Errorf("unstable: a fingerprint refusal = %q, %v; want exactly %q", got, ok, dev)
	}
	if got, ok := devFingerprintMessageAt("locked", fp); ok || got != "" {
		t.Errorf("locked: a fingerprint refusal = %q, %v; want no dev message", got, ok)
	}
	abi := &LoadError{Reason: "abi_version", Path: "/p", Want: "2", Got: "1"}
	for _, stability := range []string{"unstable", "locked"} {
		if got, ok := devFingerprintMessageAt(stability, abi); ok || got != "" {
			t.Errorf("%s: an abi_version refusal = %q, %v; want no dev message", stability, got, ok)
		}
	}
}
