package chtypes

import (
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/wave-rf/chtypes/go/internal/abi1"
)

func TestCallErrorClasses(t *testing.T) {
	cases := []struct {
		status string
		want   string
		code   Status
	}{
		{"CHS_REJECTED", "*chtypes.SchemaError", StatusRejected},
		{"CHS_DECLINED", "*chtypes.UnsupportedError", StatusDeclined},
		{"CHS_INVALID_ARGUMENT", "*chtypes.UsageError", StatusInvalidArgument},
		{"CHS_INTERNAL", "*chtypes.InternalError", StatusInternal},
	}
	for _, c := range cases {
		err := callError(&abi1.CallError{Status: c.status, ChCode: 53, ChName: "TYPE_MISMATCH", Message: "m\xff", Column: "c\x00"})
		if got := fmt.Sprintf("%T", err); got != c.want {
			t.Errorf("%s -> %s, want %s", c.status, got, c.want)
		}
		ce, ok := AsCallError(err)
		if !ok || ce.Status != c.code || ce.ChCode != 53 || ce.ChName != "TYPE_MISMATCH" || ce.Message != "m\xff" || ce.Column != "c\x00" {
			t.Errorf("%s: AsCallError = %+v, %v: the five fields are verbatim", c.status, ce, ok)
		}
		// Wrapped, still reachable.
		if _, ok := AsCallError(fmt.Errorf("wrapped: %w", err)); !ok {
			t.Errorf("%s: AsCallError does not see through a wrap", c.status)
		}
		// The one lossy display form never contains invalid UTF-8.
		if s := err.Error(); strings.ContainsRune(s, 0xfffd) == false || strings.ToValidUTF8(s, "") != s {
			t.Errorf("%s: Error() = %q: invalid UTF-8 must be replaced for display", c.status, s)
		}
	}
}

func TestRefusalAndDeclineArePeers(t *testing.T) {
	decline := callError(&abi1.CallError{Status: "CHS_DECLINED", Message: "no"})
	var se *SchemaError
	if errors.As(decline, &se) {
		t.Error("a decline must never satisfy errors.As(*SchemaError)")
	}
	refusal := callError(&abi1.CallError{Status: "CHS_REJECTED", Message: "no"})
	var ue *UnsupportedError
	if errors.As(refusal, &ue) {
		t.Error("a refusal must never satisfy errors.As(*UnsupportedError)")
	}
}

func TestUnknownStatusIsInternalNamingItsValue(t *testing.T) {
	err := callError(&abi1.CallError{Status: "CHS_STATUS_9", Message: "?"})
	var ie *InternalError
	if !errors.As(err, &ie) || ie.Status != 9 || !strings.Contains(err.Error(), "CHS_STATUS_9") {
		t.Errorf("an unknown status = %T %v (status %d), want an *InternalError naming it", err, err, ie.Status)
	}
	if callError(nil) != nil {
		t.Error("no error is nil")
	}
}

func TestBindingDetectedMisuseHasTheInvalidArgumentShape(t *testing.T) {
	err := usageError("the schema is closed")
	ce, ok := AsCallError(err)
	if !ok || ce.Status != StatusInvalidArgument || ce.ChCode != 0 || ce.ChName != "" || ce.Column != "" || ce.Message == "" {
		t.Errorf("misuse = %+v", ce)
	}
	var ue *UsageError
	if !errors.As(err, &ue) {
		t.Errorf("%T", err)
	}
}

