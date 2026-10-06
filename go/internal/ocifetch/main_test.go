package ocifetch

// main_test.go — which fetch contract this package's tests run under.
//
// This module's non-test binaries speak the ABI v2 dev channel (channel.go).
// Most tests here, and TestConformanceV1 above all, exercise the v1 fetch
// contract the dev channel narrows: the fetch-v1 conformance cases
// (tests/fixtures/fetch-v1) are its specification, and every one names its own
// fixture registry and the test key, sets locks, and reads schema-1 records and
// abi-1 predicates. So the package runs under UseFetchV1ForTests, and the
// tests of the dev channel itself (devchannel_test.go) each switch to it with
// UseDevChannelForTests for their own duration.

import (
	"os"
	"testing"
)

func TestMain(m *testing.M) {
	restore := UseFetchV1ForTests()
	code := m.Run()
	restore()
	os.Exit(code)
}
