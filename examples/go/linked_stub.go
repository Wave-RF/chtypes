//go:build !chtypes_linked

package main

// Without the chtypes_linked tag the package is dlopen-only (the build every
// consumer gets), so the statically linked demonstration is announced rather
// than run. `chplay.sh go` builds with the tag when a build tree is available.

func section14() {
	section(14, "The linked image (Go only)")
	note("skipped: this binary is dlopen-only. Build with -tags chtypes_linked and")
	note("CGO_LDFLAGS pointing at the artifact producer's build tree to run it.")
}
