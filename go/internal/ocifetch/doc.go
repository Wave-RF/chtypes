// Package ocifetch is the OCI-distribution and zstd fetch layer of this
// module: resolve, trust, bytes, cache and lock (docs/guides/fetch-v1.md). A
// non-test binary of this module speaks the production generation-2 channel
// (channel.go; docs/guides/fetch-v1.md, "Generation 2 after the lock"): the v1
// contract with the production v2 base and the release key, a v2 cache of
// schema-2 records, abi-2 predicates. The ABI v2 dev channel a 2.0.0-dev SDK
// spoke (spec/abi-v2/docs.md, rules r5 and r6) stays, reachable only from a
// test binary.
package ocifetch
