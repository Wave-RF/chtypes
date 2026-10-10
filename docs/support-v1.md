# What chtypes supports

Three independent axes, and a combination works only if all three do: the **language** you call from, the **platform** you run on, and the **ClickHouse line** you want answers for. This page is written by hand. The v1 channel publishes no `supported_lines` statement, so there is no generated matrix to copy from; where this page does not know something, it says **support unknown**, which is not the same as unsupported.

## Languages

| Binding    | Package                         | Requires                  | Loads the library with          |
| ---------- | ------------------------------- | ------------------------- | ------------------------------- |
| Go         | `github.com/wave-rf/chtypes/go` | Go 1.27+                  | cgo + `dlopen`                  |
| Python     | `chtypes`                       | Python 3.11+              | stdlib `ctypes` (no build step) |
| TypeScript | `@wavehouse/chtypes`            | Node 22.21+, ESM only     | `ffi-rs` (prebuilt)             |
| Rust       | `chtypes`                       | Rust 1.87+ (edition 2024) | `libloading`                    |

All four loaders are `dlopen`, so all four bindings are Unix-only. There is no Windows library and no 32-bit build.

## Platforms

The library is native code, so a platform works only if the registry serves a build for it. The platforms a v1 fetch recognizes are fixed by [`spec/fetch-v1/constants.json`](../spec/fetch-v1/constants.json):

- `linux-amd64`
- `linux-arm64`
- `darwin-arm64` — matches ClickHouse on macOS; on 26.3, float text parses by platform; see [macOS artifacts match ClickHouse on macOS](limitations.md#macos-artifacts-match-clickhouse-on-macos-on-263-match-the-servers-platform)

A host on any other platform gets `CHTYPES_ARTIFACT_UNPUBLISHED` from a fetch, naming what the registry does offer.

**The glibc floor.** On Linux the library needs a glibc at or above the floor its own build records. The floor is carried in each library's signed statement (the `glibc_floor` field, passed through the fetch layer opaque) and checked by the loader after the library loads, never by the fetch layer. A host below it is refused with `CHTYPES_ARTIFACT_INCOMPATIBLE` rather than loaded and left to fail later. This page states no number on purpose: the floor belongs to a build, and a copy here could only drift from it. musl-based images are in the same position: support unknown until a build says otherwise.

## ClickHouse lines

One library per ClickHouse line, each carrying that release's own C++. A line is published once the artifact producer has compared it against a real server.

**Which lines are supported is not stated here.** The v1 channel carries no `supported_lines` statement, so the honest answer for any line is the one the registry gives you: either the line is published, or it is not. A line that is published and not named as supported anywhere reads **support unknown**, never unsupported.

List the lines the registry publishes with the CLI of any binding:

```sh
chtypes list
```

`chtypes list` reads the registry's tag listing and filters it as [`guides/fetch-v1.md`](guides/fetch-v1.md) describes. The exact ClickHouse patch each line is built from is in the library's signed statement and moves with every upstream patch release, so it is not repeated here. Ask for a line, never a nearest match: `for("25.8")` resolves the newest build of that line and fails if it is absent, rather than quietly handing back a neighbor whose answers differ.

## Checking from your own machine

```sh
chtypes fetch 26.8              # resolve, verify and install one line for this host
chtypes list                    # what the registry publishes
chtypes verify                  # re-verify every installed line against its signed statement
chtypes where                   # the cache directory
chtypes resolve 26.8            # the build each platform's 26.8 resolves to, verified, installing nothing
chtypes prune --dry-run         # the installed builds newer ones of their line supersede
```

The six commands are spelled the same way in all four bindings, and [`guides/fetch-v1.md`](guides/fetch-v1.md) is the contract they share, including the error codes and exit statuses.

## What "supported" means for a golden answer

The public golden set is generated per line and gates itself on the **exact** patch version, not the line. A case runs against a library only when that library's exact version equals the one its expectations were produced on, and skips loudly by name otherwise: an expectation produced on one build says nothing about another. This is also why the golden set shrinks as lines are added: a case the lines answer differently is refused by the generator rather than recorded twice. Version-dependent truth lives with the artifact producer, per line. **The SDK asserts a version-specific answer nowhere.** See [`guides/goldens-v1.md`](guides/goldens-v1.md).

## Pre-1.0

Until 1.0, the ABI is provisional: the `chs_*` function table, its description under [`spec/abi-v1/`](../spec/abi-v1/) and the public API in [`reference/bindings-v1.md`](reference/bindings-v1.md) may change. A library is matched to an SDK by the ABI fingerprint its statement carries, and the loader refuses a mismatch naming both sides.

## A generated v1 matrix

A generated per-line, per-platform matrix for v1 is planned for 1.1. Until then this page is the whole statement.
