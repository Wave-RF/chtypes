# Reference

The deep layer, under [the guides](../guides/). Most people never need to read it: the guides and the per-language pages cover using chtypes. This is for anyone implementing against the ABI, auditing what an artifact promises, or checking whether a binding conforms.

| Page                                                                                  | For                                                                                                                |
| ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| [`go.md`](go.md) · [`python.md`](python.md) · [`ts.md`](ts.md) · [`rust.md`](rust.md) | the per-language API surface                                                                                       |
| [`artifact.md`](artifact.md)                                                          | the artifact and registry contract: file names, `manifest.json`, platform keys, multi-version layout, verification |
| [`bindings.md`](bindings.md)                                                          | the API shape every binding implements, and what one must do to claim conformance                                  |

`MUST`, `MUST NOT`, `SHOULD`, `MAY` are RFC 2119. Where a statement is an observation from a specific artifact rather than a rule, it says so and names the artifact.

The C ABI and the artifact contract are the product; Go, Python, TypeScript and Rust are peer SDKs over them. No language is privileged.

## What is normative, and where the working reference lives

**The C ABI and the artifact contract are the product; every language — go, python, ts, rust — is a peer SDK over them.** No language is privileged: for ABI-level questions the artifact's measured behavior is the ground truth, and this spec records it.

For binding-level semantics the spec cannot fully capture in prose, `go/chtypes` (Go, via cgo) is the _working reference_ — today's most complete SDK (all four SDKs are scored by the rigs). Where this spec and that package disagree, measure the artifact: whichever of the two is wrong gets the bug. Two files carry most of the semantics a binding has to reproduce:

- `go/chtypes/chtypes.go` — the surface, the result-document parsers (`rowDoc` / `batchDoc` / `colDoc`), the `quoteBareDenormals` repair, the package documentation that is the prose contract for volatile DEFAULTs, the statelessness claim and the resource envelope.
- `go/chtypes/transform.go` — silent-transformation detection and the reason vocabulary. This is the one part of the product ClickHouse does not provide, so it is the one part a binding could get wrong without the C library noticing.

`include/chtypes.h` is the ABI's own documentation and is authoritative here. The normative C ABI specification, the conformance levels and the golden-set documentation live in the core repository: this repository documents how to USE the four SDKs and enforces that they agree with each other, not how to build a fifth.

## Rules that are frozen

- The artifact base name is **`libchtypes`** and the C symbol prefix is **`chs_`**. Every prebuilt artifact (hours of C++ compute each) exports them. They are not renameable without rebuilding the entire matrix. Older Linux artifacts carried the historical file name `libchtypes_s1.so` (none remain in this machine's cache, but such artifacts circulate), which is exactly why the `manifest.json` `library` field — not the file name — is the loader's source of truth.
- **Pre-1.0, the C ABI is not additive-only.** Names and the `chs_format` integer codes are frozen (bindings pass those integers), but until the first publish a deliberate consolidation cycle may change signatures — and MUST relink every artifact and rerun both rigs in the same cycle. Symbol presence therefore proves a function EXISTS, never that its signature matches the caller's header: pair an artifact with the header it was built from. Loaders still treat a missing symbol as "this artifact cannot do that" and degrade to `unsupported`, never as a load failure. Signatures freeze at 1.0; the core repository's C ABI specification §Stability is the norm.
- **Wall-clock-dependent cases can never be goldens.** Anything whose answer depends on when it ran is either pinned via `chtypes_now_epoch_nanos` or declined; it is never recorded as an expected value.
- **Never trust exit codes or self-reports.** Assert on outputs. The verdicts in `tests/*/RESULTS.md` are computed from runs and are never hand-edited; a binding's conformance claim is worth exactly the run that produced it.
