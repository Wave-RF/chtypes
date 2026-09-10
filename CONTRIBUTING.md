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
