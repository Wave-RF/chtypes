# Releasing the SDKs

Four packages, one repository, one tag convention: **the directory prefix
is the tag prefix**, `go/v0.1.0`, `python/v0.1.0`, `ts/v0.1.0`,
`rust/v0.1.0`. Go requires that form for a module in a subdirectory; the
others follow it so every release is addressed the same way.

| package | registry | trigger | workflow | what it needs once |
|---|---|---|---|---|
| `github.com/wave-rf/chtypes/go` | proxy.golang.org (by tag) | `go/v*` | `release-go.yml` (build + vet only) | nothing |
| `chtypes` | PyPI | `python/v*` | `release-python.yml` | a Trusted Publisher on PyPI — can be added BEFORE the project exists (a *pending* publisher), so the first release is the tag like every other |
| `@wavehouse/chtypes` | npm | `ts/v*` | `release-ts.yml` | OIDC trusted publishing, no token. The first publish is by hand (`cd ts && pnpm build && pnpm publish --access public`) from a laptop logged in to the `wavehouse` scope, because npm only lets a trusted publisher be configured on a package that exists; then npmjs.com → package settings → Trusted Publisher → `Wave-RF/chtypes`, `release-ts.yml`, environment `npm` |
| `chtypes` | crates.io | `rust/v*` | `release-rust.yml` | the first publish by hand (crates.io lets a Trusted Publisher be configured only on an existing crate), then the publisher: repo `Wave-RF/chtypes`, workflow `release-rust.yml`, environment `crates-io` |

Each workflow refuses a tag whose version does not equal the manifest's,
builds, and publishes with provenance where the registry supports it. No
long-lived token is stored for PyPI or crates.io.

## Before the first tag of each package

1. Bump the manifest version (`python/pyproject.toml`, `ts/package.json`,
   `rust/Cargo.toml`; Go has none — the tag is the version).
2. `CHANGELOG` entry naming the ABI revision the release speaks (`4` today)
   and the ClickHouse lines the golden set was generated on.
3. Run that package's suite against a registry, and
   `scripts/check-standalone.sh` for Go.
4. Tag: `git tag go/v0.1.0 && git push origin go/v0.1.0`, etc.

The first tag freezes the `chs_*` signatures and the Go module path.

## Registry setup, once each (the owner's console; nothing is stored here)

The repository has the deployment environments `pypi`, `npm` and `crates-io`
— one per publishing workflow, named to match what each registry's trusted
publisher is configured with — and no registry token exists anywhere: every
publish authenticates by OIDC, with the one exception per registry noted
below. Do these only once the repository is public — provenance links point
at the source, and a package on a public registry whose source 404s is worse
than no package.

**PyPI** — no manual publish needed. pypi.org → your account → *Publishing* →
*Add a new pending publisher*: PyPI project name `chtypes`, owner `Wave-RF`,
repository `chtypes`, workflow `release-python.yml`, environment `pypi`. The
first `python/v*` tag creates the project under that publisher. Afterwards add
the org/team as a project owner so it is not tied to one account.

**crates.io** — one manual publish. `cargo login` on a laptop with a
crates.io API token (mint it at crates.io → Account → API Tokens, scope
`publish-new`, short expiry; it is used once and then deleted), then
`cd rust && cargo publish`. Once the crate exists: crates.io → the crate →
*Settings* → *Trusted Publishing* → GitHub, repository `Wave-RF/chtypes`,
workflow `release-rust.yml`, environment `crates-io`. Every `rust/v*` tag after
that is the workflow. Add a co-owner (`cargo owner --add github:wave-rf:<team>`)
for the same reason as above.

**npm** — one manual publish, as the table says; then the trusted publisher on
the package's settings page: repository `Wave-RF/chtypes`, workflow
`release-ts.yml`, environment `npm`. `@wavehouse` is an org scope we own, so
`--access public` is required on that first publish.

**Go** — nothing to configure. proxy.golang.org fetches a tag the first time
anyone asks for it; it needs the repository to be public, and that is all.
`go/v0.1.0` is the whole release.

## The order

1. The repository goes public (licence and history already prepared).
2. `go/v0.1.0` — the tag alone.
3. `python/v0.1.0` — the tag, under the pending publisher.
4. The manual `pnpm publish` and `cargo publish`, then their trusted
   publishers, then `ts/v0.1.0` and `rust/v0.1.0` — those two tags re-run
   the same versions through the workflows and must be no-ops or refusals,
   which is the check that the publishers are configured (a workflow that
   cannot publish fails on the tag, loudly).
