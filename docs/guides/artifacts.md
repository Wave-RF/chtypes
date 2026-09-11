# Artifacts — getting one, and knowing you got the right one

An **artifact** is one ClickHouse release compiled and wrapped in the `chs_*` C ABI: a single self-contained shared library of 160–300 MB that carries that release's real type machinery. A binding is a few thousand lines of glue; the artifact is the product. Nothing in this repository builds one — you download it, and you can prove you got the bytes the release was cut from.

This page is what a consumer needs. [`fetch.md`](fetch.md) is the normative contract underneath it — the verification chain link by link, the signature format, the exit codes, the decisions where four implementations had to agree.

## Get one

Every binding ships the same command, so you need nothing from this repository:

```sh
go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest fetch 25.8   # Go
python -m chtypes fetch 25.8                                         # Python
npx @wavehouse/chtypes fetch 25.8                                    # TypeScript
cargo install chtypes && chtypes fetch 25.8                          # Rust
```

`fetch --all` takes every line the release publishes for this platform. From a checkout, [`scripts/fetch.sh`](../../scripts/fetch.sh) is the reference implementation of the same contract.

All four print the installed directory alone on stdout and progress on stderr, so `dir="$(python -m chtypes fetch 25.8)"` composes. The other three subcommands are spelled identically everywhere:

| command           | answers                                                     |
| ----------------- | ----------------------------------------------------------- |
| `fetch <line>...` | install one or more lines (`--all` for every published one) |
| `verify`          | re-hash every installed line against its own manifest       |
| `list`            | what is installed, and what the release offers              |
| `where`           | the registry directory a fetch would write to               |

Exit codes: 0 ok · 1 verification failed · 2 usage · 3 source unreachable · 4 not published for this platform or line.

The same thing from code, when you would rather not shell out:

<details open><summary><b>Go</b></summary>

```go
inst, err := chtypes.Ensure(ctx, "25.8", chtypes.FetchOptions{Progress: os.Stderr})
// inst.Dir is <registry>/25.8; inst.AlreadyInstalled says whether any bytes moved
```

</details>

<details><summary><b>Python</b></summary>

```python
import chtypes

path = chtypes.ensure("25.8")   # returns the installed directory; idempotent
```

</details>

<details><summary><b>TypeScript</b></summary>

```ts
import { ensure } from '@wavehouse/chtypes';

const r = await ensure('25.8');   // r.dir is <registry>/25.8; r.installed says whether bytes moved
```

</details>

<details><summary><b>Rust</b></summary>

```rust
use chtypes::{EnsureOptions, ensure};

let installed = ensure("25.8", &EnsureOptions::default())?;   // installed.dir, installed.action
```

</details>

`ensure` is idempotent in all four: a line that is installed and hashes what the signed release says is a no-op, and nothing is downloaded. Every flag of the command is a field or option of the function — `dest`, `platform`, `url`/`tag`, `lock`, `frozen`, `force`, `offline`, plus the trust policy.

## Where it lands, and where it is looked for

A **registry** is a directory holding one subdirectory per ClickHouse minor line. Lookup walks a search path and takes the **first directory that holds the line asked for**:

1. a path handed to the registry constructor;
2. `$CHTYPES_REGISTRY`;
3. `${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/<os>-<arch>` — the per-user cache, where a fetch installs;
4. `/usr/local/share/chtypes/artifacts/<os>-<arch>`, then `/opt/chtypes/artifacts/<os>-<arch>` — reserved system locations, empty today, for images that bake artifacts in.

A fetch **writes** to the first of (1), (2), (3) that is set, and never to (4). `<os>` is `linux` or `darwin`; `<arch>` is `amd64` or `arm64`, in exactly those spellings. One machine set up once therefore serves all four bindings, and a core build lands in the same place.

Inside, one directory per line:

```text
<registry>/
  25.8/
    manifest.json         the record — read this, infer nothing
    libchtypes.dylib      (.so on Linux; the NAME comes from manifest.json)
    CH_VERSION            the ClickHouse version, as plain text
    unsafe_families.txt   this build's own refuse-list; empty is a valid list
  26.7/
...
  sdk-goldens.json        the served golden set, if the release publishes one
```

**The library's file name comes out of `manifest.json` and is never assumed.** The name `libchtypes` and the `chs_` prefix are frozen, but Linux artifacts built before the `_s1` suffix was dropped call the file `libchtypes_s1.so`, and such artifacts still circulate. Every loader here is manifest-driven and takes either.

## Loading

What a loader does with that directory, in order. This is the reference algorithm every binding implements; a binding may add to it but may not skip a step.

