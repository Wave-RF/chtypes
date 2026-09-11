# What chtypes supports

Three independent axes, and a combination works only if all three do: the **language** you call from, the **platform** you run on, and the **ClickHouse line** you want answers for.

The table below is **generated** from this tree's four manifests and from the release's own `index.json` — never typed by hand, because every number in it changes on a schedule this repository does not control. Regenerate it with `scripts/support-matrix.sh`.

<!-- BEGIN GENERATED — scripts/support-matrix.sh; do not edit by hand -->

## Languages

| Binding | Package | Requires | Loads the artifact with |
|---|---|---|---|
| Go | `github.com/wave-rf/chtypes/go` | Go 1.27+ | cgo + `dlopen` |
| Python | `chtypes` | Python 3.11+ | stdlib `ctypes` (no build step, no dependencies) |
| TypeScript | `@wavehouse/chtypes` | Node 22+, ESM only | `ffi-rs` (prebuilt) |
| Rust | `chtypes` | Rust 1.85+ (edition 2024) | `libloading` |

## Platforms

The artifact is native code, so a platform is supported only if the release publishes a build for it. Today that is:

- `darwin-arm64` — development floor, not an oracle: its `long double` makes float parses diverge from a real server
- `linux-amd64`
- `linux-arm64`

Both loaders are `dlopen`, so all four bindings are Unix-only. There is no Windows artifact and no 32-bit build.

## ClickHouse lines

One artifact per ClickHouse line, each carrying that release's own C++. A line is supported when it has a committed run of record in the core repository and the release publishes it:

| Line | Exact version | Platforms |
|---|---|---|
| `24.8` | `24.8.14.39-lts` | all |
| `25.3` | `25.3.14.14-lts` | all |
| `25.8` | `25.8.33.6-lts` | all |
| `25.10` | `25.10.7.6-stable` | all |
| `26.2` | `26.2.19.43-stable` | all |
| `26.3` | `26.3.33.24-lts` | all |
| `26.4` | `26.4.5.143-stable` | all |
| `26.5` | `26.5.7.64-stable` | all |
| `26.6` | `26.6.4.55-stable` | all |
| `26.7` | `26.7.6.57-stable` | all |
| `26.8` | `26.8.2.7-lts` | all |

Ask for a line, never a nearest match: `for("25.8")` resolves the newest build of that line and fails if it is absent, rather than quietly handing back a neighbor whose answers differ.

<!-- END GENERATED -->

## Why the ClickHouse list is not a promise about the future

A line appears above once core has a committed run of record for it and the release publishes the artifact. Lines are added as core certifies them, so this page is a snapshot of a moving list — `curl -s https://artifacts.wavehouse.dev/artifacts/index.json` is always the live answer, and `scripts/fetch.sh --all` reads it rather than restating it.

Nothing is removed to make room. The index keeps every patch row ever published, so a machine holding an older patch keeps working.

## Checking from your own machine

```sh
scripts/fetch.sh --all          # install every published line for this host
chtypes list                    # what is installed, and what the release offers
chtypes verify                  # re-hash every installed line against its manifest
```

`chtypes list` and `chtypes verify` are spelled the same way in all four bindings (`go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest`, `python -m chtypes`, `npx @wavehouse/chtypes`, `cargo install chtypes`), and [`guides/fetch.md`](guides/fetch.md) §6 is the contract they share.

## What "supported" means for a golden answer

The public golden set is generated per line and gates itself on the **exact** patch version, not the line. A case runs against an artifact only when that artifact's exact version equals the one its expectations were produced on, and skips loudly by name otherwise — an expectation produced on one build says nothing about another. See the core repository's golden-set documentation.

This is also why the golden set shrinks as lines are added: a case the lines answer differently is refused by the generator rather than recorded twice. Version-dependent truth lives in the core repository, per line. **The SDK asserts a version-specific answer nowhere.**

## macOS is a development floor

The darwin artifacts exist so you can develop and run the suites on a laptop. They are not an oracle: macOS's `long double` makes some float parses diverge from a real server, so a float expectation is taken from Linux or from a live ClickHouse, never from a Mac.

## Pre-1.0

The ABI is frozen at revision 4 (`include/chtypes.h`, 28 `chs_*` functions) and the four bindings pin that number at compile time. Package names, the artifact name `libchtypes`, the `chs_` prefix and the `enum chs_format` numbers are frozen; function signatures froze at the first tag. Anything else may still move before 1.0 — each binding's CHANGELOG carries its own list.
