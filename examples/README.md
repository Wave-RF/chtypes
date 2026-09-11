# playground — a runnable tour of chtypes, in four languages

chtypes answers one question — _"if this row were inserted into this table on this ClickHouse version, what would happen?"_ — without a server. Each directory here is the same guided tour of that library from one SDK: **sixteen numbered sections, in the same order, against the same schemas and the same rows** in all four languages, so you can run two side by side and diff them. What survives the diff is the language's own idiom, which is exactly what `docs/reference/bindings.md` says a binding may vary and nothing else.

**Everything runs offline.** Each tour needs only its language's toolchain plus artifacts in the per-user cache — no Docker, no ClickHouse server, no network. Even the discovery section runs against canned bytes (clearly labeled) shaped exactly like a real server's responses.

## Run it

```bash
./chplay.sh              # run every language whose toolchain is installed
./chplay.sh go           # just one (go | python | ts | rust)
./chplay.sh --list       # what would run, and what would be skipped and why
```

`chplay.sh` checks prerequisites per language (a missing toolchain is a polite skip, not a failure), runs the tours, counts the sections each one actually printed, and exits nonzero only if a tour crashed. Or run any tour directly:

| tour      | run it                               |
| --------- | ------------------------------------ |
| `go/`     | `cd go && go run .`                  |
| `python/` | `cd python && uv run demo.py`        |
| `ts/`     | `cd ts && pnpm install && pnpm demo` |
| `rust/`   | `cd rust && cargo run`               |

All four read `$CHTYPES_REGISTRY` (default: the per-user cache) and honor `$CHTYPES_VERSION` (any spelling: `25.8`, `25.8.28.1-lts`; default: the newest line held). No artifacts yet? `../scripts/fetch.sh 25.8`. With only one version built, everything still runs — the cross-version sweeps in section 12 degrade gracefully and say so.

These are examples, not tests. The real suites live inside each binding (`../{go,python,ts,rust}/`) and the rigs (core: `tests/`). If a tour and a binding's test suite disagree, believe the test suite — then file the tour bug.

## The sixteen sections

Read any tour top to bottom as a tutorial; every section carries a comment block saying what it demonstrates, why an ingest pipeline cares, what to look at in the output, and which `chs_*` C functions it exercises.

1. **Load the library and check the ABI** — the multi-version dlopen registry, artifacts naming themselves, the ABI-revision check on both sides, and the refusal (never a fallback) for a version that is not built.
2. **Ask a build about itself** — type validation and canonicalization from the build's own `DataTypeFactory`, plus (where the SDK exposes them) the widened reference type, the type-family registry, and the function-flags audit.
3. **Compile a schema and read it back** — column introspection, DEFAULT kinds/expressions, and the two rewrites only a schema-aware compile can see (`DEFAULT NULL` making a type Nullable; an ALIAS type being inferred).
4. **Compile under a settings profile** — `flatten_nested` changing the storage shape, a type gate refusing with the server's own 455, an unknown setting name refusing with the server's own 115 _and its did-you-mean hint_, and the compile mode.
5. **Accept, reject, decline (and poison)** — the whole verdict contract, each outcome shown concretely with what a caller should do about it.
6. **DEFAULT evaluation** — where every stored value comes from (`input` / `default` / `default_substituted` / `absent`), the pinned clock, MATERIALIZED values, unknown fields, and the clock-skew budget.
7. **One schema, every format** — all ten `chs_format` codes with accepts, rejects, and each format's signature behavior; binary payloads are hand-built hex, explained byte by byte.
8. **Engines, MergeTree settings, and TTL** — SummingMergeTree's post-merge preview, the refusal-vs-decline pair on MergeTree settings, and a row accepted per row yet stored nowhere per batch (TTL).
9. **Settings precedence** — `per-call > handle profile > library defaults > ClickHouse's own`, measured layer by layer, including the same row passing under the handle profile and failing under a per-call override, and a wholesale-refused seed that commits nothing.
10. **The error taxonomy** — the typed errors, caught by type (never string matching), and why the decline type must never be mistaken for the refusal type.
11. **The discovery kit, offline** — the three canonical queries, their parsers, and DDL reconstruction against CANNED server responses; ends with the same row accepted under the discovered profile and rejected under a stock compile.
12. **Version pinning** — the same input answered differently by the artifacts resident in one process (25.10 rejecting a DEFAULT its neighbors accept; the Buffers format not existing before 26.5).
13. **Teardown** — what to release and when, per SDK (and why Go's Registry deliberately has no teardown).
14. **The static path (Go only)** — the cgo-linked single-version shape only Go has; the other three tours print a stub saying why.
15. **Export: bytes + spans** — the same `chs_rows` call serializing the batch's accepted rows to wire bytes (JSONCompactEachRow), addressed per row by index-aligned spans; the lean document flags; the fail-closed export declines (a poisoned batch emits nothing, and says why); and emitted-empty versus declined. Degrades with a note on an artifact that predates ABI revision 3.
16. **Filters: WHERE semantics at the edge** — one boolean expression compiled against the schema and evaluated per row: `x = 256` over UInt8 PROMOTING (never wrapping), NULL-is-not-true, the compiles-then-throws 'e' class, clock reads refused at compile (an UNBOUND `{p:Type}` is the server's own 456 since ABI revision 4), and the enforcement gate (shadow/replay until the WHERE-truth rig gates green). Two revision-4 sub-demos: **query parameters** (a tenant filter compiled once per (schema, expr, params) — values are strings, and a hostile `' OR 1=1 --` value is printed comparing as exactly that literal, no escaping anywhere) and **the block twin** (one 3-row event parsed ONCE, two role filters evaluated against the same block — eval(parse) ≡ rows, the live-SSE call shape). Degrades with the same note on an artifact that predates each surface.

## Where the SDKs deliberately differ

The tours print these differences rather than hiding them:

- **Loaders.** Go alone adds a statically linked path (section 14); Python (ctypes), TS (ffi-rs) and Rust (libloading) always dlopen. Optional by `docs/reference/bindings.md`.
- **Introspection.** All four SDKs expose the full trio per `Library` — `reference_type`, `registered_families`, `function_flags` — since the 2026-08-26 parity cycle (`docs/reference/bindings.md` §Introspection); Go's static path mirrors them as package-level functions.
- **Error shapes.** Peer types everywhere: Go and TS peer classes, Rust sibling enum variants, Python peer exceptions (its grandfathered `UnsupportedError(SchemaError)` subtype was retired 2026-08-26). Section 10 of each tour is the local idiom.
- **Compile mode.** Rust's `CompileMode` has one variant, so the invalid mode the other three pass through (and get the library's `-2` decline for) does not typecheck there.
- **Teardown.** Python `close()`/context manager, TS `close()`/ `Symbol.dispose`, Rust `shutdown()`/`Drop`, Go deliberately nothing (`docs/reference/bindings.md` §Teardown).

## Also here

- `go/ingest-demo/` — the **optional, online** demo: a miniature of an ingest worker running the same discovery-to-publish flow against a **real ClickHouse**. `chplay.sh` never runs it; see its README for what it needs. The offline tours' section 11 is the same flow with canned bytes.

## History

An earlier revision of these playgrounds (nine sections, live-server discovery) surfaced five cross-SDK inconsistencies in August 2026; all five were fixed in the library, the SDKs and the spec on 2026-08-26. The findings and their outcomes are recorded in `docs/reference/bindings.md` (§Teardown, §Concurrency, rule 12) and the C ABI contract (§Compile-time vs per-call settings), which is where the normative story lives.