1. Read the registry directory. For each entry that is a directory:
2. Read `manifest.json`. **If it is missing or unparseable, skip the directory silently** — a registry may legitimately hold scratch directories, and a `.DS_Store` is not a version.
3. `dlopen` the file `manifest.json`'s `library` field names, with `RTLD_NOW | RTLD_LOCAL`. A failure here **is** an error and aborts with the path and the `dlerror()` text: a directory that has a manifest and does not load is broken, not absent.
4. Resolve the `chs_*` symbols by name. Four are **mandatory** — `chs_clickhouse_version`, `chs_init`, `chs_schema_compile`, `chs_rows`. If any is missing, `dlclose` and reject the library: it is not a chtypes artifact.
5. Resolve `chs_abi_revision`. **Absent** means the artifact predates the probe: record revision `0` and continue under the rules below, because absence is ignorance, not incompatibility. **Present** means call it — and if it returns a value that is neither `0` nor the revision this binding was written against (`CHS_ABI_REVISION`), **reject the library**, naming both numbers. The artifact has positively stated that the binding's declarations do not describe it, and calling through them is undefined.
6. Every other symbol is **optional**. A missing one means "this artifact predates the feature" and must degrade to an `unsupported` answer at call time, never to a load failure.
7. Column introspection is **all-or-nothing**: the six `chs_schema_column_*` entry points shipped together, so if any is missing, treat the whole group as absent and leave the column list empty rather than partially populated.
8. Ask the library its own version with `chs_clickhouse_version()`. **The library names itself; nothing is inferred from the path.** Derive the minor line from that string.
9. Call `chs_init(timezone, unsafe_families)` once per library, with the contents of that version's own `unsafe_families.txt`. Each library keeps its own DateLUT and its own refuse-list.
10. Index the library under **both** its exact version and its minor line.

`RTLD_LOCAL` is not a detail: it is what keeps each library's ClickHouse symbols private, so two builds that both define `DB::DataTypeFactory` never collide. A loader that uses `RTLD_GLOBAL` will appear to work and answer with the wrong version's semantics. [`multi-version.md`](multi-version.md) is what that buys you.

Loading is lazy per line in every binding's search-path constructor: constructing a registry reads manifests, and asking for a version is what dlopens it.

## Verification, and what it is not

A fetch is a **verification chain, not a download**. Nothing is a verdict but the chain — not an exit code, not a `Content-Length`, not "download finished". In order:

0. `SHA256SUMS.sig` is an ed25519 signature over the exact bytes of `SHA256SUMS`, checked against the release key each binding embeds (key id `deb275922dbff76e`). An unsigned or mis-signed release is `CHTYPES_ARTIFACT_UNTRUSTED`, and nothing is downloaded around it.
1. `index.json` names the asset for this line and platform, and records its sha256.
2. The now-authentic `SHA256SUMS` must list the same file with the same sha256.
3. The tarball is hashed **before** it is unpacked.
4. The installed library is re-hashed in place, against the `manifest.json` that came inside the tarball.

The install is atomic — unpack into a temporary sibling, rename into place — so an interrupted fetch can never leave a half-installed line for a loader to find. Any mismatch is `CHTYPES_ARTIFACT_CORRUPT` and nothing is installed.

The reason this is worth five steps rather than one: a truncated 300 MB library and a good one produce the same `curl` and `tar` status, and the failure being prevented is silent. A wrong or damaged library loaded into a gateway that then answers _authoritatively_ about types is exactly the outcome chtypes exists to stop.

Two environment variables move the trust boundary, and both are deliberate:

- `CHTYPES_TRUSTED_KEYS=<hex>[,<hex>…]` **replaces** the embedded key — for a mirror or a private registry signed by someone else.
- `CHTYPES_ALLOW_UNSIGNED=1` skips step 0 with one loud warning naming the source. Never the default, never silent.

**Once files are in a registry directory, the loader trusts the directory.** Verification is a fetch-time policy, not a load-time gate, exactly as a runtime trusts `node_modules`. Custom or locally built artifacts are installed by copying them in. If you want the check at load time anyway, Python and TypeScript take a constructor flag for it (`verify_hashes=True`, `verifyChecksums: true`), and `chtypes verify` re-hashes everything installed.

## Pinning, for CI and production

`fetch --lock chtypes.lock` records, per `<os>-<arch>/<minor>`, the asset file and sha256 that were installed. `fetch --frozen` then refuses anything else with `CHTYPES_ARTIFACT_PINNED`. It is the lockfile model every package manager uses: trust on first fetch, byte-identical thereafter, CI fails on drift.

```sh
npx @wavehouse/chtypes fetch 25.8 --lock chtypes.lock   # record
npx @wavehouse/chtypes fetch 25.8 --frozen              # refuse anything the lock does not pin
```

`--frozen` without `--lock` reads `./chtypes.lock`. A lock file that does not exist under `--frozen` is `CHTYPES_ARTIFACT_PINNED`: nothing is pinned, so nothing is installed.

