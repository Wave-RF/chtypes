package chtypes

// setup.go — the process setup (bindings-v1.md section 6). The image zone and
// the default settings are process-wide in ClickHouse, so both are chosen once,
// before traffic: Setup records them, and every image gets them at load step 7.
// The record latches once an image completes step 7; a step 7 failure before
// that clears it, so a corrected setup can be recorded.

import (
	"reflect"
	"sync"
)

// SetupOptions is the process setup: the image zone, which governs compiled
// types, and the default settings, which layer under every call's own.
type SetupOptions struct {
	// Timezone is the image zone, as a name; empty means the library's
	// default, UTC. It is never canonicalized here.
	Timezone string
	// Defaults are the default settings, fixed for the life of the process.
	Defaults map[string]string
}

// setup is the setup guard: the record, and whether any image has completed
// load step 7 under it. Every open holds images.mu around its commit, its load
// and its settle, so the record a load ran under is the one it settles.
var setup struct {
	mu       sync.Mutex
	recorded bool
	latched  bool // an image completed load step 7 under the record
	opts     SetupOptions
}

func sameSetup(a, b SetupOptions) bool {
	if a.Timezone != b.Timezone {
		return false
	}
	if len(a.Defaults) == 0 && len(b.Defaults) == 0 {
		return true
	}
	return reflect.DeepEqual(a.Defaults, b.Defaults)
}

func describeSetup(o SetupOptions) string {
	defaults, err := stringMapJSON(o.Defaults)
	if err != nil || defaults == nil {
		defaults = []byte("{}")
	}
	return "timezone " + quoteForMessage(o.Timezone) + ", defaults " + string(defaults)
}

func quoteForMessage(s string) string {
	b, err := marshalNoEscape(s)
	if err != nil {
		return `"?"`
	}
	return string(b)
}

// Setup records the process setup. It records and does not load: called before
// any library is open it stores the zone and defaults, and called again with
// the same zone, byte for byte, and the same defaults it is a no-op. A
// different zone or different defaults is a *UsageError naming both, and the
// first setup stands. The first open records the empty setup if Setup was
// never called. The setup latches once an image completes load step 7
// (chs_initialize, then chs_set_defaults when there are defaults); from then
// on Setup succeeds only with exactly the setup in effect. If step 7 fails
// before any image has completed it, the record is cleared, so a corrected
// Setup is accepted and the next open runs step 7 with it. Call it first.
func Setup(opts SetupOptions) error {
	setup.mu.Lock()
	defer setup.mu.Unlock()
	if !setup.recorded {
		setup.recorded = true
		setup.opts = SetupOptions{Timezone: opts.Timezone, Defaults: copyMap(opts.Defaults)}
		return nil
	}
	if sameSetup(setup.opts, opts) {
		return nil
	}
	return usageError("setup is already %s; refusing a different setup, %s", describeSetup(setup.opts), describeSetup(opts))
}

// commitSetup returns the setup every image is loaded under, recording the
// empty setup when Setup was never called.
func commitSetup() (zone []byte, defaults []byte, err error) {
	setup.mu.Lock()
	defer setup.mu.Unlock()
	setup.recorded = true
	defaults, err = stringMapJSON(setup.opts.Defaults)
	if err != nil {
		return nil, nil, err
	}
	return []byte(setup.opts.Timezone), defaults, nil
}

// settleSetup records how load step 7 ended, under the setup guard. A success
// latches the setup in effect. A failure before any image has completed step 7
// clears the record, so Setup accepts a corrected setup; once the setup has
// latched, a failure changes nothing (the library's own process-once rule
// answers a different zone on an image that already has one).
func settleSetup(completed bool) {
	setup.mu.Lock()
	defer setup.mu.Unlock()
	if completed {
		setup.latched = true
		return
	}
	if !setup.latched {
		setup.recorded, setup.opts = false, SetupOptions{}
	}
}

func copyMap(m map[string]string) map[string]string {
	if len(m) == 0 {
		return nil
	}
	out := make(map[string]string, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}
