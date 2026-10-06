# Releasing the SDKs

Four packages, one repository, one tag convention: **the directory prefix is the tag prefix**, `go/v0.1.0`, `python/v0.1.0`, `ts/v0.1.0`, `rust/v0.1.0`. Go requires that form for a module in a subdirectory; the others follow it so every release is addressed the same way.

| package                         | registry                  | trigger     | workflow                              | what it needs once                                                                                                                                                                                                                                                                                                                                                         |
| ------------------------------- | ------------------------- | ----------- | ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `github.com/wave-rf/chtypes/go` | proxy.golang.org (by tag) | `go/v*`     | `release-go.yml` (build, vet, verify) | nothing                                                                                                                                                                                                                                                                                                                                                                    |
| `chtypes`                       | PyPI                      | `python/v*` | `release-python.yml`                  | a Trusted Publisher on PyPI — can be added BEFORE the project exists (a _pending_ publisher), so the first release is the tag like every other                                                                                                                                                                                                                             |
| `@wavehouse/chtypes`            | npm                       | `ts/v*`     | `release-ts.yml`                      | OIDC trusted publishing, no token. The first publish is by hand (`cd ts && pnpm build && pnpm publish --access public`) from a laptop logged in to the `wavehouse` scope, because npm only lets a trusted publisher be configured on a package that exists; then npmjs.com → package settings → Trusted Publisher → `Wave-RF/chtypes`, `release-ts.yml`, environment `npm` |
| `chtypes`                       | crates.io                 | `rust/v*`   | `release-rust.yml`                    | the first publish by hand (crates.io lets a Trusted Publisher be configured only on an existing crate), then the publisher: repo `Wave-RF/chtypes`, workflow `release-rust.yml`, environment `crates-io`                                                                                                                                                                   |

Each workflow refuses a tag whose version does not equal the manifest's, builds, and publishes with provenance where the registry supports it. No long-lived token is stored for PyPI or crates.io.

**And each one then installs what it just published.** The `verify` job of every release workflow runs `scripts/release-verify.sh <binding> registry <version>`: a clean-room install of the PUBLISHED package from its public registry, anonymous, retrying for registry lag. Then, in a clean directory with a clean cache, it checks four things. The binding's ABI fingerprint must equal `CHS_ABI_FINGERPRINT` in `include/chtypes.h`. Its CLI must list the production registry and fetch the newest line. And its public API must load that line, whose own `build_info` reports the same fingerprint. A dry run runs the same script in `local` mode against the package it just built (see the 1.x releases section below).

This is here because a publish step exiting 0 does not mean anyone can install the package. On `ts/v0.1.1` the job went green about seven minutes before npm served the tarball, and for two of those minutes `dist-tags.latest` resolved to a version that 404'd — a clean `npm install` failed while CI showed a green release (issue #10). A publish that never completes looks identical. Presence is not the test: the check installs and runs, so it also catches an artifact that resolves but does not work, and one whose ABI disagrees with the header.

It fetches anonymously on purpose. A registry can show a maintainer a version the public cannot see — `~/.npmrc` carrying a token is what made #10 take three wrong turns to diagnose — so the check is made with no credentials in scope.

## The 1.x releases

