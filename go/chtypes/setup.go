package chtypes

// setup.go — the process setup (bindings-v1.md section 6). The image zone and
// the default settings are process-wide in ClickHouse, so both are chosen once,
// before traffic: Setup records them, and every image gets them at load step 7.
// The record latches once an image completes step 7; before that, any failed
// open clears it, so a corrected setup can be recorded.

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
// load step 7 under it. An open commits the record, loads and latches under
// images.mu, and a failed open clears the record under images.mu too, so a
// clear never lands between another open's commit and its latch.
var setup struct {
	mu       sync.Mutex
	recorded bool
	latched  bool // an image completed load step 7 under the record
	// gen counts the records Setup made and the records failed opens cleared.
	// An open reads it when it begins, and clears the record on failure only
	// if it is unchanged: a failed open never clears a setup recorded after it
	// began.
	gen  uint64
	opts SetupOptions
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
// on Setup succeeds only with exactly the setup in effect. Until then, any
// open that fails, whatever failed (the fetch, the signature, an incompatible
// artifact, a missing symbol or step 7), clears the record: the next Setup is
// accepted, and an open with no Setup after the failure records the empty
// setup. Call it first.
func Setup(opts SetupOptions) error {
	setup.mu.Lock()
	defer setup.mu.Unlock()
	if !setup.recorded {
		setup.recorded = true
		setup.gen++
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

// setupGeneration is read when an open begins, for failedOpen.
func setupGeneration() uint64 {
	setup.mu.Lock()
	defer setup.mu.Unlock()
	return setup.gen
}

// latchSetup is called, under images.mu, when an image completes load step 7:
// from then on the setup in effect stands.
func latchSetup() {
	setup.mu.Lock()
	defer setup.mu.Unlock()
	setup.latched = true
}

// failedOpen settles an open that failed, whatever failed. While no image has
// completed step 7 it clears the record, so Setup accepts a corrected setup,
// unless a setup was recorded after the open began (gen moved). Once the setup
// has latched it changes nothing: the library's own process-once rule answers
// a different zone on an image that already has one. It takes images.mu, so it
// never lands between another open's commit and its latch.
func failedOpen(gen uint64) {
	images.mu.Lock()
	defer images.mu.Unlock()
	setup.mu.Lock()
	defer setup.mu.Unlock()
	if setup.latched || setup.gen != gen {
		return
	}
	setup.recorded, setup.opts = false, SetupOptions{}
	setup.gen++
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
