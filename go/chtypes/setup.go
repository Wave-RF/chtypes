package chtypes

// setup.go — the process setup (bindings-v1.md section 6). The image zone and
// the default settings are process-wide in ClickHouse, so both are chosen once,
// before traffic: Setup records them, and every image gets them at load step 7.

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

var setup struct {
	mu       sync.Mutex
	recorded bool
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
// first setup stands. The first open commits an empty setup if Setup was never
// called, after which Setup succeeds only with exactly the setup in effect.
// Call it first.
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

// commitSetup returns the setup every image is loaded under, committing the
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
