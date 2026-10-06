package abi2

// identity_test.go — the binding's compiled-in ABI identity, printed for the
// jobs that test the Go binding without loading a library: each one reads this
// line and asserts it against spec/binding-majors.json and the description at
// its commit (scripts/abi-v1/majors.py assert-identity), so a job never takes
// the map's word for the major it tested.

import (
	"strings"
	"testing"
)

func TestABIIdentity(t *testing.T) {
	if ChsAbiVersion < 1 || !strings.HasPrefix(ChsAbiFingerprint, "sha256:") || len(ChsAbiFingerprint) != len("sha256:")+64 {
		t.Fatalf("the compiled identity is malformed: %d %q", ChsAbiVersion, ChsAbiFingerprint)
	}
	t.Logf("chtypes_abi_identity binding=go abi=%d fingerprint=%s", ChsAbiVersion, ChsAbiFingerprint)
}
