package ocifetch

import (
	"errors"
	"fmt"
	"strings"
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

// The message alone carries what the error concerns (public issue #500): the
// public ArtifactError keeps no Request, Platform or Source field, so a text
// like ensure.go's "layer: %v", which names none of them, must gain all three,
// and one that already names one must not repeat it.
func TestFetchErrorMessageCarriesRequestPlatformSource(t *testing.T) {
	err := newError(CodeArtifactCorrupt, "26.8", "linux-arm64", "https://registry.example/chtypes/v2",
		errors.New("digest mismatch"), "layer: %v", errors.New("digest mismatch"))
	want := "chtypes: layer: digest mismatch (request 26.8, platform linux-arm64, " +
		"source https://registry.example/chtypes/v2) [CHTYPES_ARTIFACT_CORRUPT]"
	if err.Msg != want || err.Error() != want {
		t.Fatalf("message = %q, want %q", err.Msg, want)
	}

	named := newError(CodeArtifactMissing, "26.8", "linux-arm64", "", nil,
		"nothing installed answers %s for %s", "26.8", "linux-arm64")
	if want := "chtypes: nothing installed answers 26.8 for linux-arm64 [CHTYPES_ARTIFACT_MISSING]"; named.Msg != want {
		t.Fatalf("a message that names the request and platform = %q, want %q", named.Msg, want)
	}

	secret := newError(CodeSourceUnreachable, "26.8", "", "https://user:hunter2@registry.example/chtypes/v2", nil, "boom")
	if strings.Contains(secret.Msg, "hunter2") || !strings.Contains(secret.Msg, "source https://user:xxxxx@registry.example/chtypes/v2") {
		t.Fatalf("a source URL's password reached the message: %q", secret.Msg)
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
