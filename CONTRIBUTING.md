# Contributing

chtypes' SDKs are thin, honest bindings over one frozen C ABI (`include/chtypes.h`,
`spec/`). Most contributions are to a binding's ergonomics, its docs, or its
golden cases; the type system itself is ClickHouse's, vendored in the native
artifact, and is not reimplemented here — a PR that re-derives ClickHouse
behaviour in Go, Python, TypeScript or Rust will be declined however good it is.

- **Build and test locally.** `scripts/fetch.sh <clickhouse-line>` installs an
  artifact for this machine into the per-user cache; each binding's README says
  how to run its suite against it. Without one, every test that needs an
  artifact skips by name and the rest still runs — `scripts/check-suite.sh
  --no-artifacts <lang>` and `scripts/check-standalone.sh --no-artifacts` are
  exactly what CI runs first; it then runs the same suites with two published
  lines. The artifact-backed proof beyond that lives in the core repository.
- **Every binding follows the same contract** (`spec/`). A change to what a
  call means belongs in the spec and in all four bindings, not one.
- **Goldens are served, not tracked.** The golden set is published in the
  rolling release as `sdk-goldens.json` and installed beside the artifacts by
  `scripts/fetch.sh`; there is no cases file in this repository to edit. Open an
  issue against the core repository for a missing case (`goldens/README.md`).
- **Report security issues privately**: see `SECURITY.md`.

By contributing you agree your work is licensed under the Apache License 2.0
(`LICENSE`).

## Linting

Each binding has its own linter, configured to be strict but green on the current tree: `go/.golangci.yml` (golangci-lint), `ts/biome.json` (Biome), `[lints]` in `rust/Cargo.toml` (clippy, already run by CI's `rust` job), and `python/pyproject.toml`'s `[tool.ruff.lint]` section (already run by CI's `python` job). `scripts/lint-actions.sh` runs actionlint over `.github/workflows/*.yml` and shellcheck over every tracked `*.sh`.

`scripts/lint-prose.sh` enforces American spelling over the whole tree — every tracked file except `**/fixtures/fetch/**` and `LICENSE`, no other exceptions. It runs two independent checks: `misspell -locale US`, and a small grep-based wordlist for a specific family of words (`canonicalise`, `normalise`, `optimisation`, `specialise`, `generalise`, `initialiser`, `desynchronise`, `parameterise`, `unmodelled`, `unmarshalling`, and their inflections) that misspell's own dictionary matcher has been proven not to catch reliably, even though some of them are present in its built-in list — see the long comment at the top of that script. Run `scripts/lint-prose.sh --selftest` to see the proof yourself: it builds a small probe file, shows `misspell -locale US` missing most of it, and shows the wordlist grep catching all of it.

Markdown formatting and structure (`dprint check`, `.markdownlint.json`) run in CI's `prose` job but do not block merges yet, for the same reason: this tree's markdown was written hard-wrapped and most of it hasn't been through a `dprint fmt` pass. Dependency vulnerability scanning (`govulncheck`, `pip-audit`, `pnpm audit`, `cargo audit`) runs in CI's `security` job and also does not block — a fresh advisory against a pinned dependency with no fix available yet should not stall every unrelated PR. Both jobs still report their findings on every run.

Run any of these locally with the same command CI uses; each job's step name in `.github/workflows/ci.yml` names the exact invocation.
