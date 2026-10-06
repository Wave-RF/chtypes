package chtypes

// main_test.go — which fetch contract this package's tests run under.
//
// The public API fetches under the ABI v2 dev channel (internal/ocifetch,
// channel.go): abi-2 predicates, schema-2 records, the v2-dev cache, no
// pinning, and no override of the base or the trust list. These tests need a
// fixture registry signed with the test key, so the package runs under
// AllowOverridesForTests: the dev channel with exactly those overrides
// honored, and nothing else changed. A test of the dev channel's own refusals
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
