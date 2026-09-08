# Contributing

This repository holds the chtypes SDKs (Go, Python, TypeScript, Rust), the C
header they are written against, the spec and the playgrounds. It is Apache
2.0. The library that produces the artifacts, and the proof behind them, is
the core repository (`chtypes-core`), and a change here is finished only when
both are green.

## Ground rules

- **A binding is a passthrough.** No ClickHouse semantics are reimplemented in
  an SDK; the artifact answers, the binding presents. `spec/bindings.md` says
  what a binding may vary and what it may not.
- **Four bindings, one answer.** A behaviour change lands in all four, and the
  golden set (`goldens/`) stays green in all four. Never hand-edit
  `goldens/cases.json`; regenerate it in the core repository.
- **The header is the contract.** `include/chtypes.h` changes together with the
  four bindings' pinned constants in one commit. The frozen identity — the
  artifact name `libchtypes`, the `chs_` prefix, the `enum chs_format`
  numbers — never changes; signatures are breakable until the first tag.
- **Skips are loud, zero-runs fail.** A test that cannot load a registry says
  so and why; a suite that asserts nothing is not a pass.

## Running the gates

Fetch an artifact first (`scripts/fetch.sh 25.8`) or point `CHTYPES_REGISTRY`
at a registry, then:

```sh
scripts/check-standalone.sh        # Go, from a bare copy of go/
cd go     && go build ./... && go vet ./... && go test ./...
cd python && uv sync --group dev && uv run ruff check src tests && uv run pytest -q
cd ts     && pnpm install && pnpm build && pnpm typecheck && pnpm test
cd rust   && cargo clippy -- -D warnings && cargo test
```

`.github/workflows/ci.yml` runs the same set. The server-truth suites for these
SDKs (fixture replays against captured ClickHouse behaviour, version-specific
semantics) live in the core repository under `tests/sdk/` and run there with
this repository checked out as a sibling.

## Commit messages

One change per commit, with the measurement that backs it: which suites ran,
what they counted. "Tests pass" is not a measurement.
