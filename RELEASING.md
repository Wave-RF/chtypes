# Releasing the SDKs

Four packages, one repository, one tag convention: **the directory prefix is the tag prefix**, `go/v0.1.0`, `python/v0.1.0`, `ts/v0.1.0`, `rust/v0.1.0`. Go requires that form for a module in a subdirectory; the others follow it so every release is addressed the same way.

| package                         | registry                  | trigger     | workflow                              | what it needs once                                                                                                                                                                                                                                                                                                                                                         |
| ------------------------------- | ------------------------- | ----------- | ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `github.com/wave-rf/chtypes/go` | proxy.golang.org (by tag) | `go/v*`     | `release-go.yml` (build, vet, verify) | nothing                                                                                                                                                                                                                                                                                                                                                                    |
| `chtypes`                       | PyPI                      | `python/v*` | `release-python.yml`                  | a Trusted Publisher on PyPI — can be added BEFORE the project exists (a _pending_ publisher), so the first release is the tag like every other                                                                                                                                                                                                                             |
| `@wavehouse/chtypes`            | npm                       | `ts/v*`     | `release-ts.yml`                      | OIDC trusted publishing, no token. The first publish is by hand (`cd ts && pnpm build && pnpm publish --access public`) from a laptop logged in to the `wavehouse` scope, because npm only lets a trusted publisher be configured on a package that exists; then npmjs.com → package settings → Trusted Publisher → `Wave-RF/chtypes`, `release-ts.yml`, environment `npm` |
| `chtypes`                       | crates.io                 | `rust/v*`   | `release-rust.yml`                    | the first publish by hand (crates.io lets a Trusted Publisher be configured only on an existing crate), then the publisher: repo `Wave-RF/chtypes`, workflow `release-rust.yml`, environment `crates-io`                                                                                                                                                                   |

Each workflow refuses a tag whose version does not equal the manifest's, builds, and publishes with provenance where the registry supports it. No long-lived token is stored for PyPI or crates.io.

