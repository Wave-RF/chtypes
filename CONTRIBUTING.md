# Contributing

chtypes' SDKs are thin, honest bindings over one frozen C ABI (`include/chtypes.h`, `docs/reference/`). Most contributions are to a binding's ergonomics, its docs, or its golden cases; the type system itself is ClickHouse's, vendored in the native artifact, and is not reimplemented here — a PR that re-derives ClickHouse behavior in Go, Python, TypeScript or Rust will be declined however good it is.

- **Build and test locally.** `scripts/fetch.sh <clickhouse-line>` installs an artifact for this machine into the per-user cache; each binding's README says how to run its suite against it. Without one, every test that needs an artifact skips by name and the rest still runs — `scripts/check-suite.sh --no-artifacts <lang>` and `scripts/check-standalone.sh --no-artifacts` are exactly what CI runs first; it then runs the same suites with two published lines. The artifact-backed proof beyond that lives with the artifact producer. A registry path is not a fingerprint, so before either runs its suite it prints every loaded artifact line's identity (`clickhouse_version`, `chtypes_build`, `abi_revision`, `core_commit`, `library_sha256`) and warns if a line's ABI revision does not match `include/chtypes.h` — the per-user cache is not guaranteed to hold what the release currently serves.
- **Every binding follows the same contract** (`docs/reference/`). A change to what a call means belongs in the spec and in all four bindings, not one.
- **A change to when a library is opened is a change to the ABI-revision handshake.** The refusal in `docs/reference/artifact.md` step 5 is a load-time gate, so anything that moves, defers or removes a load can silence it without touching a line of refusal code — that is how it broke once, unnoticed. The two cases that prove it need no artifact: `scripts/abi-fixtures.sh --out <dir>` builds the fixture pair from `include/chtypes.h`, and with `CHTYPES_ABI_FIXTURES=<dir>` exported the ordinary no-artifact commands above run them in every binding and fail if either case did not run. CI does the same in its own `abi-fixtures` job, for all four bindings, so a required check will now tell you — but run it yourself before you push a change to load timing.
- **A PR that moves `CHS_ABI_REVISION` ahead of what is served is not expected-red.** `scripts/abi-channel.sh` derives the CI `artifacts` job's channel per run and points it at the artifact producer's signed candidate build for that revision instead of the (correctly refusing) served one — a CI-only mechanism, never a tag to fetch or install yourself.
- **A sync between long-lived branches is merged with a MERGE COMMIT, never a squash.** Everything else here is squash-merged, so this is the one place the habit is wrong. A squash produces a single-parent commit, so the branch gains the _content_ and none of the _ancestry_ — git then still believes every commit of the source branch is unmerged, and the next sync replays them all. That is not merely noisy: it silently re-applies wording a branch deliberately rejected. It happened once, and the next merge would have re-introduced a sentence about lazy loading into a consumer-facing guide that the branch had removed on purpose. For a sync, the ancestry **is** the deliverable.
- **Goldens are served, not tracked.** The golden set — a published file of expected answers every binding must reproduce — ships in the rolling release as `sdk-goldens.json` and installed beside the artifacts by `scripts/fetch.sh`; there is no cases file in this repository to edit. Open an issue here for a missing case.
- **Report security issues privately**: see `SECURITY.md`.

By contributing you agree your work is licensed under the Apache License 2.0 (`LICENSE`).

## Linting

