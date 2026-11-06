package chtypes

// main_test.go — which fetch contract this package's tests run under.
//
// The public API fetches under the production generation-2 channel
// (internal/ocifetch, channel.go) by default. These tests were written against
// the ABI v2 dev channel (abi-2 predicates, schema-2 records, the v2-dev cache,
// no pinning) and need a fixture registry signed with the test key, so the
// package runs under AllowOverridesForTests: the dev channel with exactly the
// base, trust and unsigned overrides honored, and nothing else changed. The
// production channel's own rules are internal/ocifetch's (TestConformanceProdV2,
// prodv2channel_test.go). A test of the dev channel's own refusals
// switches to it exactly (UseDevChannelForTests); a test of a rule only the v1
// contract has switches to that (UseFetchV1ForTests).

import (
	"os"
	"testing"

	"github.com/wave-rf/chtypes/go/v2/internal/ocifetch"
)

func TestMain(m *testing.M) {
	restore := ocifetch.AllowOverridesForTests()
	code := m.Run()
	restore()
	os.Exit(code)
}