**This copy is the `v2` branch's.** Its four workflows release ABI v2 in one of two modes, and the tag alone selects which: the pre-releases `2.0.0-dev.N` ([Dev pre-releases](#dev-pre-releases-200-devn)) and, once the maintainer approves the lock, the stable `2.N.N` ([Stable releases](#stable-releases-2nn)). They refuse every other tag before anything is built. 1.x is released from `main`, by `main`'s copy of each workflow and of `scripts/release-verify.sh`, which is what the next three paragraphs and [The 1.x releases](#the-1x-releases) describe.

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

## Dev pre-releases (2.0.0-dev.N)

Until ABI v2 locks, the `v2` branch releases the four bindings only as pre-releases, `2.0.0-dev.N`, which no package manager installs by default (public issue #511 has the plan and the lock conditions). Each one speaks the UNSTABLE generation-2 fingerprint in `include/v2/chtypes.h`, fetches only from the staging dev repository `https://registry-staging.wavehouse.dev/chtypes/v2-dev`, and trusts only the staging key (key id `824345f9bcf8e5bf`), never production and never the release key: rule r6 of `spec/abi-v2/docs.md`. Nothing on this branch publishes a 1.x, and nothing here publishes to production.

**One definition.** [`scripts/release-channel.sh`](scripts/release-channel.sh) is the only place the two modes are written down (THE MODES there): the version rule, the npm dist-tag, the registry, the key, the cache subroot, the header and the Go module path. Every release workflow on this branch runs its selftest and then `scripts/release-channel.sh tag <binding> <tag>` as its first step, before any toolchain is installed, and refuses any tag that is neither `<binding>/v2.0.0-dev.N` nor `<binding>/v2.N.N`, with a message naming the rule. `scripts/release-verify.sh` reads the same file. The Python release's PEP 440 mapping is [`scripts/release-pep440.py`](scripts/release-pep440.py).

**The tags, and the order.** The same order as 1.x, for the same reasons, one at a time, each workflow green before the next tag, and every dry run green first. The tags sit on a `v2` commit:

> **`rust/v2.0.0-dev.N` → `ts/v2.0.0-dev.N` → `python/v2.0.0-dev.N` → `go/v2.0.0-dev.N`**

```sh
for x in rust ts python go; do
  gh workflow run "release-$x.yml" --ref v2 -f "tag=$x/v2.0.0-dev.N" -f dry_run=true
done
```

Before the tags, the version is bumped as [below](#before-the-first-tag-of-each-package), with one difference: `python/pyproject.toml` spells it the PEP 440 way, `2.0.0.devN`, and the other three `2.0.0-dev.N` (Go's `bindingVersion` included).

**What each registry does with a pre-release.**

| registry  | the version                                                  | what a plain install gets                                                                                                         | how a user asks for the dev build                          |
| --------- | ------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| npm       | `2.0.0-dev.N`, published with `--tag dev`                    | `latest`, which a publish under another dist-tag never moves: the 1.x                                                             | `npm install @wavehouse/chtypes@dev`, or the exact version |
| PyPI      | `2.0.0.devN`, a PEP 440 pre-release                          | pip and uv skip a pre-release unless given `--pre` or an exact pin: the 1.x                                                       | `pip install chtypes==2.0.0.devN`                          |
| crates.io | `2.0.0-dev.N`, a semver pre-release                          | Cargo never resolves a pre-release for a requirement that does not name one, and `cargo add` picks the newest stable: the 1.x     | `cargo add chtypes@2.0.0-dev.N`                            |
| Go        | `go/v2.0.0-dev.N`, module `github.com/wave-rf/chtypes/go/v2` | a v2 version exists only under the `/v2` module path, a different module: `go get github.com/wave-rf/chtypes/go` stays on the 1.x | `go get github.com/wave-rf/chtypes/go/v2@v2.0.0-dev.N`     |

The `/v2` module has no stable release, so `go get github.com/wave-rf/chtypes/go/v2@latest` resolves the newest dev build; that asks for the v2 module by name, which is the point of the path.

**"Never installed by default" is asserted, never assumed.** Each workflow records what its registry installs by default just before the publish (it must be a stable 1.x, or nothing is published), and a `never-default` job asserts it again after the publish, on every read while it polls for the registry's lag. A dry run runs the same job against the registry as it is now, as the assertions' twin. `scripts/release-channel.sh --selftest` plants every refusal.

| workflow             | before the publish                                                                                                                                                                             | after the publish                                                                                                                          | the dry run's twin                                                                                                 |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------ |
| `release-ts.yml`     | the publish is `pnpm publish --tag dev` (the dry run `pnpm publish --dry-run --tag dev`); `dist-tags.latest` is recorded                                                                       | `dist-tags.latest` is still the recorded 1.x, and `dist-tags.dev` is the version                                                           | `latest` is the recorded 1.x; the version is not published; the same check finds the recorded version              |
| `release-python.yml` | `packaging.version` maps the tag to the manifest (`2.0.0-dev.N` is `2.0.0.devN`), which must be canonical and a pre-release; the distributions carry that name; a plain resolution is recorded | a plain, unpinned `uv pip compile chtypes` (no `--pre`) still picks the recorded 1.x, and `chtypes==2.0.0.devN` resolves                   | the plain resolution is the recorded 1.x; `==2.0.0.devN` does not resolve; `==<recorded>` does                     |
| `release-rust.yml`   | `max_stable_version` is recorded                                                                                                                                                               | `max_stable_version` is unchanged, a plain `cargo add chtypes` in a clean project locks the recorded 1.x, and the version's page is served | the same two reads give the recorded 1.x; the version is not published; the recorded version's page is served      |
| `release-go.yml`     | the first step refuses unless `go/go.mod` declares `github.com/wave-rf/chtypes/go/v2`; `go list -m github.com/wave-rf/chtypes/go@latest` is recorded                                           | `@latest` of the default module path is still the recorded 1.x, and `github.com/wave-rf/chtypes/go/v2@v2.0.0-dev.N` resolves               | `@latest` is the recorded 1.x; the `/v2` module does not list the version; the default path lists the recorded one |

The Go twin reads the version list rather than the version, so a dry run never asks the proxy for a tag that does not exist yet.

**The post-publish verify, on the dev channel.** `scripts/release-verify.sh` (the `verify` job, and a dry run's local install) runs six checks and reports each as PASS, FAIL or NOT RUN, failing unless all six pass: the binding's generation-2 fingerprint constant equals `include/v2/chtypes.h`'s; `chtypes where` names the `v2-dev` cache subroot (rule r5); every line `chtypes list` prints is a tag the staging dev repository serves, read independently from that repository's own `tags/list`; the platform manifest `chtypes fetch` installed (its cache directory is named by that manifest's digest) is the one the staging dev repository serves for that line; the install's `verified.json` is record schema 2, signed by the staging key, for abi 2 and the dev fingerprint; and the public API loads the line, whose own `build_info` reports abi 2 and the dev fingerprint. A verify that ran against the production v1 registry and "passed" is the failure this exists to refuse, so checks 3 to 6 assert the registry from outside the binding, and nothing is fetched when check 3 fails. A binding with no dev build yet, or a staging repository with no dev build in it, is red here by design: read which checks failed, never the run's color.

**Staging and the dev key are the only targets.** No dev binding takes a registry or a trust list from its caller or the environment (rule r6), and the verify unsets every override before it runs. These workflows publish only to the four package registries above; the libraries the dev bindings fetch reach the staging dev repository from the artifact producer, never from these workflows.

**The lock.** When #511's conditions hold, nothing in these workflows changes: a `2.N.N` tag already selects the stable mode below, and the maintainer's repository variable is what lets it publish. SDK `2.0.0` then ships from production, signed with the release key.

## Stable releases (2.N.N)

The stable mode releases SDK `2.0.0` and every later `2.N.N` (public issue #597). It is ready before the lock, so the maintainer's approval can publish in minutes, and until he sets one repository variable it can only dry-run.

**The tag selects the mode, and nothing else does.** No workflow input, variable or file sets it; `release_mode` in `scripts/release-channel.sh` is the one derivation, and `scripts/release-channel.sh mode <binding> <tag>` prints it:

| tag                                                                      | mode     | what it is                                                        |
| ------------------------------------------------------------------------ | -------- | ----------------------------------------------------------------- |
| `<binding>/v2.0.0-dev.N`                                                 | `dev`    | a pre-release, never the default install (the section above)      |
| `<binding>/v2.N.N` (`v2.0.0`; PEP 440 `2.0.0`)                           | `stable` | the default install once published                                |
| `<binding>/v1.x.y`                                                       | refused  | 1.x is tagged from `main`, never from this branch                 |
| anything else (`-rc.1`, `-dev` with no number, `+build`, a leading zero) | refused  | not a release; the message names the two shapes this branch takes |

**The gate: a stable release publishes only with the maintainer's fingerprint.** A stable release that is not a dry run (a tag push, or a dispatch with `dry_run=false`) refuses in its first step, before anything is built, unless the repository variable **`CHTYPES_V2_LOCKED_FP`** equals the fingerprint the binding is built against, and `spec/abi-v2/abi.json` says `"stability": "locked"`. The variable is one for all four bindings, because they speak one header. Its value is the locked `CHS_ABI_FINGERPRINT` of `include/v2/chtypes.h`, spelled as that header spells it (`sha256:` and 64 lowercase hex). Each workflow compares it with the binding's OWN generated constant (`go/internal/abi2/abi_gen.go`, `python/src/chtypes/_abi2/_decls.py`, `ts/src/abi2/decls.gen.ts`, `rust/src/abi2/decls.rs`), never with a value typed in a workflow. **Only the maintainer sets it**, when he approves the lock (`gh variable set CHTYPES_V2_LOCKED_FP --repo Wave-RF/chtypes --body sha256:<the locked fingerprint>`); nothing in this repository and no agent sets it. Without it, or when it names another fingerprint, the stable mode can only dry-run, so no session can tag or publish 2.0.0 by accident. A dry run always passes the gate, because it publishes nothing, and the dev mode has no gate. Go publishes by the tag existing, so for Go the gate cannot stop the publish: it turns that run red, loudly, which is one more reason Go goes last. `scripts/release-channel.sh --selftest` proves each row: the variable unset, refused; on another fingerprint, refused; on this fingerprint with the spec locked, allowed; on this fingerprint with the spec not locked, refused; and a dry run, allowed to the dry run's end whatever the variable says.

**Verify reads the production channel.** In the stable mode `scripts/release-verify.sh` runs the same six checks against the production generation-2 channel every binding carries (`docs/guides/fetch-v1.md`, "Generation 2 after the lock"): the cache subroot `v2`, record schema 2, abi 2, the production repository `https://registry.wavehouse.dev/chtypes/v2`, the release key (key id `deb275922dbff76e`), and no alias step. None of those values is typed in a workflow or in the script: they are `prod_v2` and the release key of `spec/fetch-v1/constants.json`, the definition `scripts/fetch-v1/gen-constants.py` generates into all four bindings. The production repository caches its tags and `tags/list` for 300 s, so the listing check retries for 360 s before it fails (the dev mode still reads once).

**The stand-in, for dry runs only.** Production `chtypes/v2` holds nothing until the lock, so a fetch there answers `CHTYPES_ARTIFACT_UNPUBLISHED`, and a stable dry run's verify could prove nothing against it. A stable dry run therefore verifies against a stand-in (`scripts/release-verify.sh <binding> standin`): the production channel's base and trust point at the staging dev repository `https://registry-staging.wavehouse.dev/chtypes/v2-dev` and the staging key, through that channel's own overrides, `CHTYPES_ARTIFACTS_URL` and `CHTYPES_TRUSTED_KEYS`. The production channel honors them, and the dev channel ignores them, so a CLI that still speaks the dev channel fails check 2 (its cache root is `v2-dev`, not `v2`). Everything else stays the production channel's. The workflows select the stand-in only for a stable dry run, and the script refuses it unless `RELEASE_DRY_RUN=true` and the run is not a tag push: a real release is verified against production and nothing else. It exists only in dry runs because its whole content is a substitution of registry and key, which a release must never make.

- **Before the lock**, no non-test binary of this repository speaks the production channel: each binding reaches it only through its test-only selector (`UseProdV2ForTests`, `use_prod_v2_for_tests`, `useProdV2ForTests`, `use_prod_v2_for_tests`). So the stand-in builds the binding's own test binary around its CLI and its public API (a Go test binary of `go/cmd/chtypes`, a pytest run, a vitest run with `ts/`'s own lockfile, the crate's bin and lib test binaries), selects the production channel there, and runs every check through it. The harnesses are written by the script, built and deleted; none is committed.
- **Once the spec is locked**, the shipped CLI speaks the production channel by default, and the stand-in runs it as is.
- The production channel has no alias step, so the stand-in fetches each line's own tag from the staging dev repository. It passes only while that tag serves a build of the tree's fingerprint; otherwise check 5 names both fingerprints.

**"Never by default" flips.** The `never-default` job keeps its name and, in the stable mode, asserts the opposite: once published, 2.N.N IS what a plain install resolves. `scripts/release-channel.sh default-check` holds both modes, and its selftest runs each against recorded registry answers (one passing and one failing record per registry), because a dry run publishes nothing and so cannot exercise the live check.

| workflow             | before the publish (recorded)                                               | after the publish (stable)                                                                                            | the dry run's twin (stable)                                                                                         |
| -------------------- | --------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `release-ts.yml`     | `dist-tags.latest` (the 1.x); the publish is `--tag latest`                 | the version is published and `dist-tags.latest` is it                                                                 | `latest` is the recorded 1.x; the version is not published; the same check finds the recorded version               |
| `release-python.yml` | a plain, unpinned `uv pip compile chtypes` (the 1.x)                        | `chtypes==2.N.N` resolves, and the plain resolution picks it                                                          | the plain resolution is the recorded 1.x; `==2.N.N` does not resolve; `==<recorded>` does                           |
| `release-rust.yml`   | `max_stable_version` (the 1.x)                                              | the version's page is served, `max_stable_version` is it, and a plain `cargo add chtypes` in a clean project locks it | both reads give the recorded 1.x; the version is not published; the recorded version's page is served               |
| `release-go.yml`     | `go list -m github.com/wave-rf/chtypes/go/v2@latest` (the newest dev build) | `github.com/wave-rf/chtypes/go/v2@v2.N.N` resolves, and `@latest` of the `/v2` path is it                             | `@latest` of `/v2` is the recorded dev build; the `/v2` module does not list the version; it lists the recorded one |

On every read after the publish the default must be the recorded version or the new one, never a third, and each workflow polls for its registry's lag (npm up to an hour, the Go proxy 20 minutes, PyPI and crates.io 15 minutes) before it fails.

**Go.** The module path is `github.com/wave-rf/chtypes/go/v2` in both modes (`go/go.mod` must declare it, or the first step refuses), so the stable tag is `go/v2.N.N` and its default install is `go get github.com/wave-rf/chtypes/go/v2`, `@latest` of that path. A plain `go get github.com/wave-rf/chtypes/go` keeps resolving the 1.x, as Go's major-version rule requires.

**The order.** The same as every release: dry runs first, then **`rust/v2.0.0` → `ts/v2.0.0` → `python/v2.0.0` → `go/v2.0.0`**, one at a time, each workflow green before the next tag, from the lock's merge commit and only after the maintainer has set `CHTYPES_V2_LOCKED_FP`:

```sh
for x in rust ts python go; do
  gh workflow run "release-$x.yml" --ref <the lock candidate> -f "tag=$x/v2.0.0" -f dry_run=true
done
```

The manifests must say `2.0.0` (and Go's `bindingVersion`) for the version agreement to pass, as for every release.

**The support page after a publish.** [`docs/support-v2.md`](docs/support-v2.md) is generated from the registry the Python binding's active generation-2 channel names (the staging dev repository today, production `chtypes/v2` with the release key after the lock's one-line `active()` switch), every signed statement verified before a fact is read from it, no layer downloaded. Nothing regenerates it automatically and no check turns red when the delivery side publishes: when a production publish notice arrives, open the regeneration pull request with

```sh
uv run scripts/support-v2/gen.py --write
```

Exit status 0 means the page is complete, 3 means it was written with some rows omitted (each is named on stderr: read them before committing), and 4 means nothing was written. A pull request that touches only `docs/support-v2.md` is enqueued by the policy merge once `checks` passes; on a pull request that touches the page or the generator, `checks` reruns `gen.py --check` against the registry and fails unless the page equals the output byte for byte.

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

**The publish environments are tag-only, and dry runs never enter them.** In `release-ts.yml`, `release-rust.yml` and `release-python.yml` the job that carries `environment:` (`npm`, `crates-io`, `pypi`) runs only for a real publish: a tag push, or a dispatch with `dry_run=false` that is not `verify_only`. Every dry-run step (checks, build, the `--dry-run` rehearsal, the packed artifact) runs in a `build` job with no environment. This matters because trusted publishing (OIDC) trusts the environment NAME, not a stored secret: any run that enters the environment can mint a publish credential. So each environment is to carry a tag-only deployment policy (`*/v*`), applied once this split is in place: with dry runs inside the environment jobs, that policy would block every dry run, since a dry run is dispatched from a branch.

**PyPI** — no manual publish needed. pypi.org → your account → _Publishing_ → _Add a new pending publisher_: PyPI project name `chtypes`, owner `Wave-RF`, repository `chtypes`, workflow `release-python.yml`, environment `pypi`. The first `python/v*` tag creates the project under that publisher. Afterwards add the org/team as a project owner so it is not tied to one account.

**crates.io** — one manual publish. `cargo login` on a laptop with a crates.io API token (mint it at crates.io → Account → API Tokens, scope `publish-new`, short expiry; it is used once and then deleted), then `cd rust && cargo publish`. Once the crate exists: crates.io → the crate → _Settings_ → _Trusted Publishing_ → GitHub, repository `Wave-RF/chtypes`, workflow `release-rust.yml`, environment `crates-io`. Every `rust/v*` tag after that is the workflow. Add a co-owner (`cargo owner --add github:wave-rf:<team>`) for the same reason as above.

**npm** — one manual publish, as the table says; then the trusted publisher on the package's settings page: repository `Wave-RF/chtypes`, workflow `release-ts.yml`, environment `npm`. `@wavehouse` is an org scope we own, so `--access public` is required on that first publish.

**Go** — nothing to configure. proxy.golang.org fetches a tag the first time anyone asks for it; it needs the repository to be public, and that is all. `go/v0.1.0` is the whole release.

## The order

1. The repository goes public (license and history already prepared).
2. `go/v0.1.0` — the tag alone.
3. `python/v0.1.0` — the tag, under the pending publisher.
4. The manual `pnpm publish` and `cargo publish`, then their trusted publishers, then `ts/v0.1.0` and `rust/v0.1.0` — those two tags re-run the same versions through the workflows and must be no-ops or refusals, which is the check that the publishers are configured (a workflow that cannot publish fails on the tag, loudly).
