// Package ocifetch is the v1 OCI-distribution and zstd fetch layer shared by
// every chtypes binding's Go code: resolve, trust, bytes, cache and lock,
// against the OCI registry at registry.wavehouse.dev (docs/guides/fetch-v1.md).
// It exports nothing to the rest of this module yet; the Go v1 lane wires
// go/chtypes onto it once the seam (the v1 fetch-layer plan's §1.3) is
// implemented here.
package ocifetch
