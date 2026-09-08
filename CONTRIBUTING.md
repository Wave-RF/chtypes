# Contributing

chtypes' SDKs are thin, honest bindings over one frozen C ABI (`include/chtypes.h`,
`spec/`). Most contributions are to a binding's ergonomics, its docs, or its
golden cases; the type system itself is ClickHouse's, vendored in the native
artifact, and is not reimplemented here — a PR that re-derives ClickHouse
behaviour in Go, Python, TypeScript or Rust will be declined however good it is.

- **Build and test locally.** `scripts/fetch.sh <clickhouse-line>` installs an
  artifact for this machine into the per-user cache; each binding's README says
  how to run its suite against it. `scripts/check-standalone.sh` is the gate CI
  runs for Go.
- **Every binding follows the same contract** (`spec/`). A change to what a
  call means belongs in the spec and in all four bindings, not one.
- **Goldens are generated, not hand-edited.** `goldens/cases.json` comes from
  the core repository's generator; open an issue for a missing case.
- **Report security issues privately**: see `SECURITY.md`.

By contributing you agree your work is licensed under the Apache License 2.0
(`LICENSE`).
