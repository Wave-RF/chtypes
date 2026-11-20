package chtypes

// cpufloor_test.go — the CPU floor, loader step 2b (spec/abi-v2/docs.md,
// "Loading"): a library below its floor opens, nothing of it has run, and the
// loader refuses it as an ordinary error, carrying the library's three answers
// verbatim. They are spelled out here, not taken from the code under test.
//
// The stubs plant it on every leg with no emulation
// (scripts/abi-v1/emit/_stubshared.py, CPU_FLOOR). In cpu-floor-refused the setup
// call answers CHS_INTERNAL, so a loader that reached it reads as an
// *InternalError, never as this refusal. Without CHTYPES_ABI2_STUBS the
// stub tests skip LOUDLY by name.

import (
	"errors"
	"strings"
	"testing"

	"github.com/wave-rf/chtypes/go/v2/internal/abi2"
)

const (
	cpuFloorWant     = "x86-64-v3+pclmul"
	cpuFloorGot      = "avx2,bmi2"
	cpuFloorSentence = "this CPU cannot run this library: missing avx2,bmi2 (the 26.8 linux-amd64 build requires x86-64-v3+pclmul). Use a CPU at or above that floor."
)

// The mapping alone, with no library.
func TestCPUFloorMapping(t *testing.T) {
	err := loadError(&abi2.LoadError{Reason: "cpu_floor", Path: "/p", Want: cpuFloorWant, Got: cpuFloorGot, Sentence: cpuFloorSentence})
	var ae *ArtifactError
	if !errors.As(err, &ae) || ae.Code != "CHTYPES_CPU_UNSUPPORTED" {
		t.Fatalf("a cpu_floor refusal = %v (%+v); want an *ArtifactError with CHTYPES_CPU_UNSUPPORTED", err, ae)
	}
	if CodeCPUUnsupported != "CHTYPES_CPU_UNSUPPORTED" {
		t.Errorf("CodeCPUUnsupported = %q", CodeCPUUnsupported)
	}
	if ae.Error() != "chtypes: /p refused: "+cpuFloorSentence+" [CHTYPES_CPU_UNSUPPORTED]" {
		t.Errorf("message = %q", ae.Error())
	}
	if !errors.Is(err, ErrCPUUnsupported) || !errors.Is(err, ErrArtifactIncompatible) || errors.Is(err, ErrArtifactCorrupt) {
		t.Errorf("errors.Is: cpu %v, incompatible %v, corrupt %v; the first two hold and the last does not",
			errors.Is(err, ErrCPUUnsupported), errors.Is(err, ErrArtifactIncompatible), errors.Is(err, ErrArtifactCorrupt))
	}

	// An ordinary incompatible refusal does not match the new sentinel.
	other := loadError(&abi2.LoadError{Reason: "dlopen", Path: "/p", Got: "x"})
	if errors.Is(other, ErrCPUUnsupported) || !errors.Is(other, ErrArtifactIncompatible) {
		t.Errorf("a dlopen refusal: %v", other)
	}

	// A library with the list and no sentence still refuses, with the same code.
	bare := loadError(&abi2.LoadError{Reason: "cpu_floor", Path: "/p", Want: cpuFloorWant, Got: cpuFloorGot})
	if !errors.As(bare, &ae) || ae.Code != CodeCPUUnsupported || !strings.Contains(ae.Error(), cpuFloorGot) {
		t.Errorf("no sentence: %v (%+v)", bare, ae)
	}
}

func TestCPUFloorRefusedThroughALoad(t *testing.T) {
	resetSetup(t)
	t.Setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
	path := stubFile(t, "cpu-floor-refused")
	lib, err := OpenUnverified(path, true)
	if err == nil {
		t.Fatalf("opened a library below its CPU floor: %v", lib)
	}
	var ie *InternalError
	if errors.As(err, &ie) {
		t.Fatalf("the loader reached the library's setup call: %v", err)
	}
	var ae *ArtifactError
	if !errors.As(err, &ae) {
		t.Fatalf("got %T (%v); want an *ArtifactError", err, err)
	}
	if ae.Code != "CHTYPES_CPU_UNSUPPORTED" || ae.Reason != "cpu_floor" || ae.Path != path || ae.Want != cpuFloorWant || ae.Got != cpuFloorGot {
		t.Errorf("code %q, reason %q, path %q, want %q, got %q", ae.Code, ae.Reason, ae.Path, ae.Want, ae.Got)
	}
	if !strings.Contains(err.Error(), cpuFloorSentence) {
		t.Errorf("message %q does not carry the library's sentence", err.Error())
	}
	if !errors.Is(err, ErrCPUUnsupported) || !errors.Is(err, ErrArtifactIncompatible) {
		t.Errorf("%v: errors.Is misses a sentinel", err)
	}
	var le *abi2.LoadError
	if !errors.As(err, &le) || le.Message() != cpuFloorSentence || le.Code() != "CHTYPES_CPU_UNSUPPORTED" || le.Class() != "artifact_incompatible" {
		t.Errorf("the loader's own error: %+v", le)
	}
}

// A library from before the check is today's fingerprint refusal: not cpu_floor,
// and not a missing symbol.
func TestCPUFloorAbsentIsTheFingerprintRefusal(t *testing.T) {
	resetSetup(t)
	t.Setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
	_, err := OpenUnverified(stubFile(t, "cpu-floor-absent"), true)
	var ae *ArtifactError
	if !errors.As(err, &ae) || ae.Code != CodeArtifactIncompatible || ae.Reason != "fingerprint" {
		t.Fatalf("got %v (%+v); want the fingerprint refusal", err, ae)
	}
	if abi2.ChsAbiStability == "unstable" {
		if want := r6Message(abi2.ChsAbiFingerprint, "sha256:"+strings.Repeat("0", 64)); err.Error() != want {
			t.Errorf("message = %q, want %q", err.Error(), want)
		}
	}
}

func TestCPUFloorOKStubOpens(t *testing.T) {
	resetSetup(t)
	t.Setenv("CHTYPES_ALLOW_UNVERIFIED_LIBRARY", "1")
	if _, err := OpenUnverified(stubFile(t, "ok"), true); err != nil {
		t.Fatalf("the ok stub answers \"\" and must open: %v", err)
	}
}
