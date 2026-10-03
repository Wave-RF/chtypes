# Contributing

chtypes' SDKs are thin, honest bindings over one frozen C ABI (`include/chtypes.h`, `docs/reference/`). Most contributions are to a binding's ergonomics, its docs, or its golden cases; the type system itself is ClickHouse's, vendored in the native artifact, and is not reimplemented here — a PR that re-derives ClickHouse behavior in Go, Python, TypeScript or Rust will be declined however good it is.

- **Build and test locally.** `scripts/fetch.sh <clickhouse-line>` installs an artifact for this machine into the per-user cache; each binding's README says how to run its suite against it. Without one, every test that needs an artifact skips by name and the rest still runs — `scripts/check-suite.sh --no-artifacts <lang>` and `scripts/check-standalone.sh --no-artifacts` are exactly what CI runs first; it then runs the same suites with two published lines. The artifact-backed proof beyond that lives with the artifact producer. A registry path is not a fingerprint, so before either runs its suite it prints every loaded artifact line's identity (`clickhouse_version`, `chtypes_build`, `abi_revision`, `core_commit`, `library_sha256`) and warns if a line's ABI revision does not match `include/chtypes.h` — the per-user cache is not guaranteed to hold what the release currently serves.
- **Every binding follows the same contract** (`docs/reference/`). A change to what a call means belongs in the spec and in all four bindings, not one.
- **A change to when a library is opened is a change to the loader's handshake.** The refusals in `docs/reference/abi-v1.md` are load-time gates, so anything that moves, defers or removes a load can silence one without touching a line of refusal code — that is how the v0 revision refusal broke once, unnoticed. The cases that prove them need no artifact: `scripts/abi-v1/build-stubs.sh --out <dir>` builds a stub library per refusal from the description, and each binding's conformance runner drives its loader against them in the `v1-abi-conformance` CI jobs, which `v1-abi-parity` reads, so a required check will tell you — but run your binding's runner yourself before you push a change to load timing.
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

## Changelog entries

