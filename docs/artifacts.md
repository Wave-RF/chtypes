# Artifacts — how a consumer obtains and verifies the native library

An artifact is one ClickHouse release compiled and wrapped in the `chs_*` C
ABI, plus the files a loader needs (`spec/artifact.md` is the contract for
what is inside and how a registry directory is laid out). Nothing that
consumes chtypes builds one: CI, a container image, an application and a
developer's laptop all *download* the same bytes a release was cut from, and
can prove they got them.

This page is the consumer's half of that contract: the asset names, the
listing, the verification chain, `scripts/fetch.sh`, and the versioning rule.
Producing and publishing artifacts is the core repository's business
(`chtypes-core/docs/distribution.md`), and the two pages are written against
one contract — the naming, the `index.json` schema and the verification chain
are fixed; fields are added, never changed.

| | |
|---|---|
| **channel** | `https://artifacts.wavehouse.dev/<tag>/`, one directory per release tag, plus the rolling `artifacts` release; the same files as the publishing repository's GitHub Releases |
| **unit** | one tarball per (ClickHouse version, platform) |
| **listing** | `index.json` (schema 1) as a release asset |
| **integrity** | `SHA256SUMS` as a release asset, plus each artifact's own `manifest.json` |
| **consumer** | [`scripts/fetch.sh`](../scripts/fetch.sh) |
| **where it lands** | `${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/<os>-<arch>/<minor>/` — the registry every SDK here defaults to |

---

## 1. The asset names

```
chtypes-<clickhouse_version>-<os>-<arch>.tar.gz
```

`<clickhouse_version>` is **the manifest's `clickhouse_version`**, verbatim,
channel suffix included — `25.8.28.1-lts`, `26.7.3.19-stable`. Not the minor
line, and not a repo version: the exact ClickHouse release whose type system is
inside. `<os>-<arch>` is the cache platform key: `linux`/`darwin` ×
`amd64`/`arm64`, in those spellings (a manifest that says `x86_64` or `aarch64`
is normalised, and `publish.sh` fails if the manifest and the platform it was
found under disagree about anything else).

```
chtypes-24.8.14.39-lts-darwin-arm64.tar.gz
chtypes-25.8.28.1-lts-linux-amd64.tar.gz
chtypes-26.7.3.19-stable-darwin-arm64.tar.gz
```

Each tarball holds the artifact directory's files **at the tar root**, no
leading directory:

```
manifest.json          the record — read this, infer nothing
libchtypes.so          the library manifest.json names
unsafe_families.txt    the refuse-list Library.Load feeds to chs_init
CH_VERSION             the ClickHouse version as plain text
```

**The library file name comes out of `manifest.json` and is never assumed.**
The name `libchtypes` and the `chs_` prefix are frozen, but Linux
artifacts built before the `_s1` suffix was dropped call the file
`libchtypes_s1.so` — none remain in this machine's cache (the matrix was
relinked as `libchtypes.so` by 2026-08-26), but such artifacts still
circulate externally. `chtypes.NewRegistry` (Go; every SDK has the equivalent) is manifest-driven and loads either;
so are `publish.sh` and `fetch.sh`. A tarball preserves exactly what the
artifact directory contains — packing is not an opportunity to rename anything.

Two fields are missing from those same early Linux manifests (every current
manifest carries both): `clickhouse_minor`
and `library_bytes`. The minor line is derived from `clickhouse_version` when
absent, which is what `minorOf()` in `../chtypes/go/chtypes/multiversion.go` does, so the
tools and the loader agree by construction.


## 2. `index.json` — the listing

One per release, machine-readable, and the only thing a consumer needs to read
before it knows what to download.

```json
{
 "schema": 1,
 "generated_at": "2026-08-17T13:20:24Z",
 "release_tag": "test-tag",
 "artifacts": [
  {
   "arch": "arm64",
   "bytes": 57588360,
   "clickhouse_minor": "25.8",
   "clickhouse_version": "25.8.28.1-lts",
   "file": "chtypes-25.8.28.1-lts-darwin-arm64.tar.gz",
   "library": "libchtypes.dylib",
   "library_sha256": "275c396f60e5f6c614104aa4513526cf6fe1a0d1cd39914f41ed8029327ccbd3",
   "os": "darwin",
   "sha256": "528266f052bd0dd418371ee4fdb0a04f96fdad59197e490db52b4fc0abdd328f"
  }
 ]
}
```