A pin survives a rebuild on purpose. The same ClickHouse version can be published more than once, each with a higher build number, and a line resolves to its newest version and then to the **highest build** of that version. Because a lock pins a file name and a hash rather than a line, a rebuild does not silently become what you install.

## Lazy fetch is opt-in

Opening a line that no directory on the search path holds can fetch it first, but only if you ask: the registry constructor's `autofetch` option, or `CHTYPES_AUTOFETCH=1` for every registry in the process. Off, the missing line is the one error below.

It is off by default because **a production process must not begin a 250 MB download inside a request**. With it on, the fetch runs once per process per line under one lock, so concurrent opens share the one download.

In TypeScript the split is in the method names rather than a flag: `registry.for(v)` is synchronous and never fetches, `await registry.open(v)` is its twin that can.

## The one error

A line no directory on the search path holds is one identifiable error in every binding — Go's `ErrArtifactMissing` (which works with `errors.Is`), Python's and TypeScript's `ArtifactMissingError`, Rust's `Error::ArtifactMissing` — carrying the same message everywhere apart from the bracketed parts:

```text
chtypes: no artifact for ClickHouse 25.8 (darwin-arm64). Looked in: /Users/me/.cache/chtypes/artifacts/darwin-arm64, /usr/local/share/chtypes/artifacts/darwin-arm64, /opt/chtypes/artifacts/darwin-arm64.
Install it:  python -m chtypes fetch 25.8
or set CHTYPES_AUTOFETCH=1 to fetch on first use.
```

It names every directory it looked in and the exact command that would fix it, because the alternative — a stack trace about a `NULL` handle — sends people to the wrong half of the system. The fetch-time failures share a vocabulary of codes with it: `CHTYPES_ARTIFACT_MISSING`, `…_UNTRUSTED`, `…_CORRUPT`, `…_PINNED`, `…_UNPUBLISHED`, and `CHTYPES_SOURCE_UNREACHABLE`.

**A version is never a nearest match.** Asking for `25.8` resolves that line or fails naming what is present; it never quietly hands back a neighbor. Version behavior is not monotonic — 25.10 rejects a DEFAULT that both 25.8 and 26.6 accept — so a neighbor's answer is not an approximation of the right one, it is a different answer.

## What a release contains

One release, one `index.json` (schema 1), and it is a **complete set rather than a delta**: read it and you see everything that tag offers. Asset names are

```text
chtypes-<clickhouse_version>-<os>-<arch>[-b<build>].tar.gz
```

where `<clickhouse_version>` is the manifest's own `clickhouse_version`, channel suffix included (`25.8.28.1-lts`, `26.7.3.19-stable`) — not the minor line, and not a repository version. `-b<build>` is the wrapper build for that ClickHouse version; a name without one is build 0.

Two version axes meet here, and conflating them is the mistake to avoid. **The ClickHouse version** is a property of an artifact: it is in the asset name, in `manifest.json`, in `CH_VERSION`, and the library reports it itself. **The release tag** is a snapshot of this repository's code packaged with the set of ClickHouse artifacts current when it was cut. So one release carries N ClickHouse versions × M platforms.

There are two kinds of release and only one of them is a version. The rolling `artifacts` release is a staging area whose membership changes as core certifies lines; a versioned `v*` tag says "these are the ones". **Pin the versioned one** — pointing CI at the rolling release is how a pipeline starts silently testing something new. A published `(tag, asset name)` never changes bytes: adding a line, adding a platform or rebuilding an artifact means a new tag, which is what keeps a consumer's recorded hash meaningful.

Which lines exist is a question [`index.json`](https://artifacts.wavehouse.dev/artifacts/index.json) answers, and `fetch --all` reads it rather than restating a list. [`../support.md`](../support.md) renders the current answer, generated rather than typed.

## Licensing

Artifacts are **Elastic License 2.0** — a different license from the Apache 2.0 bindings that load them. `LICENSE` and `NOTICE` ship inside every release, `index.json` names the license, and a fetch says so once. Downloads are anonymous.

## Two notes about platforms

**macOS is a development floor, not an oracle.** The darwin artifacts exist so you can develop and run the suites on a laptop. Their `long double` is 53-bit, which makes some float parses diverge from a real server, so a float expectation is taken from Linux or from a live ClickHouse, never from a Mac. See [`../limitations.md`](../limitations.md).

**Linux holds as many versions as you like.** An artifact needs no static thread-local storage, so a process may `dlopen` as many as it wants on glibc with no tunable set — the producer proves it per build. One historical exception, for anyone holding old files: the 24.8 and 25.3 artifacts published before 2026-09-10 carried one initial-exec TLS access and failed on the third load with `cannot allocate memory in static TLS block`. Re-fetch them.