Most CHANGELOG bullets describe themselves. One has a fixed shape, because its criterion lives elsewhere and must not drift into a second, competing description: an **enforcement-gate lift** on the filter surface (`docs/limitations.md` → [Filters are for comparison, not enforcement, for now](docs/limitations.md#filters-are-for-comparison-not-enforcement-for-now)).

When a `(ClickHouse line, platform)` pair meets that page's lift criterion, announce it as its own bullet under an existing `### Changed` heading in each affected binding's CHANGELOG — never a new `### Changed` heading for it, which would fail markdownlint's MD024 — in this shape:

- **Enforcement gate lifted** for `<line>` on `<platform>`.

That bullet is the only record of a lift: nothing elsewhere — a comment, an issue, a dashboard — lifts the gate. A `(line, platform)` pair stays under the gate until its own CHANGELOG entry, in this shape, says otherwise, and a pair that diverges again after being lifted gets a fresh entry the same way.

## Policy merge

A pull request enqueues itself to main's merge queue once every condition below holds — `.github/workflows/policy-merge.yml`, whose decision is entirely `scripts/policy-merge-check.py` (chtypes#280). An agent, or a maintainer, is needed only when a condition fails: a check is red, the pull request touches a file in a protected class, or it changes a binding's exported API.

**The seven conditions.** ALL must hold:

1. the head is a branch of **this** repository — a fork's head never auto-enqueues;
2. every required check is green on the head, read by name;
3. the head has not moved since the checks ran;
4. GitHub reports no merge conflict, and no review conversation — no requested changes, no open review comment — exists;
5. no file the pull request touches (its current path, or for a rename its old path too; a deletion counts) matches a **protected glob**, below — and for a binding's source tree, which is protected only conditionally, that binding's **API verdict** on the head is `changed=false` (chtypes#285 §1, below); and for one ecosystem's manifest and lockfile, also protected only conditionally, this is a proven **dependabot patch-level dev-dependency bump** of exactly that ecosystem (chtypes#285 §2, below); and for `tests/fixtures/fetch/**`, also protected only conditionally, the head's tree there is **byte-identical to the served, release-signed set** (chtypes#344, below);
6. only when `docs/support.md` is part of the diff: it was modified (not added, deleted or renamed), and `scripts/support-matrix.sh`, run from main against the live served index over a scratch copy seeded with the **head's own** bytes (never main's checked-out copy, chtypes#316), reproduces the head's copy byte for byte;
7. only when the pull request touches a **test or fixture path** (chtypes#285 §1b, below): no suite's executed-test count, and no golden-case count, fell below main's last green `ci` push run.

**Binding source and the API verdict (condition 5).** A pull request that touches a binding's source but changes none of its exported API merges itself once its checks pass. CI's non-blocking `api-surface` job (`scripts/api-surface.py`) compares the pull request's merge base with main — never main's tip — against its head, for each binding whose source the diff touches, with one pinned tool per binding, each pinned in that script and nowhere else:

- Go: `golang.org/x/exp/cmd/apidiff` at `v0.0.0-20260908205506-85c1c2202aba`, in module mode, over both the default build and `-tags chtypes_linked`;
- Python: griffe `2.3.0`, through `uv run --with`. `griffe check` alone reports only breaking changes, so the job compares griffe's own model of the public API instead: every public object, with its kind, signature, bases, annotation and module-level value;
- TypeScript: `@microsoft/api-extractor` `7.59.3`, in report mode, base report against head report, forgotten exports included;
- Rust: `cargo-public-api` `0.52.0` on `nightly-2026-05-20`, with `--all-features`. `cargo-semver-checks` was not used, because it reports semver violations and an addition is not one.

An exported **addition** counts as a change, the same as a removal or a signature change. Doc comments and unexported code do not. Before any verdict, every tool must pass a fixture proof on `tests/fixtures/api-surface/`: an unexported-only change reads unchanged, and an addition, a removal and a signature change each read changed. The job prints one line per touched binding to its log, `chtypes-api-surface binding=<b> head=<sha> changed=<true|false|error>`, and the checker reads it as it reads test counts. The job stays green for any genuine verdict, so an intended API change is not shown red, only left for a manual merge. It goes red only when a tool fails, and a red or skipped job's log is never read. **A missing, unparseable or errored verdict counts as changed, never unchanged.** A binding the diff does not touch needs no verdict. Nothing from the judged head runs in that job; its script's header says how.

**The security carve-out.** Signature and checksum verification code, the trust policy and the embedded release key stay protected **whatever** the API verdict says: an unchanged API says nothing about whether a verification still verifies. The carve-out entries are in the generated list below. They were derived by grepping each binding for its verification primitives and trust anchors, and the required `abi` job runs `scripts/policy-merge-check.py --check-carve-out` to derive them again on every pull request. That check fails if a binding file holding verification code is not in the carve-out, or if a carve-out entry no longer covers a file.

**Dependency bumps (condition 5, chtypes#285 §2).** A patch-level bump of a dev or test-only dependency merges itself; a runtime dependency, a manifest's own version field, and GitHub Actions bumps (`.github/**` stays unconditionally protected) all stay manual. Classified from dependabot's own commit metadata, read as data: the checker reads `updated-dependencies:` from the head commit's own message through the API, never from the pull request body. The narrowing applies only when **all** of these hold:

- the pull request's author is `dependabot[bot]` — an identity GitHub assigns only to a pull request its own Dependabot installation opens, which neither a fork nor an arbitrary collaborator can trigger on request. **This proves only who opened the pull request**, not who wrote every commit on its branch afterward: a force-push by anyone with push access could replace the head commit with one carrying a forged trailer without changing this field, so two more checks close that gap —
- the **head commit's own** `author.login` is `dependabot[bot]`, its `committer.login` is `web-flow`, and its signature is verified — trusting only the GitHub-identity `.login` fields, never the git-level author/committer name or email strings a forged commit can set to anything (measured against this repository's own real dependabot commit `84401fd`);
- **every** `HEAD_REF_FORCE_PUSHED_EVENT` on the pull request's own timeline has an actor of GraphQL's own shape for the bot — `("Bot", "dependabot")`, **not** REST's `"dependabot[bot]"` — read via GraphQL, since the REST API carries no force-push timeline (measured against this repository's own PR #249: GraphQL spells the same bot differently from REST, so comparing a GraphQL actor to the REST spelling would refuse dependabot's own routine rebases). More events than the read fetched is always a refusal, never a pass — an unseen one could be anyone's. **This check is unit-tested only:** no pull request here has had a non-empty force-push timeline yet, so the live query has only ever returned an empty list. The live negative proof is #336;
- the pull request carries exactly one commit, with exactly one parent, and every dependency its metadata names is both `direct:development` and `version-update:semver-patch`;
- the diff touches nothing **outside** one ecosystem's manifest and lockfile — a non-empty subset of that pair, not necessarily both: a patch bump that stays inside the manifest's own version range moves only the lockfile (measured against this repository's own history, commit `84401fd`);
- the manifest diff — the head commit against its own parent, never against main's possibly-unrelated tip — changes only the named dependencies' version specifiers: the manifest's own version field is unchanged, no runtime (production) dependency changed at all, no dev dependency was added or removed, and no dev dependency outside the named set changed. Both the pre-bump and the head manifest are parsed with Python's own `tomllib`/`json` — a declarative data format read as data, never executed, sourced or evaluated as code, the same posture `docs/support.md`'s bytes already get.

A touched **unconditional** protected glob (`.github/**`, `scripts/**`, …) refuses regardless of any of the above: the unconditional check runs before the dependabot-ecosystem one, so no dependabot proof, however complete, can excuse a diff that also touches one.

**Go has no dev/runtime dependency split**, and this tree has zero external Go dependencies today (no `require` block in `go/go.mod`, so `go/go.sum` does not exist yet either) — there is no case to narrow, and proving a module is test-only would need `go list -deps -test` against `go list -deps` run on main's own tree, a heuristic this checker does not implement. A Go dependency bump stays manual.

**Served fixtures (condition 5, chtypes#344).** `tests/fixtures/fetch/` carries its own outcomes: `expected.json` says what every binding's fetch suite must do with each refusal case. A diff that edits a refusal case and its `expected.json` entry together could turn "fetch refuses a tampered tarball" into "fetch accepts it" in all four suites at once, with the same test count and no verification code touched. The set is generated by the artifact producer and served as `sdk-fetch-fixtures.tar.gz`, a row in the rolling release's signed `SHA256SUMS`, so a re-import of it merges itself and anything else waits for a human. A pull request touching `tests/fixtures/fetch/**` is excused only when **all** of these hold, and fails closed on every error:

- main's own `scripts/fetch.sh --release-file sdk-fetch-fixtures.tar.gz`, run with every `CHTYPES_*` variable removed from its environment, fetches the asset from its default source and verifies `SHA256SUMS.sig` under the release key embedded in main's `fetch.sh` (key id `deb275922dbff76e`), never the fixture test key in `tests/fixtures/fetch/test-key/`, which a pull request could edit. It then verifies the asset's sha256 against that signed `SHA256SUMS`. A non-zero exit for any reason (no network, an HTTP error, a bad or foreign signature, a sha256 mismatch) refuses, and so does a success line that does not name the release key;
- the head's whole tree under `tests/fixtures/fetch/`, read from one `git/trees/<head>?recursive=1` call (paths, modes and git blob ids, never a checkout), equals the tarball's members file for file: no file missing, no file extra, every git blob id (a hash of the bytes) and every mode the same. A truncated listing refuses. `abi-revision/` is left out of the served side because this repository does not track it (the retired v0 fixture job fetched that generator from the release itself), so a head that carries anything there is an extra file and refuses;
- nothing else in the diff is unconditionally protected: that check runs first, and no fixture proof can excuse it.

A fixture tree that passes still answers to the test counts (condition 7). `tests/fixtures/api-surface/` has no served source: it is hand-written test data for the `api-surface` job's fixture proof, which runs on the pull request's own copy of it before any API verdict is trusted, so it is protected unconditionally.

**Test counts (condition 7).** Once section 1 lets a non-API source change merge itself, a pull request could change code and also weaken the test that would have caught its bug, then pass its own checks — tests stay unprotected (a protected glob would let a pull request weaken its own judge just as surely as editing `.github/**` would). So a pull request that touches `go/**/*_test.go`, `python/tests/**`, `ts/test/**`, `rust/tests/**` or `tests/fixtures/**` carries one more condition: each suite's own required job (its no-artifact job, and its step inside the `artifacts` job) prints one fixed line to its own job console log — `chtypes-count suite=<label> ran=<n> skipped=<n>` — derived from the exact same summary line `scripts/check-suite.sh`/`scripts/check-standalone.sh` already parse for their own pass/fail verdict, never a second, hand-set count; the `artifacts` job additionally prints `chtypes-count golden-cases=<n>`, sourced from where the goldens are already counted for the zero-checked guard (chtypes#225). The checker reads this job's log and main's last green `ci` push run's same job's log (`GET .../actions/jobs/{id}/logs` — a check run's own `output.summary` is **not** populated for a GitHub-Actions-authored job, measured directly against a real run) and refuses on any per-suite drop or a golden-case-count drop, naming the suite and both numbers; a missing or unparseable count on either side counts as a drop, and a test that moved from ran to skipped is caught the same way, because that shift always drops the `ran` count by one. A pull request touching none of those paths skips this condition entirely, at zero extra API cost.

**It enqueues, it does not merge.** Once every condition holds, the workflow calls the `enqueuePullRequest` GraphQL mutation (`expectedHeadOid` = the judged head sha, so a moved head still refuses) rather than merging directly. The call is made by the merge-bot GitHub App from a separate `enqueue` job, which runs in the `merge-bot` environment (deployable from main only) and is the only reader of the App's credentials. An entry enqueued with the workflow's built-in token gets no `merge_group` run of `ci` and stalls (#291); one the App enqueues does. The judging job itself has read-only permissions. The merge queue itself then tests the actual combination — the pull request's changes merged with main's current tip — and merges only once that combination's checks pass; `ci.yml` triggers on `merge_group` as well as `pull_request` so those checks run. This is what makes "the tree that lands is the tree CI tested" true for every pull request, including ones enqueued moments apart, without this workflow re-reading main's tip and hoping nothing moved in between — see `.github/workflows/policy-merge.yml`'s own header for why a direct merge could not have guaranteed that on its own (`main`'s branch protection does not require a head to be up to date with main before merging). A protected-class pull request is merged by hand through the same queue. `gh pr merge` does not work here: with a merge queue required it falls back to enabling auto-merge, which this repository does not allow. Enqueue it with the same mutation the workflow uses, pinned to the head you reviewed:

```sh
gh api graphql -f query='mutation($id:ID!,$oid:GitObjectID!){enqueuePullRequest(input:{pullRequestId:$id,expectedHeadOid:$oid}){mergeQueueEntry{state position}}}' -f id=<the pull request's node_id> -f oid=<the reviewed head sha>
```

`gh api repos/Wave-RF/chtypes/pulls/<n> --jq .node_id` prints the node id.

**Draft is the hold switch.** Besides the seven conditions, the pull request must be open, non-draft and based on main (`pull-request`, in the checker's terms), so marking it draft stops it from being enqueued. To stop the mechanism outright for every pull request: `gh workflow disable policy-merge`.

**Closing a pull request's own linked issues is a separate workflow.** GitHub's keyword auto-close does not fire for a merge whose actor is the merge-bot App, so `.github/workflows/close-linked-issues.yml` runs on every push to main and closes each issue the merged pull request's own `closingIssuesReferences` names, with a comment pointing back at it (chtypes#362).

**Protected globs.** Conservative by design: over-protect rather than under-protect. `--print-protected` prints this same list from the one constant (`PROTECTED_GLOBS` in `scripts/policy-merge-check.py`) that decides it; the block below is generated from it and `scripts/policy-merge-check.py --check-guide` fails if the two drift apart.

<!-- BEGIN policy-merge protected globs (generated by `scripts/policy-merge-check.py --print-protected`; do not hand-edit between the markers) -->

- `.github/**` — the workflows define and run the required checks; a pull_request run of ci uses the pull request's own copy
- `scripts/**` — every gate a required check runs, this checker itself, and scripts/fetch.sh, which verifies release signatures
- `include/**` — the frozen C ABI header
- `spec/**` — the v1 fetch layer's generated-constants source, JSON schemas and binding enrollment markers — the frozen interface every v1 fetcher builds against
- `go/**/*.go` (except `*_test.go`) — go binding source (the exported API is computed by apidiff); protected only while the `api-surface` verdict for `go` on the head is not `changed=false`
- `python/src/**` — python binding source (the public API is computed from griffe's model); protected only while the `api-surface` verdict for `python` on the head is not `changed=false`
- `ts/src/**` — ts binding source (the exported API is computed by api-extractor); protected only while the `api-surface` verdict for `ts` on the head is not `changed=false`
- `rust/src/**` — rust binding source (the public API is computed by cargo-public-api); protected only while the `api-surface` verdict for `rust` on the head is not `changed=false`
- `go/chtypes/fetch_sign.go` — the embedded release public key and the ed25519 signature check; security carve-out, protected whatever the API verdict
- `go/chtypes/fetch.go` — the fetch chain: the signature-check call, every tarball's sha256 against SHA256SUMS, the lock pin, the trusted-keys and allow-unsigned options; security carve-out, protected whatever the API verdict
- `go/chtypes/registry_path.go` — names the CHTYPES_TRUSTED_KEYS and CHTYPES_ALLOW_UNSIGNED variables the trust policy reads; security carve-out, protected whatever the API verdict
- `go/chtypes/multiversion.go` — load-time verification: the library_bytes size check on every load and the WithVerifyChecksums sha256 re-hash; security carve-out, protected whatever the API verdict
- `go/chtypes/resolve.go` — exact-patch resolution's load path, which runs the same load-time verification; security carve-out, protected whatever the API verdict
- `python/src/chtypes/_ed25519.py` — the ed25519 signature check itself; security carve-out, protected whatever the API verdict
- `python/src/chtypes/fetch.py` — the embedded release public key, the trust policy, and the fetch chain's signature and sha256 checks; security carve-out, protected whatever the API verdict
- `python/src/chtypes/_manifest.py` — load-time verification: check_library_bytes and verify_library's sha256 re-hash; security carve-out, protected whatever the API verdict
- `python/src/chtypes/registry.py` — calls the load-time verification on every load (verify_hashes); security carve-out, protected whatever the API verdict
- `ts/src/fetch.ts` — the embedded release public keys, the trust policy, and the fetch chain's signature and sha256 checks; security carve-out, protected whatever the API verdict
- `ts/src/registry.ts` — load-time verification: checkLibraryBytes on every load and the verifyChecksums sha256 re-hash; security carve-out, protected whatever the API verdict
- `rust/src/fetch/**` — the embedded release public key, the trust policy, the signature check, the sha256 checks and the lock pin; security carve-out, protected whatever the API verdict
- `rust/src/digest.rs` — the sha256 helper every checksum check hashes with; security carve-out, protected whatever the API verdict
- `rust/src/registry.rs` — load-time verification: the library_bytes size check and the verify_checksums sha256 re-hash; security carve-out, protected whatever the API verdict
- `go/internal/ocifetch/**` — the v1 fetch layer: generated constants (the embedded release key) plus the trust, resolve and byte-verification code built on them; security carve-out, protected whatever the API verdict
- `python/src/chtypes/_ocifetch/**` — the v1 fetch layer: generated constants (the embedded release key) plus the trust, resolve and byte-verification code built on them; security carve-out, protected whatever the API verdict
- `ts/src/ocifetch/**` — the v1 fetch layer: generated constants (the embedded release key) plus the trust, resolve and byte-verification code built on them; security carve-out, protected whatever the API verdict
- `rust/src/ocifetch/**` — the v1 fetch layer: generated constants (the embedded release key) plus the trust, resolve and byte-verification code built on them; security carve-out, protected whatever the API verdict
- `rust/build.rs` — a build script cargo runs on every build, consumers' and the api-surface job's alike (none exists today)
- `go/go.mod` — a release input: the Go module's own manifest
- `go/go.sum` — a release input: the Go module's dependency lockfile (not yet present in this tree; protected in advance of needing one)
- `python/pyproject.toml` — a release input: the Python package manifest; protected only while this is not a proven dependabot patch-level dev-dependency bump of python
- `python/uv.lock` — a release input: the Python dependency lockfile; protected only while this is not a proven dependabot patch-level dev-dependency bump of python
- `ts/package.json` — a release input: the npm package manifest; protected only while this is not a proven dependabot patch-level dev-dependency bump of ts
- `ts/pnpm-lock.yaml` — a release input: the npm dependency lockfile; protected only while this is not a proven dependabot patch-level dev-dependency bump of ts
- `ts/pnpm-workspace.yaml` — a release input: the pnpm workspace manifest — not in issue #280's own list, found via `git ls-files` per this checker's own brief
- `rust/Cargo.toml` — a release input: the crate manifest; protected only while this is not a proven dependabot patch-level dev-dependency bump of rust
- `rust/Cargo.lock` — a release input: the crate dependency lockfile; protected only while this is not a proven dependabot patch-level dev-dependency bump of rust
- `RELEASING.md` — the release procedure itself
- `.markdownlint.json` — configures the required prose job's markdownlint rules
- `.markdownlint-cli2.jsonc` — configures the required prose job's markdownlint-cli2 file selection
- `dprint.json` — configures the required prose job's dprint formatting check
- `go/.golangci.yml` — configures the required lint-go job's golangci-lint rules
- `ts/biome.json` — configures the required lint-ts job's biome rules
- `ts/tsconfig.json` — configures the required ts job's build step (tsc -p tsconfig.json)
- `ts/tsconfig.test.json` — configures the required ts job's typecheck step (tsc -p tsconfig.test.json)
- `tests/parity/manifest.json` — the cross-binding parity contract each of the required go/python/ts/rust jobs' own parity test reads and enforces — declares what every binding must support
- `tests/fixtures/fetch/**` — the fetch fixtures every binding's fetch suite asserts: the test key, the refusal cases, and expected.json, the outcome each case must produce; protected unless the head's tree under it is byte-identical to a fresh extraction of the served `sdk-fetch-fixtures.tar.gz` (`abi-revision/` excepted, which this repository does not track), verified under the release key
- `tests/fixtures/fetch-v1/**` — the v1 conformance fixtures: the test and other-key signing keys, the route trees and OCI layouts, the scripted HTTP responses, and cases.json's outcome for each
- `tests/fixtures/api-surface/**` — the api-surface job's fixture proof: the packages each pinned API tool must read as changed or unchanged before any verdict is trusted
- `docs/divergences.json` — the machine-checkable register of known divergences the divergences job reads; an allowlist that excuses a result
- `**/.cargo/**` — cargo configuration (source replacement, rustflags) the required rust job would read
- `**/rust-toolchain*` — selects the Rust toolchain the required rust job resolves
- `**/.npmrc` — npm registry and token configuration the required ts job would read
- `**/go.work*` — a Go workspace file that redirects module resolution in the required go job
- `**/.python-version` — selects the Python interpreter the required python job resolves
- `**/pip.conf` — pip index and source configuration a Python install would read
- `**/.yarnrc*` — yarn registry configuration a Node install would read
- `**/bunfig.toml` — bun registry configuration a Node install would read

<!-- END policy-merge protected globs -->

`go/**/*.go` (except `*_test.go`), `python/src/**`, `ts/src/**` and `rust/src/**` are each binding's source tree, protected only while that binding's API verdict is not `changed=false` (above): no path rule can read an export list, so the export list is computed instead. The carve-out entries inside them, and `rust/build.rs`, are protected unconditionally; cargo runs a crate's build script on every build, including the `api-surface` job's own. `python/pyproject.toml`/`python/uv.lock`, `ts/package.json`/`ts/pnpm-lock.yaml` and `rust/Cargo.toml`/`rust/Cargo.lock` are each protected only while that ecosystem's manifest/lockfile pair is not a proven dependabot patch-level dev-dependency bump (above, chtypes#285 §2); `go/go.mod`/`go/go.sum` and `ts/pnpm-workspace.yaml` stay unconditionally protected — Go has no dev/runtime split to narrow, and the pnpm workspace manifest is not a dependency manifest dependabot bumps. `tests/fixtures/fetch/**` is protected unless the head's tree there is byte-identical to the served set, and `tests/fixtures/api-surface/**` unconditionally (both above, chtypes#344): each is fixture data that decides an outcome by itself, the fetch suites' expected verdicts and the API judge's own proof. Tests, examples, docs (`docs/support.md` alone excepted, as condition 6 above) and CHANGELOGs are deliberately **not** protected — the protected gates (condition 2) judge them, and the test counts (condition 7) catch a test removed or turned into a skip.

The `.markdownlint.json`/`.markdownlint-cli2.jsonc`/`dprint.json`/`go/.golangci.yml`/`ts/biome.json`/`ts/tsconfig*.json` entries configure a required check's own tool — the same "must not weaken its own judge" reasoning as `.github/**` and `scripts/**`, one level down at the tool-config layer. `tests/parity/manifest.json` and `docs/divergences.json` are data a check reads to reach its verdict rather than code: the parity manifest declares what every binding must support, and the divergences register is an allowlist that excuses a result, the same threat model as a lint exemption — protected even though the `divergences` job itself is non-blocking. This list was produced by a sweep of every `ci.yml` `run:` line for a tool's config flags and name, cross-checked against each tool's default config-file name in `git ls-files`; it found no config file for cargo clippy/fmt, shellcheck, actionlint or vitest, and confirmed `.nvmrc` is read by no workflow here (every `setup-node` step hardcodes `node-version: 22`), so none of those needed adding.

The carve-out covers both halves of verification in every binding: the fetch chain (the ed25519 signature over `SHA256SUMS`, each tarball's sha256, the lock pin, the trusted keys and the allow-unsigned switch) and the load-time checks (the `library_bytes` size check on every load and the opt-in sha256 re-hash). `scripts/lint-public.sh`'s one exemption (the literal `runner` segment in its local-path allowlist) lives inside the script itself, so `scripts/**` already covers it.

## Reading the `artifacts` job's result without a token

The `artifacts` job (`.github/workflows/ci.yml`) is the one place this repository proves a channel (the served release, or the artifact producer's `abi<N>-candidate`) was actually tested — but its job LOG is not readable by an unauthenticated caller: `.../actions/jobs/<id>/logs` returns 403 ("Must have admin rights to Repository."), measured 2026-09-30. Everything that log would otherwise be the only record of is instead emitted as a check-run **annotation** (chtypes#320), which a plain `curl` or a signed-out `gh api` CAN read: one `::notice title=chtypes artifact provenance::<line>` naming the channel this run tested, one more for each `provenance: …` line `scripts/lib/provenance.py` already prints (go/python/ts/rust each get their own, with the artifact line's `clickhouse_version`, `chtypes_build`, `abi_revision`, `core_commit` and `library_sha256`), and — never silence — a `::warning title=chtypes artifact provenance::unknown — <which>` for any of those that could not be determined (a suite that never ran because the fetch failed, a channel `scripts/abi-channel.sh` could not decide).

Two reads answer "did this ref's `artifacts` job pass, and what exactly did it test", both without an `Authorization` header:

```sh
# 1. the job's own conclusion, by its check-run name, for a given commit sha
gh api --method GET "repos/Wave-RF/chtypes/commits/<sha>/check-runs" \
  -f "check_name=artifacts — published lines, linux-amd64, every suite runs the golden set" \
  --jq '.check_runs[] | {id, status, conclusion}'

# 2. that check run's own annotations — the channel it tested, and every provenance line
gh api "repos/Wave-RF/chtypes/check-runs/<id>/annotations" \
  --jq '.[] | select(.title == "chtypes artifact provenance") | .message'
```

`--method GET` matters on the first call: `gh api` defaults to `POST` once you pass it a `-f` parameter, unless told otherwise. Both calls work exactly the same signed out of `gh` entirely, or as a bare `curl` with no `-H` at all — confirmed directly against a real `artifacts` run on `main` (2026-09-30): the `check-runs` and `check-runs/<id>/annotations` reads both returned HTTP 200 with no credential of any kind, while that same job's `.../actions/jobs/<id>/logs` returned HTTP 403 on the identical, credential-less request. A reader that sees only `status`/`conclusion` and these annotations never needs the log at all.