| field | meaning |
|---|---|
| `schema` | `1`. A consumer must refuse a schema it does not know rather than guess. |
| `generated_at` | UTC ISO-8601, when the release was packed (`SOURCE_DATE_EPOCH` pins it). |
| `release_tag` | the repo tag this release was published under. |
| `artifacts[]` | every tarball in the release. |
| `.clickhouse_version` | exact ClickHouse release, e.g. `25.8.28.1-lts`. |
| `.clickhouse_minor` | the line, e.g. `25.8` — the key `Registry.For` and the arbiter ask in. |
| `.os`, `.arch` | `linux`/`darwin`, `amd64`/`arm64`. |
| `.file` | the asset name to download. |
| `.sha256` | sha256 **of the tarball**. |
| `.bytes` | size **of the tarball**. |
| `.library` | the library file name inside the tarball. |
| `.library_sha256` | sha256 of that library, copied from the artifact's `manifest.json`. |

`artifacts` is sorted by `os`, `arch`, then the minor line **numerically** — so
`25.8` precedes `25.10`, which a string sort gets backwards. That ordering trap
is the reason `ci/resolve-version.py` exists at all (its docstring records a
version-keying bug that cost an entire scoring column), and it is worth
remembering when writing any consumer that sorts these rows itself.

`SHA256SUMS` is the standard `sha256sum` format — `<hex>␣␣<file>` — one line per
tarball, sorted by file name, and nothing else. It is verifiable with the stock
tool: `sha256sum -c SHA256SUMS` (or `shasum -a 256 -c`) in a directory holding
the tarballs.


## 3. The verification chain

Four links, and a consumer must walk them in this order. The reason is the rule
that runs through this whole repo: an exit code is not evidence. A truncated
300 MB library and a good one produce the same `curl` and `tar` status, and the
failure this exists to prevent — a wrong or damaged library loaded into a
gateway that then answers *authoritatively* about types — is silent.

1. **`index.json` → asset.** Names the file, its `sha256` and its `bytes`.
2. **`SHA256SUMS` → the same sha256.** Two independently written records of the
   same hash. If they disagree the *release* is inconsistent; refuse it and do
   not install anything. (`fetch.sh` treats this as fatal, not as a warning.)
3. **tarball → sha256, before unpacking.** Size first, then hash. A mismatch
   aborts with the archive still on disk and nothing extracted.
4. **`manifest.json` → `library_sha256`, after installing.** The manifest inside
   the tarball is the artifact's own claim about itself; it must agree with
   `index.json`, and the installed library is re-hashed *in place* — everything
   before this step proved the bytes were right somewhere else.

Step 4 is the same check the core repository's `ci/cache.sh verify` performs on its build cache, which
means one predicate — "this library hashes what its manifest says" — covers the
artifact from the moment it is built to the moment it is dlopen'd.

There is no signature and no provenance attestation yet; see
the core repository's page (`docs/distribution.md` §9).


## 4. Fetching

```bash
scripts/fetch.sh <version-spelling> [--platform <os-arch>] [--dest <registry-dir>] \
                                 [--tag <release-tag> | --url <base-url>] \
                                 [--repo owner/name] [--force]

scripts/fetch.sh --all --platform linux-amd64 --dest /opt/chtypes/artifacts   # every line, for a container
```

`--out` is an alias for `--dest`, and `--all` installs every line the release
publishes for the platform — which lines those are is a question `index.json`
answers, so a caller never restates the version list.

Installs into `<dest>/<clickhouse_minor>/`, which *is* the `NewRegistry` layout.
Point a `Registry` at `<dest>` afterwards and the version is live. The install
path prints only the installed directory on stdout, so it composes:

```bash
dir="$(scripts/fetch.sh 25.8)"
```

**Version spellings.** `25.8`, `25.8.28.1` and `v25.8.28.1-lts` all work,
normalised locally; `index.json` is the authority on what exists. (With the
core repository checked out beside this one, its `ci/resolve-version.py` is
used instead and Docker digests resolve too.) One
distinction is deliberate and matters:

- a **line** (`25.8`) takes whatever patch the release published for that line,
  and says so when that differs from the patch the moving tag points at today
  (asked for `25.8`, `resolve-version.py` expands to `25.8.30.16-lts`; the
  release publishes `25.8.28.1-lts`; you get the published one, with a note);
- an **exact patch** (`25.8.30.16-lts`) is a hard requirement. If the release
  does not publish that patch, `fetch.sh` fails and lists what it does have,
  rather than installing a neighbour.

**Defaults.** `--platform` is this host's own `<os>-<arch>` (`CHTYPES_TARGET`
overrides it): a consumer fetches the library its process will dlopen. Fetching
for another platform — Linux artifacts on a Mac, for a container — is legitimate
and is printed loudly so it is never mistaken for a native one. `--dest` is the
per-user cache, `${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/<platform>`,
which is what every SDK's registry default resolves to; a `--dest` elsewhere is
reachable by pointing `CHTYPES_REGISTRY` at it.

