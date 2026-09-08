# playground/rust — the Rust tour

Sixteen sections over the whole chtypes surface, matching `../go`,
`../python` and `../ts` section for section. See
[`../README.md`](../README.md) for the section list. **Runs entirely
offline** — no Docker, no ClickHouse server, no network.

## Run it

```bash
cargo run           # or, with prerequisite checks: ../chplay.sh rust
```

## Prerequisites

- **Artifacts.** The tour reads `$CHTYPES_REGISTRY`, defaulting to the repo's
  the per-user cache (`~/.cache/chtypes/artifacts/<os>-<arch>/`) or wherever
  `$CHTYPES_REGISTRY` points. No artifacts? `../../scripts/fetch.sh 25.8`, or
  build one in the core repository. One version is enough; section 12's cross-version
  sweeps want several and degrade gracefully without them.
- Rust 1.85+ (the binding is edition 2024), and a **Unix** host: the loader
  is `dlopen`, and `rust` has a `compile_error!` for anything else.

## Knobs

| variable | effect |
|---|---|
| `CHTYPES_VERSION` | which artifact the tour uses (`25.8`, `25.8.28.1-lts`, …). Default: the newest line held |
| `CHTYPES_REGISTRY` | the artifact directory. Default: the per-user cache (`Registry::from_env_or_default`) |

## What is Rust-specific here

Everything in the tour is the same *concept* in all four SDKs; these are the
places where the Rust spelling is its own.

- **A compile builder.** `lib.compile(ddl).settings(...).mode(...).compile()`
  — Rust has neither named nor optional arguments, and the alternative was
  `None, CompileMode::Declared` at every call site.
- **Sibling enum variants.** `Error::Schema` and `Error::Unsupported` are
  variants of one `#[non_exhaustive]` enum — never a subtype relationship.
  `err.code()` / `err.is_unsupported()` in section 10.
- **The invalid compile mode does not typecheck.** `CompileMode` has exactly
  one variant, so the out-of-range mode the other three tours pass through
  is not constructible here (section 4d).
- **The full introspection group.** `validate_type`, `reference_type`
  (returns `Option`), `registered_families` and `function_flags` (section 2).
  Rust was the complete column the other three SDKs were brought up to in the
  2026-08-26 parity cycle.
- **Drop frees.** Schema handles free on `Drop`; `Registry::shutdown()` (and
  `Library::shutdown()`) run `chs_shutdown` explicitly (section 13).
