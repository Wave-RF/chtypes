//go:build !chtypes_linked

package main

// Without the chtypes_linked tag the package is dlopen-only (the build every
// consumer gets), so the two statically linked demonstrations are announced
// rather than run. `chplay.sh go` builds with the tag when the core
// repository is beside this one.

func section9Static(iso []byte, basic, bestEffort map[string]string) {
	_, _, _ = iso, basic, bestEffort
	note("(the library-defaults layer needs the statically linked shape:")
	note(" go run -tags chtypes_linked . with CGO_LDFLAGS=-L<core>/lib/build)")
}

func section14() {
	section(14, "The static path (Go only)")
	note("skipped: this binary is dlopen-only. Build with -tags chtypes_linked and")
	note("CGO_LDFLAGS=-L<core>/lib/build (chplay.sh go does this) to run it.")
}