**Sources.** By default the public artifacts host,
`https://artifacts.wavehouse.dev/<tag>/<asset>` (`CHTYPES_ARTIFACTS_URL`
overrides the host; `--tag` picks a release, the rolling `artifacts` release
otherwise). `--repo owner/name` (or `CHTYPES_RELEASE_REPO`) switches to that
repository's GitHub Releases — `gh release download` when `gh` exists, plain
`curl` against `https://github.com/<repo>/releases/{download/<tag>,latest/download}/<asset>`
when it does not. `--url <base>` is anything else — a mirror, a local directory
or a `file://` path, which is how the offline test drives it against a
`--dry-run` stage. A download token, when the host requires one, travels as
`Authorization: Bearer` from `CHTYPES_DOWNLOAD_TOKEN`.

**Idempotent.** If `<dest>/<minor>/` already holds a library that hashes what
`index.json` says, the fetch says "already installed and verified" and downloads
nothing. That is a *verified* claim (the same hash check the install path ends
with), not an inference from a directory existing. `--force` re-downloads.

**Never half-installed.** The tarball is unpacked into a temp sibling and moved
into place with a rename, so an interrupted fetch cannot leave a partial
`<minor>/` for `NewRegistry` to dlopen. Two processes fetching the same version
concurrently both download and the last rename wins — with identical bytes, so
the outcome is the same either way.


## 5. How a client binding fetches one version lazily

A binding (Go, Python, Node) must not ship 300 MB per ClickHouse line, and must
not require the user to have this repo. The pattern:

1. Decide the one version the caller asked for; do **not** fetch the set.
2. Look in a per-user cache: `${XDG_CACHE_HOME:-~/.cache}/chtypes/artifacts/<os>-<arch>/<minor>/`.
   Present and hash-verified → done, no network.
3. Absent → read `index.json` from the pinned release, select the row for
   `(os, arch, minor)`, and walk §3's chain: `SHA256SUMS` cross-check, hash the
   tarball before unpacking, unpack to a temp sibling, rename into place,
   re-hash the installed library against `manifest.json`.
4. `NewRegistry(<cache dir>)`, or the binding's equivalent, and dispatch.

That is exactly what `fetch.sh` does, and a binding that can shell out should
call it rather than re-implement the chain. What a binding must **pin** is the
release tag: it maps its own version to one chtypes release, so an upgrade of
the binding is what changes the artifacts, never a fetch at runtime. And it
must keep the ordering rule from §2 — resolve `25.10` after `25.8`.

Steady state is offline. The dlopen'd library needs no network, no ClickHouse
server, and no state beyond the artifact directory.


## 6. Versioning: a repo tag is a set of ClickHouse versions

Two version axes meet here and conflating them is the mistake to avoid.

- **The ClickHouse version** is a property of an artifact. It is in the asset
  name, in `manifest.json`, in `CH_VERSION`, and the library reports it itself
  (`chs_clickhouse_version`, which is what `Library.Load` believes over any
  path or file name).
- **The repo tag** is the release: a snapshot of *this* repository's code —
  the C API, the wrapper, `unsafe_families.txt`, the Go binding — packaged with
  the set of ClickHouse artifacts that were current when it was cut.

So one release carries N ClickHouse versions × M platforms, and `index.json`
enumerates them. A release is a **complete set, not a delta**: a consumer reads
one `index.json` and sees everything that tag offers.

Two kinds of release, then, and only one of them is a version:

| release | tag | who writes it | what it means |
|---|---|---|---|
| rolling | `artifacts` | each builder, as it finishes one artifact | "this exists and is verified" |
| versioned | `v*` | `--assemble` at tag time | "these are the ones" |

Consumers pin the versioned one. The rolling release is a staging area whose
membership changes; pointing CI at it is how a pipeline starts silently testing
something new.

Rules that follow, and they are the ones that keep a consumer's cached hash
meaningful:

- **A published `(tag, asset name)` must never change bytes.** Adding a
  ClickHouse line, adding a platform, or rebuilding an artifact means a **new
  repo tag**. `--clobber` exists so an interrupted upload can be completed, not
  so a release can be edited under consumers who already recorded its hashes.
- Adding a ClickHouse line is a normal, expected release. Nothing about the
  layout changes: one more directory in the cache, one more tarball, one more
  row in `index.json`, and `NewRegistry` picks it up because it scans.
- Dropping a line means it stops appearing in new releases. Old releases keep
  working; that is what pinning a tag is for.