Releases are tagged **from `main`**. `v1` was merged into `main` on 2026-10-06 (#475), so `main` is the 1.0 tree and the default branch. 1.0.0 and 1.0.2 were tagged from `v1` before that merge; 1.0.3 and later come from `main`. The release workflows live on `main`, so a tag pushed at a `main` commit runs `main`'s copy of the workflow against `main`'s tree.

The order is the one below: **`rust/v1.0.3` → `ts/v1.0.3` → `python/v1.0.3` → `go/v1.0.3`** (for 1.0.3), one at a time, each green before the next. Nobody tags before every dry run is green.

**Dry runs, from the release branch (or `main`), before any tag.** Each release workflow takes a `workflow_dispatch` with `tag` (the tag NAME to check, nothing is tagged) and `dry_run`:

```sh
for x in rust ts python go; do
  gh workflow run "release-$x.yml" --ref <branch> -f "tag=$x/v1.0.3" -f dry_run=true
done
```

A dry run does everything the tag path does up to the publish, skips the publish, and then RUNS the post-publish verify steps against the package the dry run built (the wheel, the packed tarball, `cargo install --path`, the local Go module through a `replace`) instead of the registry. Only the registry-lag retry loop is skipped. The verify steps are one script, `scripts/release-verify.sh`, shared by the tag path and the dry run so they cannot drift: they assert the binding's ABI fingerprint, run that CLI's `chtypes list` against the production registry, parse the newest published two-part line from the `published <spelling> support unknown` rows (the only column read is the second), fetch it, load it through the public API and assert the loaded `abi_fingerprint`. The `--selftest` of that script pins the parser against the CLI's flat output format. Read every run's STEP conclusions, not just the run's: a run is green only when each step ran and passed, the verify steps included. The parser was once proved only after an irreversible publish (the first `rust/v1.0.0` verify failed on a stale parser), which is why a dry run now runs it.

**Verify only.** To re-run the post-publish check against a version that is already published, without publishing anything:

```sh
gh workflow run release-rust.yml --ref main -f tag=rust/v1.0.3 -f verify_only=true
```

`verify_only=true` skips the publish job entirely (it wins over `dry_run`), installs the version the `tag` input names from the public registry with the retry loop, and runs the same steps. A tag push is unchanged: publish, then verify against the registry.

**The User-Agent assertion.** The fetch layer sends `chtypes-<language>/<version>`, and the version is read at a different place in each binding, so each release workflow asserts that the version in the User-Agent equals the version being tagged, in the dry run AND on the tag path, before anything is published:

| workflow             | what it reads                                                                                                             |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `release-go.yml`     | `userAgent()` (from the `bindingVersion` constant), through a throwaway test run inside the package                       |
| `release-python.yml` | `chtypes._ocifetch._http.USER_AGENT` from the BUILT wheel, installed into a clean venv                                    |
| `release-ts.yml`     | `USER_AGENT` from `dist/ocifetch/http.js` in the unpacked `pnpm pack` tarball                                             |
| `release-rust.yml`   | the unit test pinning `user_agent()` to `CARGO_PKG_VERSION` (it must report one pass), and the built binary's `--version` |

A stale Go `bindingVersion` therefore fails the dry run, not a user. There is no ancestry check in any of the four workflows: they check that the tag name and the manifest agree, and nothing about which branch the tag sits on.

## Before the first tag of each package

1. Bump the manifest version (`python/pyproject.toml`, `ts/package.json`, `rust/Cargo.toml`; Go has none — the tag is the version — so bump `bindingVersion` in `go/internal/ocifetch/useragent.go` instead; it is the version in the fetch layer's `User-Agent`).
2. **Regenerate the lockfiles that carry the version** — the crate's own, the package's own, and the examples': `(cd rust && cargo update -p chtypes)`, `(cd python && uv lock)`, `(cd examples/rust && cargo update -p chtypes)` and `(cd examples/python && uv lock)`. `rust/Cargo.lock` records `chtypes`'s own version, so `cargo build --locked` fails the moment `Cargo.toml` is bumped without it. `python/uv.lock` records it too, the same way, so CI's `uv sync --locked --group dev` in `python/` fails the same way. CI runs the four tours with `--locked`, so a stale one fails the required `examples` job. This is deliberate: `examples/rust/Cargo.lock` sat at `0.1.0` through the whole `0.1.1` release, and `examples/python/uv.lock` missed `0.1.1` _and_ `0.1.2`, because nothing resolved them. (`examples/ts/pnpm-lock.yaml` needs nothing here, on purpose: it resolves `@wavehouse/chtypes` as `file:../../ts`, so pnpm's lockfile carries no version string for it to fall behind.)
3. `CHANGELOG` entry naming the ABI revision the release speaks (`4` today) and the ClickHouse lines the golden set was generated on.
4. Run that package's suite against a registry, and `scripts/check-standalone.sh` for Go.
5. Tag: `git tag go/v0.1.0 && git push origin go/v0.1.0`, etc.

The first tag freezes the Go module path; what stays provisional for the `chs_*` signatures until 1.0 is in [`docs/support-v1.md`](docs/support-v1.md#pre-10).

## Every release after the first: the order, and why Go is last

Push the four tags **one at a time**, and wait for each workflow to go green before pushing the next:

> **`rust` → `ts` → `python` → `go`**

**Rust first** because it has the most failure surface — a `--locked` build, a `--locked` publish and an OIDC exchange — so a problem stops the sequence while nothing is public yet.

⚠️ **Go last, and this is the one that matters.** A Go module publishes by **the tag existing on a public repository**. `release-go.yml` only _verifies_; it does not publish, and nothing gates it. The instant `git push origin go/v0.1.2` lands, proxy.golang.org can serve it, and it cannot be withdrawn. Every other registry has a workflow between the tag and the public artifact. Go has none, so it goes last, when the other three have already proved the release is good.

None of the four can be taken back: crates.io and PyPI refuse to reuse a version number, and npm the same. A release is a one-way door on all four — the order only decides how much you know before you walk through the last one.

⚠️ **Do not read `release-ts` or `release-rust` failing on an already-published version as broken publishing.** `error: crate chtypes@X already exists` and `[E403] You cannot publish over the previously published versions` are both reached _after_ authentication succeeds, so they are the check that the publishers are still configured. Read the error before reporting a problem.

npm can take about half an hour to serve a good publish (`ts/v0.4.0` measured ~26 minutes, with no human action anywhere in that window), so `release-ts.yml`'s `verify` job waits up to an hour before it gives up; if it still fails, re-run only the `verify` job — `publish` already succeeded and re-running it is refused by npm. The failure text says what it measured (whether the packument lists the version, what `dist-tags.latest` is, the tarball's HTTP status) before it guesses at a cause; on `0.4.0` the "may be staged for approval" guess it used to lead with was wrong — nothing was staged, npm was just slow.

## Registry setup, once each (the owner's console; nothing is stored here)

The repository has the deployment environments `pypi`, `npm` and `crates-io` — one per publishing workflow, named to match what each registry's trusted publisher is configured with — and no registry token exists anywhere: every publish authenticates by OIDC, with the one exception per registry noted below. Do these only once the repository is public — provenance links point at the source, and a package on a public registry whose source 404s is worse than no package.

**PyPI** — no manual publish needed. pypi.org → your account → _Publishing_ → _Add a new pending publisher_: PyPI project name `chtypes`, owner `Wave-RF`, repository `chtypes`, workflow `release-python.yml`, environment `pypi`. The first `python/v*` tag creates the project under that publisher. Afterwards add the org/team as a project owner so it is not tied to one account.

**crates.io** — one manual publish. `cargo login` on a laptop with a crates.io API token (mint it at crates.io → Account → API Tokens, scope `publish-new`, short expiry; it is used once and then deleted), then `cd rust && cargo publish`. Once the crate exists: crates.io → the crate → _Settings_ → _Trusted Publishing_ → GitHub, repository `Wave-RF/chtypes`, workflow `release-rust.yml`, environment `crates-io`. Every `rust/v*` tag after that is the workflow. Add a co-owner (`cargo owner --add github:wave-rf:<team>`) for the same reason as above.

**npm** — one manual publish, as the table says; then the trusted publisher on the package's settings page: repository `Wave-RF/chtypes`, workflow `release-ts.yml`, environment `npm`. `@wavehouse` is an org scope we own, so `--access public` is required on that first publish.

**Go** — nothing to configure. proxy.golang.org fetches a tag the first time anyone asks for it; it needs the repository to be public, and that is all. `go/v0.1.0` is the whole release.

## The order

1. The repository goes public (license and history already prepared).
2. `go/v0.1.0` — the tag alone.
3. `python/v0.1.0` — the tag, under the pending publisher.
4. The manual `pnpm publish` and `cargo publish`, then their trusted publishers, then `ts/v0.1.0` and `rust/v0.1.0` — those two tags re-run the same versions through the workflows and must be no-ops or refusals, which is the check that the publishers are configured (a workflow that cannot publish fails on the tag, loudly).
