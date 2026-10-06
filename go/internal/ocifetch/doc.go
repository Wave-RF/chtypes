// Package ocifetch is the OCI-distribution and zstd fetch layer of this
// module: resolve, trust, bytes, cache and lock (docs/guides/fetch-v1.md). A
// non-test binary of this 2.0.0-dev module speaks the ABI v2 dev channel, which
// narrows the v1 contract (channel.go; spec/abi-v2/docs.md, rules r5 and r6):
// the staging base and key only, no override, no pinning, a v2-dev cache of
// schema-2 records, abi-2 predicates.
package ocifetch
