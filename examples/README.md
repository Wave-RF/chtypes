# playground — a runnable tour of chtypes, in four languages

chtypes answers one question — _"if this row were inserted into this table on this ClickHouse version, what would happen?"_ — without a server. Each directory here is the same guided tour of that library from one SDK: **eighteen numbered sections, in the same order, against the same schemas and the same rows** in all four languages, so you can run two side by side and diff them. What survives the diff is the language's own idiom, which is exactly what `docs/reference/bindings-v1.md` says a binding may vary and nothing else.

**The tours need a real artifact, and nothing else.** Each tour needs its language's toolchain plus one library in the per-user v1 cache (`${XDG_CACHE_HOME:-~/.cache}/chtypes/v1`, or the directory `CHTYPES_CACHE` names). No Docker, no ClickHouse server. Fetching that library is the only network step; after it the tours run offline, and even the discovery section runs against canned bytes (clearly labeled) shaped exactly like a real server's responses.

## Run it

```bash
./chplay.sh              # run every language whose toolchain is installed
./chplay.sh go           # just one (go | python | ts | rust)
./chplay.sh --list       # what would run, and what would be skipped and why
./chplay.sh --require-all --locked   # the CI form: a skip fails, lockfiles as committed
```

`chplay.sh` checks prerequisites per language (a missing toolchain is a polite skip, not a failure). Before each tour it fetches one line through THAT binding's own `chtypes fetch` command, so a tour exercises the same fetch path a user gets. It then runs the tour, counts the sections the tour actually printed, and exits nonzero if a fetch or a tour failed or a tour printed fewer than eighteen sections.

| variable                | effect                                                                                                |
| ----------------------- | ----------------------------------------------------------------------------------------------------- |
| `CHPLAY_LINE`           | the line `chplay.sh` fetches before each tour (default `26.8`)                                        |
| `CHPLAY_FETCH_ARGS`     | extra flags for that fetch, for example `--offline` on a machine whose cache is already seeded        |
| `CHTYPES_CACHE`         | the cache directory every fetch and tour uses                                                         |
| `CHTYPES_VERSION`       | which build a tour opens: any spelling (`26.8`, `26.8.15.10-lts`). Default: the newest line installed |
| `CHTYPES_ARTIFACTS_URL` | the registry base the fetch reads (the only base override; there is no flag)                          |

Or run any tour directly, after `chtypes fetch 26.8` with that binding's CLI:

| tour      | run it                               |
| --------- | ------------------------------------ |
| `go/`     | `cd go && go run .`                  |
| `python/` | `cd python && uv run demo.py`        |
| `ts/`     | `cd ts && pnpm install && pnpm demo` |
| `rust/`   | `cd rust && cargo run`               |

These are examples, not tests. The real suites live inside each binding (`../{go,python,ts,rust}/`) and the artifact producer's server-comparison suites. If a tour and a binding's test suite disagree, believe the test suite — then file the tour bug. CI type-checks or compiles every tour; it runs a tour only against a real artifact, and the `examples` check passes vacuously until the `V1_READY` marker is added to `examples/<lang>/`.

## The eighteen sections

Read any tour top to bottom as a tutorial; every section carries a comment block saying what it demonstrates, why an ingest pipeline cares, and what to look at in the output. A section whose v0 feature the v1 API deletes (`docs/reference/bindings-v1.md` section 7) keeps its number and prints which deletion removed it, so the four tours stay diffable.

1. **Set up, open a library, check what it is** — the cache, the v1 fetch layer, the build naming itself, the ABI fingerprint check on both sides, and the refusal (never a fallback) for a version that is not installed.
2. **Ask a build about itself** — type validation and canonicalization from the build's own `DataTypeFactory`.
3. **Compile a schema and read it back** — column introspection, DEFAULT kinds and expressions, and the rewrites only a schema-aware compile can see.
4. **Compile under a settings profile** — a setting changing the storage shape, a type gate refusing with the server's own error, and an unknown setting name refusing with the server's did-you-mean hint.
5. **Accept, reject, decline (and poison)** — the whole verdict contract, each outcome shown concretely with what a caller should do about it.
6. **DEFAULT evaluation: where every value comes from** — `input` / `default` / `default_substituted` / `absent`, the pinned clock, MATERIALIZED values, unknown fields, and the clock-skew budget.
7. **One schema, every format** — every supported format with accepts, rejects and each format's signature behavior; binary payloads are hand-built hex, explained byte by byte.
8. **Engines, MergeTree settings, TTL and partitions** — SummingMergeTree's post-merge preview, the refusal-versus-decline pair on MergeTree settings, and a row accepted per row yet stored nowhere per batch (TTL).
9. **Settings and zones: who wins** — settings precedence measured layer by layer, and time zones.
10. **The error taxonomy** — the typed errors, caught by type (never string matching), and why the decline type must never be mistaken for the refusal type.
11. **Discovery, offline** — the canonical discovery queries, their parsers, and DDL reconstruction against CANNED server responses.
12. **Version pinning: same input, different answers** — the same input answered differently by the artifacts resident in one process; degrades gracefully, and says so, with only one build installed.
13. **Lifetimes and teardown** — what to release and when, per SDK (and why Go deliberately has no teardown).
14. **The static path (Go only)** — the cgo-linked single-version shape only Go has; the other three tours print a stub saying why.
15. **Export: bytes + spans** — serializing the batch's accepted rows to wire bytes, addressed per row by index-aligned spans, and the fail-closed export declines.
16. **Filters: WHERE semantics at the edge** — one boolean expression compiled against the schema and evaluated per row, query parameters, and the block twin.
17. **The INSERT column list** — a list naming an `EPHEMERAL` column whose value is read and feeds a DEFAULT but is never stored, then one refusal.
18. **The server profile (ABI v2)** — a server described by its timezone, a table compiled on it, and a `timezone()` DEFAULT filled with the server's zone. A build that does not compile on a server yet declines, and the section says SKIPPED by name.

## Where the SDKs deliberately differ

The tours print these differences rather than hiding them:

- **Loaders.** Go alone adds a statically linked path (section 14); Python (ctypes), TS (ffi-rs) and Rust (libloading) always dlopen. Optional by `docs/reference/bindings-v1.md`.
- **Error shapes.** Peer types everywhere: Go and TS peer classes, Rust sibling enum variants, Python peer exceptions. Section 10 of each tour is the local idiom.
- **Teardown.** Python `close()` and the context manager, TS `close()` and `Symbol.dispose`, Rust `Drop`, Go deliberately nothing.

## Also here

- `go/ingest-demo/` — the **optional, online** demo: a miniature of an ingest worker running the same discovery-to-publish flow against a **real ClickHouse**. `chplay.sh` never runs it; see its README for what it needs. The offline tours' section 11 is the same flow with canned bytes.