Each binding has its own linter, configured to be strict but green on the current tree: `go/.golangci.yml` (golangci-lint), `ts/biome.json` (Biome), `[lints]` in `rust/Cargo.toml` (clippy, already run by CI's `rust` job), and `python/pyproject.toml`'s `[tool.ruff.lint]` section (already run by CI's `python` job). `scripts/lint-actions.sh` runs actionlint over `.github/workflows/*.yml` and shellcheck over every tracked `*.sh`.

`scripts/lint-spelling.sh` enforces American spelling over the whole tree — every tracked file except `**/fixtures/fetch/**` (generated and signed) and `LICENSE` (legal text), plus the script itself, whose wordlist is British spellings by necessity. There are no other exceptions.

It runs two independent checks, because one is not enough: `misspell -locale US`, and a grep backstop for a family of words misspell's matcher has been proven not to fire on reliably even when they are in its own dictionary. The word list lives in the script and nowhere else, so this page cannot drift from it. Run `scripts/lint-spelling.sh --selftest` for the proof: it builds a probe file, shows misspell missing most of it, and shows the backstop catching all of it.

Markdown formatting and structure (`dprint check`, `.markdownlint.json`) run in CI's `prose` job and **do block merges** — `prose` is a required status check on `main`. It was report-only while the tree was still hard-wrapped; `dprint fmt` has since run over every markdown file, so the reason for the exemption is gone. `pnpm dlx dprint@0.57.4 fmt` fixes the formatting half automatically. Dependency vulnerability scanning (`govulncheck`, `pip-audit`, `pnpm audit`, `cargo audit`) runs in CI's `security` job and also does not block — a fresh advisory against a pinned dependency with no fix available yet should not stall every unrelated PR. Both jobs still report their findings on every run.

This repository is public, and two more jobs block on that being true. `scripts/lint-public.sh` rejects any pointer to the artifact producer's private repository **by name**; `scripts/lint-cited-paths.sh` rejects any citation of a repository path that is not here. Both are in the `public` job, and the fix for either is the same: say what a thing **is** — "the C ABI contract", with `include/chtypes.h` as its public authority — rather than where it lives. Both take `--selftest`, which proves the rules fire before you trust a clean run.

Run any of these locally with the same command CI uses; each job's step name in `.github/workflows/ci.yml` names the exact invocation.

## Policy merge

A pull request merges itself once every condition below holds — `.github/workflows/policy-merge.yml`, whose decision is entirely `scripts/policy-merge-check.py` (chtypes#280). An agent, or a maintainer, is needed only when a condition fails: a check is red, or the pull request touches a file in a protected class.

**The six conditions.** ALL must hold:

1. the head is a branch of **this** repository — a fork's head never auto-merges;
2. every required check is green on the head, read by name;
3. the head has not moved since the checks ran;
4. GitHub reports no merge conflict, and no review conversation — no requested changes, no open review comment — exists;
5. no file the pull request touches (its current path, or for a rename its old path too; a deletion counts) matches a **protected glob**, below;
6. only when `docs/support.md` is part of the diff: it was modified (not added, deleted or renamed), and `scripts/support-matrix.sh`, run from main against the live served index, reproduces the head's copy byte for byte.

**Draft is the hold switch.** Mark a pull request draft and this stops merging it — condition 2 above (`pull-request`, in the checker's terms). To stop the mechanism outright for every pull request: `gh workflow disable policy-merge`.

**Protected globs.** Conservative by design: over-protect rather than under-protect. `--print-protected` prints this same list from the one constant (`PROTECTED_GLOBS` in `scripts/policy-merge-check.py`) that decides it; the block below is generated from it and `scripts/policy-merge-check.py --check-guide` fails if the two drift apart.

<!-- BEGIN policy-merge protected globs (generated by `scripts/policy-merge-check.py --print-protected`; do not hand-edit between the markers) -->

- `.github/**` — the workflows define and run the required checks; a pull_request run of ci uses the pull request's own copy
- `scripts/**` — every gate a required check runs, this checker itself, and scripts/fetch.sh, which verifies release signatures
- `include/**` — the frozen C ABI header
- `go/**/*.go` (except `*_test.go`) — a binding's public API surface; Go has no export list to compute a narrower rule from, so v1 protects all non-test Go source
- `python/src/**` — a binding's public API surface; Python exports by visibility, not a list this checker can read, so v1 protects all of it
- `ts/src/**` — a binding's public API surface; same reasoning as python/src/** — no export list to read
- `rust/src/**` — a binding's public API surface; same reasoning as python/src/** — no export list to read
- `go/go.mod` — a release input: the Go module's own manifest
- `go/go.sum` — a release input: the Go module's dependency lockfile (not yet present in this tree; protected in advance of needing one)
- `python/pyproject.toml` — a release input: the Python package manifest
- `python/uv.lock` — a release input: the Python dependency lockfile
- `ts/package.json` — a release input: the npm package manifest
- `ts/pnpm-lock.yaml` — a release input: the npm dependency lockfile
- `ts/pnpm-workspace.yaml` — a release input: the pnpm workspace manifest — not in issue #280's own list, found via `git ls-files` per this checker's own brief
- `rust/Cargo.toml` — a release input: the crate manifest
- `rust/Cargo.lock` — a release input: the crate dependency lockfile
- `RELEASING.md` — the release procedure itself

<!-- END policy-merge protected globs -->

`go/**/*.go` (except `*_test.go`), `python/src/**`, `ts/src/**` and `rust/src/**` protect each binding's **whole** non-test source tree rather than a computed export list — Go has no export list, and Python/Rust export by visibility, not by anything a path rule can read. A follow-up could narrow this with committed API snapshots; none exist today. Tests, fixtures, examples, docs (`docs/support.md` alone excepted, as condition 6 above) and CHANGELOGs are deliberately **not** protected — the protected gates (condition 2) judge them, so nothing about loosening what they judge weakens the judge itself.

Signature/checksum verification code and each binding's embedded release public key already sit inside the globs above — confirmed by reading each: `go/chtypes/fetch_sign.go`, `python/src/chtypes/_ed25519.py`, `python/src/chtypes/fetch.py`, `ts/src/fetch.ts`, `rust/src/fetch/mod.rs`, `rust/src/fetch/release.rs`, `rust/src/fetch/trust.rs`. `scripts/lint-public.sh`'s one exemption (the literal `runner` segment in its local-path allowlist) lives inside the script itself, so `scripts/**` already covers it.
