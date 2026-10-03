# What chtypes supports

Three independent axes, and a combination works only if all three do: the **language** you call from, the **platform** you run on, and the **ClickHouse line** you want answers for.

The table below is **generated** from this tree's four manifests and from the release's own `index.json` — never typed by hand, because every number in it changes on a schedule this repository does not control. Regenerate it with `scripts/support-matrix.sh`.

<!-- BEGIN GENERATED — scripts/support-matrix.sh; do not edit by hand -->

## Languages

| Binding | Package | Requires | Loads the artifact with |
|---|---|---|---|
| Go | `github.com/wave-rf/chtypes/go` | Go 1.27+ | cgo + `dlopen` |
| Python | `chtypes` | Python 3.11+ | stdlib `ctypes` (no build step, no dependencies) |
| TypeScript | `@wavehouse/chtypes` | Node 22.21+, ESM only | `ffi-rs` (prebuilt) |
| Rust | `chtypes` | Rust 1.85+ (edition 2024) | `libloading` |

## Platforms

The artifact is native code, so a platform is supported only if the release publishes a build for it. Today that is:

- `darwin-arm64` — see [macOS artifacts are for development; Linux is the reference](limitations.md#macos-artifacts-are-for-development-linux-is-the-reference)
- `linux-amd64`
- `linux-arm64`

Both loaders are `dlopen`, so all four bindings are Unix-only. There is no Windows artifact and no 32-bit build.

## ClickHouse lines

One artifact per ClickHouse line, each carrying that release's own C++. A line is published once it has passed the artifact producer's comparison against a real server and the release includes it. Separately, chtypes supports a ClickHouse line exactly as long as upstream does — see [Served, unsupported ClickHouse lines](#served-unsupported-clickhouse-lines) below for what a line reads once upstream's own support for it ends.

Per-line platform coverage and which lines are currently supported are rendered by the artifact producer directly from `index.json`, in the same publish step that writes it and naming that index's own sha256 — so they can never drift from what this page would otherwise have to restate: the [served support table](https://artifacts.wavehouse.dev/artifacts/support-matrix.md).

The exact ClickHouse patch each line is built from is in its artifact's `manifest.json` and in the served `index.json` (`clickhouse_version`); it moves with every upstream patch release, so it is not repeated here.
Ask for a line, never a nearest match: `for("25.8")` resolves the newest build of that line and fails if it is absent, rather than quietly handing back a neighbor whose answers differ.

## ABI revisions

An SDK build speaks exactly one ABI revision and refuses, at load, any artifact reporting a different one — naming both numbers. **Both revisions can be served on the same rolling channel at once**, including during a cutover, so which artifact build an installed SDK version actually needs is not always "whatever `for()` resolves to" any more; the two tables below answer that.

### Which ABI revision an SDK version speaks

Read from this repository's own release tags and `include/chtypes.h` as it stood at each — not typed here. All four bindings tag the same version number together and were checked to agree on the revision at every tag that exists for all four.

| SDK version | Speaks ABI revision |
|---|---|
| `0.1.0` | 4 |
| `0.1.1` | 4 |
| `0.1.2` | 4 |
| `0.2.0` | 4 |
| `0.2.1` | 4 |
| `0.2.2` | 4 |
| `0.3.0` | 5 |
| `0.3.1` | 5 |
| `0.3.2` | 5 |
| `0.4.0` | 6 |
| `0.5.0` | 6 |
| `0.5.1` | 6 |
| `0.5.2` | 6 |

### Which artifact build satisfies each revision, per ClickHouse line and platform

Read from the live index: revision 4 is whichever revision the served `abi_revision` field never names (see above); every other revision listed is read directly off that field. A cell reads **not published** when the index carries no row at all for that (line, platform, revision) combination at a revision the index writes explicitly — a gap a consumer already on that revision cannot work around. A cell in the Revision 4 column reads **—** instead: the index carries no row there either, but some lines predate that baseline revision and some postdate it, and this script does not read build timestamps to guess which — it reports only that there is no such build, never a guess at what the build number would be or at why one is missing.

| Line | Platform | Revision 4 | Revision 5 | Revision 6 |
|---|---|---|---|---|
| `24.8` | `darwin-arm64` | `0` | *(linux only, by design)* | *(linux only, by design)* |
| `24.8` | `linux-amd64` | — | `1790460995` | `1790767905` |
| `24.8` | `linux-arm64` | — | `1790460995` | `1790767905` |
| `25.3` | `darwin-arm64` | — | `1790460995` | `1790767905` |
| `25.3` | `linux-amd64` | — | `1790460995` | `1790767905` |
| `25.3` | `linux-arm64` | — | `1790460995` | `1790767905` |
| `25.8` | `darwin-arm64` | `0` | `1790460995` | `1790767905` |
| `25.8` | `linux-amd64` | `0` | `1790460995` | `1790767905` |
| `25.8` | `linux-arm64` | `0` | `1790460995` | `1790767905` |
| `25.10` | `darwin-arm64` | — | `1790460995` | `1790767905` |
| `25.10` | `linux-amd64` | — | `1790460995` | `1790767905` |
| `25.10` | `linux-arm64` | — | `1790460995` | `1790767905` |
| `26.2` | `darwin-arm64` | — | `1790460995` | `1790767905` |
| `26.2` | `linux-amd64` | — | `1790460995` | `1790767905` |
| `26.2` | `linux-arm64` | — | `1790460995` | `1790767905` |
| `26.3` | `darwin-arm64` | — | `1790618314` | `1790870875` |
| `26.3` | `linux-amd64` | — | `1790618314` | `1790870875` |
| `26.3` | `linux-arm64` | — | `1790618314` | `1790870875` |
| `26.4` | `darwin-arm64` | — | `1790460995` | `1790767905` |
| `26.4` | `linux-amd64` | — | `1790460995` | `1790767905` |
| `26.4` | `linux-arm64` | — | `1790460995` | `1790767905` |
| `26.5` | `darwin-arm64` | `0` | `1790460995` | `1790767905` |
| `26.5` | `linux-amd64` | `0` | `1790460995` | `1790767905` |
| `26.5` | `linux-arm64` | `0` | `1790460995` | `1790767905` |
| `26.6` | `darwin-arm64` | `1789482654` | `1790460995` | `1790767905` |
| `26.6` | `linux-amd64` | `1789482654` | `1790460995` | `1790767905` |
| `26.6` | `linux-arm64` | `1789482654` | `1790460995` | `1790767905` |
| `26.7` | `darwin-arm64` | `1789830374` | `1790618314` | `1790870875` |
| `26.7` | `linux-amd64` | `1789830374` | `1790618314` | `1790870875` |
| `26.7` | `linux-arm64` | `1789830374` | `1790618314` | `1790870875` |
| `26.8` | `darwin-arm64` | `1790001762` | `1790632171` | `1790845279` |
| `26.8` | `linux-amd64` | `1790001762` | `1790632171` | `1790845279` |
| `26.8` | `linux-arm64` | `1790001762` | `1790632171` | `1790845279` |
| `26.9` | `darwin-arm64` | — | `1790632171` | `1790845279` |
| `26.9` | `linux-amd64` | — | `1790632171` | `1790845279` |
| `26.9` | `linux-arm64` | — | `1790632171` | `1790845279` |

Every line/platform pairing above either has a build for every revision the index writes explicitly, or is excluded by design (marked above), or reads **—** at revision 4, where the index carries no row for it at all and this script does not guess why (see above) — never merely "not yet published" without one of those reasons.

<!-- END GENERATED -->

## Why the ClickHouse list is not a promise about the future

A line appears above once it has passed the artifact producer's comparison against a real server and the release publishes the artifact. Lines are added as they pass it, so this page is a snapshot of a moving list — `curl -s https://artifacts.wavehouse.dev/artifacts/index.json` is always the live answer, and `scripts/fetch.sh --all` reads it rather than restating it.

Nothing is removed to make room. Since 2026-09-30 the release is append-only: every build it publishes stays listed in `index.json` and the signed `SHA256SUMS`, so a machine holding an older patch keeps working. Rows that the earlier retention rule dropped before that date are re-listed only where a release-signed `SHA256SUMS` proves their bytes; the rest stay unlisted.

## Served, unsupported ClickHouse lines

**chtypes supports a ClickHouse line exactly as long as upstream does.** When upstream's own support for a line ends, the artifact producer retires it: no new builds and no new ABI revisions, ever, for that line — but its existing served artifacts stay, append-only, exactly like every other build. The [served support table](https://artifacts.wavehouse.dev/artifacts/support-matrix.md) marks a retired line **served, unsupported** rather than removing it, and also states what it means when the served index's `supported_lines` key is absent (unknown, never unsupported) — this page does not restate either, since both are rendered from the same `index.json` in the same publish step and a second copy here could only drift from what is actually served.

A served, unsupported line keeps resolving and loading exactly as it does today: nothing about `for()`, fetch, or load-time ABI matching changes. What changes is scoped to the future, one ABI revision at a time. A retired line stays fetchable by every SDK release whose ABI revision it was built for — today that is revisions 5 and 6 — because the artifact producer never builds it for a revision it does not already have a build for. The first SDK release on a *later* ABI revision therefore cannot fetch that line at all: there is no build of it to find. That is intended, not a gap — it follows directly from supporting a line exactly as long as upstream does, since a future ABI revision will not include a line upstream had already retired.

## Checking from your own machine

```sh
scripts/fetch.sh --all          # install every published line for this host
chtypes list                    # what is installed, and what the release offers
chtypes verify                  # re-hash every installed line against its manifest
```

`chtypes list` and `chtypes verify` are spelled the same way in all four bindings (`go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest`, `python -m chtypes`, `npx @wavehouse/chtypes`, `cargo install chtypes`), and [`guides/fetch.md`](guides/fetch.md) §6 is the contract they share.

## What "supported" means for a golden answer

The public golden set is generated per line and gates itself on the **exact** patch version, not the line. A case runs against an artifact only when that artifact's exact version equals the one its expectations were produced on, and skips loudly by name otherwise — an expectation produced on one build says nothing about another.

This is also why the golden set shrinks as lines are added: a case the lines answer differently is refused by the generator rather than recorded twice. Version-dependent truth lives with the artifact producer, per line. **The SDK asserts a version-specific answer nowhere.**

## macOS artifacts are for development; Linux is the reference

The darwin artifacts exist so you can develop and run the suites on a laptop. They are not the reference for what a server does — see [Known limitations → macOS artifacts are for development; Linux is the reference](limitations.md#macos-artifacts-are-for-development-linux-is-the-reference) for why.

## Pre-1.0

Package names, the artifact name `libchtypes`, the `chs_` prefix and the `enum chs_format` numbers are frozen. A function's exact signature is not one of those things: pre-1.0, a signature change rides an ABI revision instead, and that revision is what a caller can actually rely on — an SDK refuses to load an artifact whose revision it does not speak, naming both numbers, and that refusal is shipped and run by every binding's own suite, not merely documented. See [`guides/fetch.md`](guides/fetch.md#1-where-artifacts-are-looked-for-the-registry-search-path) for where that match happens; what a given revision covers is maintained by the artifact producer. This tree speaks the ABI revision `CHS_ABI_REVISION` names in [`include/chtypes.h`](../include/chtypes.h) — currently 6, whose error-code table and partition key are the most recent signature change since the first tag; see [Which ABI revision an SDK version speaks](#which-abi-revision-an-sdk-version-speaks) above for the rest of the history. Anything else may still move before 1.0 — each binding's CHANGELOG carries its own list. If a refusal is the reason you are reading this, [ABI revisions](#abi-revisions) above maps your installed SDK version to the revision it speaks and the artifact build that satisfies it.
