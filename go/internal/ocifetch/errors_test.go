package ocifetch

import (
	"errors"
	"fmt"
	"testing"
)

func TestFetchErrorIsSentinel(t *testing.T) {
	err := newError(CodeArtifactCorrupt, "26.8", "linux-arm64", "https://example/x", nil, "boom")
	if !errors.Is(err, ErrArtifactCorrupt) {
		t.Fatalf("errors.Is(err, ErrArtifactCorrupt) = false, want true")
	}
	if errors.Is(err, ErrArtifactUntrusted) {
		t.Fatalf("errors.Is(err, ErrArtifactUntrusted) = true, want false")
	}
	wrapped := fmt.Errorf("context: %w", err)
	if !errors.Is(wrapped, ErrArtifactCorrupt) {
		t.Fatalf("errors.Is through fmt.Errorf wrap = false, want true")
	}
	var fe *FetchError
	if !errors.As(wrapped, &fe) {
		t.Fatalf("errors.As(wrapped, &fe) = false, want true")
	}
	if fe.Request != "26.8" || fe.Platform != "linux-arm64" {
		t.Fatalf("FetchError fields = %+v, want Request=26.8 Platform=linux-arm64", fe)
	}
}

func TestExitCodeTable(t *testing.T) {
	cases := []struct {
		code ErrorCode
		want int
	}{
		{CodeArtifactMissing, 1},
		{CodeArtifactUntrusted, 1},
		{CodeArtifactCorrupt, 1},
		{CodeArtifactPinned, 1},
		{CodeArtifactUnpublished, 4},
		{CodeSourceUnreachable, 3},
		{CodeSourceUnauthorized, 5},
		{CodeSourceForbidden, 6},
		{CodeSourceIncompatible, 7},
		{CodeArtifactIncompatible, 8},
		{CodeCacheUnusable, 9},
		{CodeSourceRetired, 10},
	}
	for _, c := range cases {
		if got := c.code.ExitCode(); got != c.want {
			t.Errorf("%s.ExitCode() = %d, want %d", c.code, got, c.want)
		}
	}
}

func TestExitCodeFunction(t *testing.T) {
	if ExitCode(nil) != 0 {
		t.Fatalf("ExitCode(nil) != 0")
	}
	err := newError(CodeSourceUnreachable, "", "", "", nil, "x")
	if got := ExitCode(err); got != 3 {
		t.Fatalf("ExitCode(sourceUnreachable) = %d, want 3", got)
	}
	if got := ExitCode(errors.New("plain")); got != 1 {
		t.Fatalf("ExitCode(plain error) = %d, want 1", got)
	}
}

func TestArtifactIncompatibleHasNoSentinel(t *testing.T) {
	// CHTYPES_ARTIFACT_INCOMPATIBLE is reserved for the FFI/loader layer;
	// this package never constructs one, but the table entry must still
	// exist so ExitCode is complete if a caller merges both layers' errors.
	if CodeArtifactIncompatible.Sentinel() != nil {
		t.Fatalf("CodeArtifactIncompatible.Sentinel() should be nil: the fetch layer never raises this code")
	}
}
