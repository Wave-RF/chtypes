# Contributing

chtypes' SDKs are thin, honest bindings over one frozen C ABI (`include/chtypes.h`, `docs/reference/`). Most contributions are to a binding's ergonomics, its docs, or its golden cases; the type system itself is ClickHouse's, vendored in the native artifact, and is not reimplemented here — a PR that re-derives ClickHouse behavior in Go, Python, TypeScript or Rust will be declined however good it is.

- **Build and test locally.** `scripts/fetch.sh <clickhouse-line>` installs an artifact for this machine into the per-user cache; each binding's README says how to run its suite against it. Without one, every test that needs an artifact skips by name and the rest still runs — `scripts/check-suite.sh --no-artifacts <lang>` and `scripts/check-standalone.sh --no-artifacts` are exactly what CI runs first; it then runs the same suites with two published lines. The artifact-backed proof beyond that lives in the core repository.
- **Every binding follows the same contract** (`docs/reference/`). A change to what a call means belongs in the spec and in all four bindings, not one.
- **Goldens are served, not tracked.** The golden set is published in the rolling release as `sdk-goldens.json` and installed beside the artifacts by `scripts/fetch.sh`; there is no cases file in this repository to edit. Open an issue here for a missing case.
- **Report security issues privately**: see `SECURITY.md`.

By contributing you agree your work is licensed under the Apache License 2.0 (`LICENSE`).

## Linting

Each binding has its own linter, configured to be strict but green on the current tree: `go/.golangci.yml` (golangci-lint), `ts/biome.json` (Biome), `[lints]` in `rust/Cargo.toml` (clippy, already run by CI's `rust` job), and `python/pyproject.toml`'s `[tool.ruff.lint]` section (already run by CI's `python` job). `scripts/lint-actions.sh` runs actionlint over `.github/workflows/*.yml` and shellcheck over every tracked `*.sh`.

`scripts/lint-prose.sh` enforces American spelling over the whole tree — every tracked file except `**/fixtures/fetch/**` (generated and signed) and `LICENSE` (legal text), plus the script itself, whose wordlist is British spellings by necessity. There are no other exceptions.

It runs two independent checks, because one is not enough: `misspell -locale US`, and a grep backstop for a family of words misspell's matcher has been proven not to fire on reliably even when they are in its own dictionary. The word list lives in the script and nowhere else, so this page cannot drift from it. Run `scripts/lint-prose.sh --selftest` for the proof: it builds a probe file, shows misspell missing most of it, and shows the backstop catching all of it.

Markdown formatting and structure (`dprint check`, `.markdownlint.json`) run in CI's `prose` job and **do block merges** — `prose` is a required status check on `main`. It was report-only while the tree was still hard-wrapped; `dprint fmt` has since run over every markdown file, so the reason for the exemption is gone. `pnpm dlx dprint@0.57.4 fmt` fixes the formatting half automatically. Dependency vulnerability scanning (`govulncheck`, `pip-audit`, `pnpm audit`, `cargo audit`) runs in CI's `security` job and also does not block — a fresh advisory against a pinned dependency with no fix available yet should not stall every unrelated PR. Both jobs still report their findings on every run.

This repository is public, and two more jobs block on that being true. `scripts/lint-public.sh` rejects any pointer to the private core repository **by name**; `scripts/lint-paths.sh` rejects any citation of a repository path that is not here. Both are in the `public` job, and the fix for either is the same: say what a thing **is** — "the C ABI contract", with `include/chtypes.h` as its public authority — rather than where it lives. Both take `--selftest`, which proves the rules fire before you trust a clean run.

Run any of these locally with the same command CI uses; each job's step name in `.github/workflows/ci.yml` names the exact invocation.
