# Releasing the SDKs

Four packages, one repository, one tag convention: **the directory prefix
is the tag prefix**, `go/v0.1.0`, `python/v0.1.0`, `ts/v0.1.0`,
`rust/v0.1.0`. Go requires that form for a module in a subdirectory; the
others follow it so every release is addressed the same way.

| package | registry | trigger | workflow | what it needs once |
|---|---|---|---|---|
| `github.com/wave-rf/chtypes/go` | proxy.golang.org (by tag) | `go/v*` | `release-go.yml` (build + vet only) | nothing |
| `chtypes` | PyPI | `python/v*` | `release-python.yml` | a Trusted Publisher on PyPI: repo `Wave-RF/chtypes`, workflow `release-python.yml`, environment `pypi` |
| `@wavehouse/chtypes` | npm | `ts/v*` | `release-ts.yml` | OIDC trusted publishing, no token. The first publish is by hand (`cd ts && pnpm build && pnpm publish --access public`) from a laptop logged in to the `wavehouse` scope, because npm only lets a trusted publisher be configured on a package that exists; then npmjs.com → package settings → Trusted Publisher → `Wave-RF/chtypes`, `release-ts.yml` |
| `chtypes` | crates.io | `rust/v*` | `release-rust.yml` | a Trusted Publisher on crates.io for the crate |

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