func TestLoaderRefusalsAreOneFamilyWithFetch(t *testing.T) {
	corrupt := loadError(&abi1.LoadError{Reason: "build_info_mismatch:core_commit", Path: "/p", Want: "a", Got: "b"})
	if !errors.Is(corrupt, ErrArtifactCorrupt) || errors.Is(corrupt, ErrArtifactIncompatible) {
		t.Errorf("a step 5 refusal = %v: want the corrupt sentinel", corrupt)
	}
	incompat := loadError(&abi1.LoadError{Reason: "fingerprint", Path: "/p", Want: "a", Got: "b"})
	if !errors.Is(incompat, ErrArtifactIncompatible) || errors.Is(incompat, ErrArtifactCorrupt) {
		t.Errorf("a fingerprint refusal = %v: want the incompatible sentinel", incompat)
	}
	var ae *ArtifactError
	if !errors.As(incompat, &ae) || ae.Reason != "fingerprint" || ae.Path != "/p" || ae.Want != "a" || ae.Got != "b" || ae.Code != CodeArtifactIncompatible {
		t.Errorf("fields = %+v", ae)
	}
	// A step 7 failure is the call's own error, never a refusal reason.
	step7 := loadError(&abi1.CallError{Status: "CHS_INVALID_ARGUMENT", Message: "zone a vs zone b"})
	var ue *UsageError
	if !errors.As(step7, &ue) {
		t.Errorf("a chs_initialize INVALID_ARGUMENT = %T, want a *UsageError", step7)
	}
	if errors.As(step7, &ae) {
		t.Error("a step 7 failure must not be an ArtifactError")
	}
	rej := loadError(&abi1.CallError{Status: "CHS_REJECTED", ChCode: 1, Message: "bad zone"})
	var se *SchemaError
	if !errors.As(rej, &se) {
		t.Errorf("a chs_initialize REJECTED = %T, want a *SchemaError", rej)
	}
	// A refused unverified open is misuse.
	if !errors.As(loadError(&abi1.UnverifiedRefusedError{Path: "/p"}), &ue) {
		t.Error("an unverified open without both opt-ins is a UsageError")
	}
}

func TestEverySharedCodeHasASentinel(t *testing.T) {
	for _, c := range []ErrorCode{
		CodeArtifactMissing, CodeArtifactUntrusted, CodeArtifactCorrupt, CodeArtifactPinned, CodeArtifactUnpublished,
		CodeSourceUnreachable, CodeSourceUnauthorized, CodeSourceForbidden, CodeSourceIncompatible, CodeArtifactIncompatible,
	} {
		e := &ArtifactError{Code: c, Msg: string(c)}
		if !errors.Is(e, sentinel(c)) || sentinel(c) == nil {
			t.Errorf("%s: no sentinel", c)
		}
	}
}

// TestWithinRequestAssertion pins the load-time assertion (public issue #481):
// a library opened for a request must report, in its own build_info, a
// clickhouse_version within that request, or the open fails as
// CHTYPES_ARTIFACT_CORRUPT with reason build_info_mismatch:clickhouse_version.
func TestWithinRequestAssertion(t *testing.T) {
	l := &Library{Version: "26.8.5.1", Path: "/cache/unpacked/sha256/x/libchtypes.so"}
	for _, ok := range []string{"26.8", "26.8.5", "26.8.5.1", "a-literal-tag"} {
		if err := checkWithinRequest(l, ok, "linux-arm64"); err != nil {
			t.Errorf("request %q answered by 26.8.5.1: want no error, got %v", ok, err)
		}
	}
	for _, bad := range []string{"26.3", "26.3.4.1", "26.8.5.2", "26.80", "26.8.15"} {
		err := checkWithinRequest(l, bad, "linux-arm64")
		var ae *ArtifactError
		if !errors.As(err, &ae) || !errors.Is(err, ErrArtifactCorrupt) {
			t.Fatalf("request %q answered by 26.8.5.1: want an *ArtifactError matching ErrArtifactCorrupt, got %T %v", bad, err, err)
		}
		if ae.Reason != "build_info_mismatch:clickhouse_version" || ae.Want != bad || ae.Got != "26.8.5.1" || ae.Request != bad {
			t.Errorf("request %q: refusal fields %+v", bad, ae)
		}
		if !strings.Contains(ae.Error(), "CHTYPES_ARTIFACT_CORRUPT") {
			t.Errorf("request %q: the message names no code: %s", bad, ae.Error())
		}
	}
}
