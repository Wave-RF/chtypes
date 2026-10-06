package main

// seam_test.go — which fetch contract this package's tests run under.
//
// The CLI a user runs speaks the ABI v2 dev channel (internal/ocifetch,
// channel.go). The tests in main_test.go exercise the command line over the
// v1 fetch contract the dev channel narrows (a fixture registry, the test key,
// locks), so this package runs under UseFetchV1ForTests. devchannel_test.go
// switches each of its tests to the dev channel, and builds the real binary
// once to prove a non-test binary speaks it with no seam at all.

import (
	"os"
	"testing"

	"github.com/wave-rf/chtypes/go/v2/internal/ocifetch"
)

func TestMain(m *testing.M) {
	restore := ocifetch.UseFetchV1ForTests()
	code := m.Run()
	restore()
	os.Exit(code)
}
