# The artifact and registry contract

An artifact is one ClickHouse release, compiled, wrapped in the `chs_*` C ABI, and published to an OCI registry together with a signed statement that binds the bytes to a platform, a version and an ABI generation. This page is the contract a publisher must not break and a loader in any language relies on. How a binding gets one onto a machine is [`guides/fetch-v1.md`](../guides/fetch-v1.md), which is normative for the wire; the C layer a loader calls is [`abi-v1.md`](abi-v1.md).

## What the registry serves

One repository, `https://registry.wavehouse.dev/chtypes/v1` by default (`CHTYPES_ARTIFACTS_URL` replaces it). Per ClickHouse version spelling there is a tag, and per tag:

- a multi-platform **OCI image index** with one descriptor per platform (`linux-amd64`, `linux-arm64`, `darwin-arm64`);
- per platform, one **image manifest** with exactly one layer: the library as a zstd-compressed tar (`application/vnd.oci.image.layer.v1.tar+zstd`);
- per manifest, one or more **Sigstore bundle** referrers carrying the signed statement, and one goldens referrer (see [`guides/goldens-v1.md`](../guides/goldens-v1.md)).

The registry is **append-only**. A build once published stays fetchable by its digest, and a corrected goldens set is a second referrer, never a replacement.

## The signed statement

The statement is an in-toto Statement v1 whose subject is the layer's sha256 and whose predicate carries what a loader needs to trust the bytes it unpacks. The fields this repository reads are:

| field                | what it is                                                                                                                              |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `abi`                | the ABI generation, always `1` for v1                                                                                                   |
| `os`, `arch`         | the platform the library was built for                                                                                                  |
| `clickhouse_version` | the exact ClickHouse release, four parts, equal to or within the request                                                                |
| `library_sha256`     | sha256 of the unpacked library file: the only integrity check that means anything                                                       |
| `library_bytes`      | size of that file, a cheap first-pass check                                                                                             |
| `abi_fingerprint`    | the fingerprint of the ABI description the library was built against, carried through the fetch layer opaque and compared by the loader |
| `glibc_floor`        | the minimum glibc the build needs, on Linux only. The loader checks it; the fetch layer never does                                      |

Additive fields are allowed, and a reader MUST ignore a field it does not know. The release key (`fdb5f06a8d4c9918d049a5f1748fa2e3b3238c3f2000986d5bb9e31beff778fc`, key id `deb275922dbff76e`) is the default trust root; the whole chain is [`guides/fetch-v1.md` §4](../guides/fetch-v1.md#4-trust).

## The cache layout

A fetch lands in a standard OCI image layout at `${XDG_CACHE_HOME:-~/.cache}/chtypes/v1/`, with each verified library unpacked at `unpacked/sha256/<manifest-hex>/` beside `blobs/`. The layout, what the `verified.json` record inside an unpacked directory proves, and how a cache is pre-seeded for an offline machine are [`guides/fetch-v1.md` §1](../guides/fetch-v1.md#1-the-cache). **A loader MUST follow symlinks and MUST NOT assume the cache is inside any repository:** build trees commonly symlink their output directories into the cache so that deleting a checkout cannot destroy hours of C++ compute.

## Loading

A loader never opens bytes the fetch layer has not verified, and never trusts a path. The load steps, in order, are [`abi-v1.md` § Loading a library](abi-v1.md#loading-a-library); in outline:

1. the fetch layer resolves the request and returns a verified, unpacked library (or a cache hit, after re-checking the request against the signed statement);
2. the loader `dlopen`s it with `RTLD_NOW | RTLD_LOCAL`;
3. it reads the library's own `chs_build_info` and refuses the library on any mismatch with the verified statement or with the binding's compiled-in ABI fingerprint, naming both sides;
4. it applies the process setup (image zone and defaults) once per image.

`RTLD_LOCAL` is not a detail: it keeps each library's ClickHouse symbols private, so two builds that both define `DB::DataTypeFactory` never collide. A loader that uses `RTLD_GLOBAL` will appear to work and answer with the wrong version's semantics. A loader never unloads a library.

## Platform properties a loader must respect

- **Self-contained by construction.** The library exports only `chs_*`, keeps libc++ statically inside, and links nothing but libc (plus CoreFoundation on macOS). That is why several versions can coexist in one process.
- **The glibc floor** is recorded per build in the signed statement (see above). A host below it is refused with `CHTYPES_ARTIFACT_INCOMPATIBLE`.
- **Many versions in one process.** A library needs no static thread-local storage, so a process may `dlopen` as many as it wants on glibc with no tunable set; the artifact producer proves it per build. A library built before 2026-09-10 for 24.8 or 25.3 is the one historical exception (it failed on the third load with `cannot allocate memory in static TLS block`), and a v1 fetch never selects one.
- **rpath.** A shipped binary that links a library statically needs an rpath relative to itself, or its RUNPATH points at the builder's filesystem. cgo accepts `$ORIGIN` on Linux; a relocatable darwin binary needs `-ldflags "-r @loader_path"` because cgo's flag validator rejects `@loader_path`.
- **macOS artifacts match ClickHouse on macOS.** On 26.7 and later, macOS and Linux ClickHouse agree bit for bit. On 26.3, ClickHouse's own parse of Float values from text input formats differs by operating system and architecture, so on 26.3 the library MUST run on the same operating system and architecture as the server. The library's guarantees are relative to a same-platform server. See [`limitations.md`](../limitations.md#macos-artifacts-match-clickhouse-on-macos-on-263-match-the-servers-platform).

## The one failure mode to design against

Statically linking two ClickHouse versions into one binary **exits 0, reports zero duplicate symbols, runs, and answers with one version's semantics for both**: same binary size, one version string, no warning at any point. It is a correctness failure that presents as success, and it is the reason an artifact is one version per shared object loaded `RTLD_LOCAL`, rather than one fat binary.

The guard is a **cross-control**, and any packaging change MUST re-run it: score version A's library against version B's server. It has to come out near 0%, not near 100%. A cross-control that scores high means the versions are not actually separate, whatever the build log said.
