<!-- BEGIN GENERATED — dist/support-table.py from index.json; do not edit by hand -->

# chtypes artifact support matrix

Rendered from `index.json` in the same publish step that writes it, so it can never drift from what is actually served. Language minimums and which ABI revision each SDK version speaks live in the SDK's own `docs/support.md` — this file covers only what `index.json` itself states.

`index.json` sha256: `a9a152be9e9caf67dc4eb34bb7203afa04ba87dd07644952930513b346a4df4f`

## Platforms

The artifact is native code, so a platform is supported only if the release publishes a build for it. Today that is:

- `darwin-arm64` — see [macOS artifacts are for development; Linux is the reference](https://github.com/Wave-RF/chtypes/blob/main/docs/limitations.md#macos-artifacts-are-for-development-linux-is-the-reference)
- `linux-amd64`
- `linux-arm64`

Both loaders are `dlopen`, so all four bindings are Unix-only. There is no Windows artifact and no 32-bit build.

## ClickHouse lines

One artifact per ClickHouse line, each carrying that release's own C++. A line is supported when it has passed the artifact producer's comparison against a real server and the release publishes it:

| Line | Platforms |
|---|---|
| `25.8` | all |

The exact ClickHouse patch each line is built from is in its artifact's `manifest.json` and in the served `index.json` (`clickhouse_version`); it moves with every upstream patch release, so it is not repeated here.
Ask for a line, never a nearest match: `for("25.8")` resolves the newest build of that line and fails if it is absent, rather than quietly handing back a neighbor whose answers differ.

## ABI revisions

An SDK build speaks exactly one ABI revision and refuses, at load, any artifact reporting a different one — naming both numbers. Revision columns below are exactly the `abi_revision` values this index actually carries; a row with no `abi_revision` at all — an artifact built before the field existed — is never guessed into one of them, and gets its own column instead.

| Line | Platform | Revision 6 | No revision recorded |
|---|---|---|---|
| `25.8` | `darwin-arm64` | `0` | not published |
| `25.8` | `linux-amd64` | `0` | not published |
| `25.8` | `linux-arm64` | `0` | not published |

A cell reads **not published** when the index carries no row for that (line, platform, revision) combination — a gap a consumer already on that revision cannot work around by pinning a different build, because the index carries no such build.

## glibc floor

The highest `GLIBC_*` symbol version the built library imports, measured off the artifact itself at build time and copied from its `manifest.json` — never a hand-maintained table. One row per (line, platform); the newest build's own measurement, never carried over from a different build.

| Line | Platform | glibc floor |
|---|---|---|
| `25.8` | `darwin-arm64` | n/a |
| `25.8` | `linux-amd64` | not recorded |
| `25.8` | `linux-arm64` | not recorded |

<!-- END GENERATED -->
