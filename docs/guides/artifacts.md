# Artifacts — getting one, and knowing you got the right one

An **artifact** is one ClickHouse release compiled and wrapped in the `chs_*` C ABI: a single self-contained shared library of 160–300 MB that carries that release's real type machinery. A binding is a few thousand lines of glue; the artifact is the product. Nothing in this repository builds one — you fetch one.

This page is what a consumer needs. [`fetch-v1.md`](fetch-v1.md) is the normative contract underneath it: the verification chain link by link, the lock, the error codes and exit statuses. [`../reference/artifact.md`](../reference/artifact.md) is what the registry serves and what a loader relies on.

## Get one

Every binding ships the same command, so you need nothing from this repository:

```sh
go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest fetch 26.8   # Go
python -m chtypes fetch 26.8                                         # Python
npx @wavehouse/chtypes fetch 26.8                                    # TypeScript
cargo install chtypes && chtypes fetch 26.8                          # Rust
```

`fetch --all` takes every line the registry publishes for this platform. A spelling is two, three or four parts (`26.8`, `26.8.15`, `26.8.15.10`) with no `v` prefix and no channel suffix; a floating one resolves to the newest build inside it. The commands are spelled identically in all four bindings:

| command           | answers                                                                     |
| ----------------- | --------------------------------------------------------------------------- |
| `fetch <line>...` | resolve, verify and install one or more lines (`--all` for every published) |
| `verify`          | re-verify every installed library against its own `verified.json` record    |
| `list`            | the lines the registry publishes                                            |
| `where`           | the cache directory a fetch would write to                                  |

`fetch` takes `--frozen`, `--offline` and `--lock <file>` ([Pinning](#pinning-for-ci-and-production)). Usage errors exit 2, and every other exit status comes from the error table in [`fetch-v1.md` §8](fetch-v1.md#8-errors), so a script can tell an unreachable registry from a refused signature.

From code you do not need to shell out: a binding's `Registry` fetches on demand when its `autofetch` option is on (see [Lazy fetch](#lazy-fetch-is-opt-in)), and the fetch layer's own entry points are [`fetch-v1.md` §9](fetch-v1.md#9-the-seam).

## Where it lands, and where it is looked for

One cache, shared by all four bindings: an OCI image layout at `${XDG_CACHE_HOME:-~/.cache}/chtypes/v1/`, or the directory `CHTYPES_CACHE` names. Verified libraries are unpacked beside it under `unpacked/sha256/<manifest-hex>/`. Read-only system directories (`/usr/local/share/chtypes/v1`, then `/opt/chtypes/v1`) are searched after the user cache and never written to, which is how an image bakes libraries in. `CHTYPES_REGISTRY`, the variable the previous generation used, is retired: set, it produces one warning and is otherwise ignored. The cache layout, and how to pre-seed one for an offline machine with `oras copy`, are [`fetch-v1.md` §1](fetch-v1.md#1-the-cache).

## What is checked

Before anything is unpacked, a fetch checks, in order: the platform manifest's digest against the index, a **Sigstore bundle** signature over the statement under the release key, that the statement names the platform, the version and the ABI generation you asked for, and the layer's size and sha256. After the layer is unpacked, the library file is hashed again against the signed statement. After it is loaded, the binding's own ABI fingerprint is compared with the library's. Any failure removes whatever it produced; nothing half-installed is ever trusted by a later `--offline` read.

- The default trust is the release key alone. `CHTYPES_TRUSTED_KEYS` **replaces** the list, never appends to it.
- `CHTYPES_ALLOW_UNSIGNED=1` skips signature verification with one loud warning naming the source. Never the default, never silent.
- `CHTYPES_DOWNLOAD_TOKEN`, when set, is sent as a bearer token to the configured registry hosts only, and never across a redirect.

## Pinning, for CI and production

`fetch --lock chtypes.lock` records, per platform, the request, the exact version and build that resolved, and the platform manifest, layer and bundle digests ([`fetch-v1.md` §6](fetch-v1.md#6-lock-schema-3-and---frozen--offlineupdate)). `fetch --frozen` then fetches by digest only and refuses anything the lock does not pin with `CHTYPES_ARTIFACT_PINNED`. It is the lockfile model every package manager uses: trust on first fetch, byte-identical thereafter, CI fails on drift.

```sh
npx @wavehouse/chtypes fetch 26.8 --lock chtypes.lock   # record
npx @wavehouse/chtypes fetch 26.8 --frozen              # refuse anything the lock does not pin
```

A lock records digests, never a host name, so it works against any mirror. A lock from the previous generation is refused, never reinterpreted. `--offline` reads only the cache and never touches the network.

## Lazy fetch is opt-in

Opening a version that nothing on the search path holds can fetch it first, but only if you ask: the registry constructor's `autofetch` option, or `CHTYPES_AUTOFETCH=1` for every registry in the process. Off, the missing version is the one error below. It is off by default because **a production process must not begin a 250 MB download inside a request**.

## The one error

A version that nothing on the search path holds is one identifiable error in every binding, `CHTYPES_ARTIFACT_MISSING`, and it names the exact command that would fix it, because the alternative, a stack trace about a `NULL` handle, sends people to the wrong half of the system. The other codes a fetch raises are `CHTYPES_ARTIFACT_UNTRUSTED`, `CHTYPES_ARTIFACT_CORRUPT`, `CHTYPES_ARTIFACT_PINNED`, `CHTYPES_ARTIFACT_UNPUBLISHED`, the `CHTYPES_SOURCE_*` family and, from the loader, `CHTYPES_ARTIFACT_INCOMPATIBLE`; each is in the table in [`fetch-v1.md` §8](fetch-v1.md#8-errors).

**A version is never another line's answer.** Asking for `25.8` resolves that line or fails naming what is present; it never quietly hands back 26.7's semantics. Version behavior is not monotonic (25.10 rejects a DEFAULT that both 25.8 and 26.6 accept), so a different line's answer is not an approximation of the right one, it is a different answer.

## Loading

The loader trusts only what the fetch layer verified, and it never opens a path it was not handed by that layer (or one you name explicitly as unverified). After `dlopen` it reads the library's own `build_info` and refuses a mismatch with the verified statement or with the binding's compiled-in ABI fingerprint, naming both sides. The steps are [`../reference/abi-v1.md`](../reference/abi-v1.md#loading-a-library). Several versions can be open in one process: each library keeps its own ClickHouse state.

## Licensing

Artifacts are **Elastic License 2.0**, a different license from the Apache 2.0 bindings that load them. The license text ships with each artifact, and downloads are anonymous.

## Two notes about platforms

**macOS artifacts match ClickHouse on macOS.** On 26.7 and later, macOS and Linux ClickHouse agree bit for bit. On 26.3, ClickHouse's float text parse differs by operating system and architecture, so run the library on the server's own platform. See [`../limitations.md`](../limitations.md#macos-artifacts-match-clickhouse-on-macos-on-263-match-the-servers-platform).

**Which lines exist is a question the registry answers.** `chtypes list` prints the lines it publishes, and [`../support-v1.md`](../support-v1.md) says what this repository can and cannot claim about them: the v1 channel carries no statement of which lines are supported, so a line's support reads unknown, never unsupported.
