# Reference

The deep layer, under [the guides](../guides/). Most people never need to read it: the guides and the per-language pages cover using chtypes. This is for anyone implementing against the ABI, auditing what an artifact promises, or checking whether a binding conforms.

| Page                                                                                  | For                                                                                                                                        |
| ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| [`go.md`](go.md) · [`python.md`](python.md) · [`ts.md`](ts.md) · [`rust.md`](rust.md) | the per-language API surface                                                                                                               |
| [`c-abi.md`](c-abi.md)                                                                | the ground truth: every `chs_*` function, ownership, thread-safety, the error model, and the exact `chs_row` / `chs_rows` result documents |
| [`artifact.md`](artifact.md)                                                          | the artifact and registry contract: file names, `manifest.json`, platform keys, multi-version layout, verification                         |
| [`bindings.md`](bindings.md)                                                          | the API shape every binding implements, and what one must do to claim conformance                                                          |
| [`goldens.md`](goldens.md)                                                            | the served golden set, and why it shrinks as ClickHouse lines are added                                                                    |

`MUST`, `MUST NOT`, `SHOULD`, `MAY` are RFC 2119. Where a statement is an observation from a specific artifact rather than a rule, it says so and names the artifact.

The C ABI and the artifact contract are the product; Go, Python, TypeScript and Rust are peer SDKs over them. No language is privileged.

## What is normative, and where the working reference lives

**The C ABI and the artifact contract are the product; every language — go, python, ts, rust — is a peer SDK over them.** No language is privileged: for ABI-level questions the artifact's measured behavior is the ground truth, and this spec records it.

For binding-level semantics the spec cannot fully capture in prose, `go/chtypes` (Go, via cgo) is the _working reference_ — today's most complete SDK (all four SDKs are scored by the rigs). Where this spec and that package disagree, measure the artifact: whichever of the two is wrong gets the bug. Two files carry most of the semantics a binding has to reproduce:

- `go/chtypes/chtypes.go` — the surface, the result-document parsers (`rowDoc` / `batchDoc` / `colDoc`), the `quoteBareDenormals` repair, the package documentation that is the prose contract for volatile DEFAULTs, the statelessness claim and the resource envelope.
- `go/chtypes/transform.go` — silent-transformation detection and the reason vocabulary. This is the one part of the product ClickHouse does not provide, so it is the one part a binding could get wrong without the C library noticing.

`include/chtypes.h` is the ABI's own documentation and is authoritative for anything `c-abi.md` restates.

## Three levels of conformance

A binding can be conformant at three increasing levels. Claim the level you actually verified.

**Level 1 — ABI conformance.** The binding loads artifacts correctly: it resolves the library file name from `manifest.json` (never by guessing), calls `chs_init` exactly once per loaded library, frees every returned `char *` with that same library's `chs_free`, keeps one handle to one thread at a time, and tolerates a symbol the artifact does not export instead of failing to load. Verifiable against a single artifact directory, offline, in minutes.

**Level 2 — surface conformance.** The binding exposes the names and semantics in `bindings.md`, including `Transformed` reporting and minor-line version resolution, and reproduces the worked examples in `bindings.md` byte for byte (modulo the platform caveats stated there).

**Level 3 — semantic conformance.** The binding drives the JSONL oracle protocol (documented with the conformance suites in the core repository) and is scored by the rigs against ground truth captured from real ClickHouse servers. This is the only level that means anything about correctness, because it is the only one where an outside process compares the answer to a real server's.

## What conformance is scored on

The rigs do not report a single number, and the ranking of the numbers is a product decision, not a statistical one:

1. **Silently different** — both accept, different stored value. Corrupts data with no signal at any point. This is the worst thing the product can emit.
2. **Missed transformation** — the value changed and `Transformed` did not say so. Same failure mode as (1) from the tenant's point of view.
3. **Over-accepts and over-rejects — both zero-budget, neither ranked behind the other.** An over-accept says _accepted_ where the server says _rejected_: in a gateway that previews rows to subscribers before the insert lands, rows were already streamed and then the insert failed — a phantom accept, caught loudly at the real insert. An over-reject says _rejected_ where the server would have taken the row: nothing fails, nothing is logged downstream, and the row is simply gone — silent data loss for the consumer, which is at least as bad. Neither has a budget, and coverage must never rise while over-accepts do.
4. Error-code mismatches: wrong, visible, survivable.
5. Coverage: cases answered at all. Declaring an area `unsupported` raises agreement and lowers coverage, and both are reported side by side so the trade is never invisible.

`unsupported` is a first-class answer and is **never** scored as agreement. A binding that cannot answer a case MUST say `unsupported` rather than guess. A guess that happens to be right is worth less than a decline, because it is not repeatable.

## Rules that are frozen

- The artifact base name is **`libchtypes`** and the C symbol prefix is **`chs_`**. Every prebuilt artifact (hours of C++ compute each) exports them. They are not renameable without rebuilding the entire matrix. Older Linux artifacts carried the historical file name `libchtypes_s1.so` (none remain in this machine's cache, but such artifacts circulate), which is exactly why the `manifest.json` `library` field — not the file name — is the loader's source of truth.
- **Pre-1.0, the C ABI is not additive-only.** Names and the `chs_format` integer codes are frozen (bindings pass those integers), but until the first publish a deliberate consolidation cycle may change signatures — and MUST relink every artifact and rerun both rigs in the same cycle. Symbol presence therefore proves a function EXISTS, never that its signature matches the caller's header: pair an artifact with the header it was built from. Loaders still treat a missing symbol as "this artifact cannot do that" and degrade to `unsupported`, never as a load failure. Signatures freeze at 1.0; `docs/reference/c-abi.md` §Stability is the norm.
- **Wall-clock-dependent cases can never be goldens.** Anything whose answer depends on when it ran is either pinned via `chtypes_now_epoch_nanos` or declined; it is never recorded as an expected value.
- **Never trust exit codes or self-reports.** Assert on outputs. The verdicts in `tests/*/RESULTS.md` are computed from runs and are never hand-edited; a binding's conformance claim is worth exactly the run that produced it.

## Provenance of everything in this spec

Every field, code, and example in these documents was read out of the source or observed by running it — base capture 2026-08-17, revised through the 2026-08-26 relink:

- Signatures and ownership: `include/chtypes.h`.
- Result-document field shapes: raw `chs_row` / `chs_rows` output captured from the `darwin-arm64/25.8` artifact (ClickHouse `25.8.28.1-lts`) driven directly through the C ABI.
- Worked examples: the core repository's oracle driver with JSONL on stdin, across all six versions then loaded (seven exist now; see `bindings.md` §Worked examples for the capture scope).
- Symbol tables: `nm` on both the `darwin-arm64` and `linux-arm64` artifacts.
- Manifest fields: real manifests under `~/.cache/chtypes/artifacts/<os>-<arch>/<minor>/`, cross-checked against the writer in the core repository's build script.

Where an observation is platform-sensitive — floats above all, because macOS's `long double` is 53-bit and its float parses diverge from a real server — the document says so and names the platform it came from.
