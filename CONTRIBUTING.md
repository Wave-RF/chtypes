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

A pull request enqueues itself to main's merge queue once every condition below holds — `.github/workflows/policy-merge.yml`, whose decision is entirely `scripts/policy-merge-check.py` (chtypes#280). An agent, or a maintainer, is needed only when a condition fails: a check is red, or the pull request touches a file in a protected class.

**The seven conditions.** ALL must hold:

1. the head is a branch of **this** repository — a fork's head never auto-enqueues;
2. every required check is green on the head, read by name;
3. the head has not moved since the checks ran;
4. GitHub reports no merge conflict, and no review conversation — no requested changes, no open review comment — exists;
5. no file the pull request touches (its current path, or for a rename its old path too; a deletion counts) matches a **protected glob**, below;
6. only when `docs/support.md` is part of the diff: it was modified (not added, deleted or renamed), and `scripts/support-matrix.sh`, run from main against the live served index, reproduces the head's copy byte for byte;
7. only when the pull request touches a **test or fixture path** (chtypes#285 §1b, below): no suite's executed-test count, and no golden-case count, fell below main's last green `ci` push run.

**Test counts (condition 7).** Once section 1 lets a non-API source change merge itself, a pull request could change code and also weaken the test that would have caught its bug, then pass its own checks — tests stay unprotected (a protected glob would let a pull request weaken its own judge just as surely as editing `.github/**` would). So a pull request that touches `go/**/*_test.go`, `python/tests/**`, `ts/test/**`, `rust/tests/**` or `tests/fixtures/**` carries one more condition: each suite's own required job (its no-artifact job, and its step inside the `artifacts` job) prints one fixed line to its own job console log — `chtypes-count suite=<label> ran=<n> skipped=<n>` — derived from the exact same summary line `scripts/check-suite.sh`/`scripts/check-standalone.sh` already parse for their own pass/fail verdict, never a second, hand-set count; the `artifacts` job additionally prints `chtypes-count golden-cases=<n>`, sourced from where the goldens are already counted for the zero-checked guard (chtypes#225). The checker reads this job's log and main's last green `ci` push run's same job's log (`GET .../actions/jobs/{id}/logs` — a check run's own `output.summary` is **not** populated for a GitHub-Actions-authored job, measured directly against a real run) and refuses on any per-suite drop or a golden-case-count drop, naming the suite and both numbers; a missing or unparseable count on either side counts as a drop, and a test that moved from ran to skipped is caught the same way, because that shift always drops the `ran` count by one. A pull request touching none of those paths skips this condition entirely, at zero extra API cost.

**It enqueues, it does not merge.** Once every condition holds, the workflow calls the `enqueuePullRequest` GraphQL mutation (`expectedHeadOid` = the judged head sha, so a moved head still refuses) rather than merging directly. The call is made by the merge-bot GitHub App from a separate `enqueue` job, which runs in the `merge-bot` environment (deployable from main only) and is the only reader of the App's credentials. An entry enqueued with the workflow's built-in token gets no `merge_group` run of `ci` and stalls (#291); one the App enqueues does. The judging job itself has read-only permissions. The merge queue itself then tests the actual combination — the pull request's changes merged with main's current tip — and merges only once that combination's checks pass; `ci.yml` triggers on `merge_group` as well as `pull_request` so those checks run. This is what makes "the tree that lands is the tree CI tested" true for every pull request, including ones enqueued moments apart, without this workflow re-reading main's tip and hoping nothing moved in between — see `.github/workflows/policy-merge.yml`'s own header for why a direct merge could not have guaranteed that on its own (`main`'s branch protection does not require a head to be up to date with main before merging). A protected-class pull request is merged by hand through the same queue. `gh pr merge` does not work here: with a merge queue required it falls back to enabling auto-merge, which this repository does not allow. Enqueue it with the same mutation the workflow uses, pinned to the head you reviewed:

```sh
gh api graphql -f query='mutation($id:ID!,$oid:GitObjectID!){enqueuePullRequest(input:{pullRequestId:$id,expectedHeadOid:$oid}){mergeQueueEntry{state position}}}' -f id=<the pull request's node_id> -f oid=<the reviewed head sha>
```

`gh api repos/Wave-RF/chtypes/pulls/<n> --jq .node_id` prints the node id.

**Draft is the hold switch.** Besides the six conditions, the pull request must be open, non-draft and based on main (`pull-request`, in the checker's terms), so marking it draft stops it from being enqueued. To stop the mechanism outright for every pull request: `gh workflow disable policy-merge`.

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
- `.markdownlint.json` — configures the required prose job's markdownlint rules
- `.markdownlint-cli2.jsonc` — configures the required prose job's markdownlint-cli2 file selection
- `dprint.json` — configures the required prose job's dprint formatting check
- `go/.golangci.yml` — configures the required lint-go job's golangci-lint rules
- `ts/biome.json` — configures the required lint-ts job's biome rules
- `ts/tsconfig.json` — configures the required ts job's build step (tsc -p tsconfig.json)
- `ts/tsconfig.test.json` — configures the required ts job's typecheck step (tsc -p tsconfig.test.json)
- `tests/parity/manifest.json` — the cross-binding parity contract each of the required go/python/ts/rust jobs' own parity test reads and enforces — declares what every binding must support
- `docs/divergences.json` — the machine-checkable register of known divergences the divergences job reads; an allowlist that excuses a result

<!-- END policy-merge protected globs -->

`go/**/*.go` (except `*_test.go`), `python/src/**`, `ts/src/**` and `rust/src/**` protect each binding's **whole** non-test source tree rather than a computed export list — Go has no export list, and Python/Rust export by visibility, not by anything a path rule can read. A follow-up could narrow this with committed API snapshots; none exist today. Tests, fixtures, examples, docs (`docs/support.md` alone excepted, as condition 6 above) and CHANGELOGs are deliberately **not** protected — the protected gates (condition 2) judge them, so nothing about loosening what they judge weakens the judge itself.

The `.markdownlint.json`/`.markdownlint-cli2.jsonc`/`dprint.json`/`go/.golangci.yml`/`ts/biome.json`/`ts/tsconfig*.json` entries configure a required check's own tool — the same "must not weaken its own judge" reasoning as `.github/**` and `scripts/**`, one level down at the tool-config layer. `tests/parity/manifest.json` and `docs/divergences.json` are data a check reads to reach its verdict rather than code: the parity manifest declares what every binding must support, and the divergences register is an allowlist that excuses a result, the same threat model as a lint exemption — protected even though the `divergences` job itself is non-blocking. This list was produced by a sweep of every `ci.yml` `run:` line for a tool's config flags and name, cross-checked against each tool's default config-file name in `git ls-files`; it found no config file for cargo clippy/fmt, shellcheck, actionlint or vitest, and confirmed `.nvmrc` is read by no workflow here (every `setup-node` step hardcodes `node-version: 22`), so none of those needed adding.

Signature/checksum verification code and each binding's embedded release public key already sit inside the globs above — confirmed by reading each: `go/chtypes/fetch_sign.go`, `python/src/chtypes/_ed25519.py`, `python/src/chtypes/fetch.py`, `ts/src/fetch.ts`, `rust/src/fetch/mod.rs`, `rust/src/fetch/release.rs`, `rust/src/fetch/trust.rs`. `scripts/lint-public.sh`'s one exemption (the literal `runner` segment in its local-path allowlist) lives inside the script itself, so `scripts/**` already covers it.
