# The artifact and registry contract

An artifact is one ClickHouse release, compiled, wrapped in the `chs_*` C ABI, plus the few files a loader needs to use it safely. A registry is a directory of them. This is the contract a loader in any language implements, and the contract a publisher must not break.

## Layout

One directory per **ClickHouse minor line**, each self-contained:

```text
<registry>/
  24.8/
    manifest.json          required — the loader's source of truth
    libchtypes.so          the shared library, named by manifest.library
    CH_VERSION             the exact ClickHouse version, one line
    unsafe_families.txt    families this build must refuse; may be empty
  25.3/   …
  25.8/   …
  25.10/  …
  26.5/   …
  26.6/   …
  26.7/   …
```

Platform-keyed, one tree per target:

```text
~/.cache/chtypes/artifacts/<os>-<arch>/<minor>/…
```

`<os>` is the lowercased OS name (`linux`, `darwin`) and `<arch>` is normalized (`aarch64 → arm64`, `x86_64 → amd64`). Current keys: **`linux-arm64`** (the shipping platform) and **`darwin-arm64`** (a development floor, never an oracle). `linux-amd64` is a build target, not a built artifact, on the current host.

**A loader MUST follow symlinks and MUST NOT assume the registry is inside any repository.** Build trees commonly symlink their output directories into `~/.cache/chtypes/` so that deleting a checkout cannot destroy hours of C++ compute, which means the path a loader is handed may be a link and the bytes may live anywhere.

Sizes, measured 2026-08-26: 159–288 MiB per `.dylib`, 191–272 MiB per `.so`, and roughly **120 MB resident per loaded version** in a process that dlopens several.

## `manifest.json`

Exactly nine fields, written by the build that produces it with `sort_keys=True, indent=1`. A real one, verbatim:

```json
{
 "arch": "arm64",
 "clickhouse_commit": "cfec8e085bab84e94140bb82eb5c18d64f4ff61a",
 "clickhouse_minor": "25.8",
 "clickhouse_version": "25.8.28.1-lts",
 "library": "libchtypes.dylib",
 "library_bytes": 232259712,
 "library_sha256": "b9800e96af1769b577e8788e2259cfd091611b06bc14617dae05df1d21402629",
 "os": "darwin",
 "unsafe_families": ""
}
```

| Field                | Type   | Meaning and how a loader uses it                                                                                                     |
| -------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| `library`            | string | **The shared library's file name.** A loader MUST read the file name from here and MUST NOT construct it. See the rule below.        |
| `library_bytes`      | int    | Size of that file. Cheap first-pass integrity check.                                                                                 |
| `library_sha256`     | string | SHA-256 of that file. The only integrity check that means anything.                                                                  |
| `clickhouse_version` | string | The exact release, e.g. `25.8.28.1-lts`. **Cross-check** against `chs_clickhouse_version()`; do not trust it over the library.       |
| `clickhouse_minor`   | string | The minor line, e.g. `25.8`. Informational — a loader SHOULD derive the minor from the library's own reported version instead.       |
| `clickhouse_commit`  | string | The upstream commit the tree was built from. Provenance; the only field that ties an artifact to a specific ClickHouse source state. |
| `os`                 | string | `linux` \| `darwin`. Lowercased `platform.system()`.                                                                                 |
| `arch`               | string | `arm64` \| `amd64`. Normalized.                                                                                                      |
| `unsafe_families`    | string | The generated refuse-list, inline. Duplicates `unsafe_families.txt`; empty on every current artifact.                                |

Additive changes to this file are allowed. A loader MUST ignore fields it does not know rather than failing, and MUST NOT require a field that is absent.

### The `library` field is not optional and not guessable

**A loader MUST resolve the shared-library file name from `manifest.json`'s `library` field.** Not from a hard-coded `libchtypes.so`, not from a glob, not from the platform.

Historically the Linux artifacts carried the name **`libchtypes_s1.so`** (built 2026-08-04, from the era when two implementations were being compared) while darwin shipped `libchtypes.dylib` — so on 2026-08-17 a loader that hard-coded `libchtypes.so` found nothing on the shipping platform. The current matrix is `libchtypes.dylib` / `libchtypes.so` on all seven versions × two platforms as of the 2026-08-26 relink, but old builds outlive a rename and third-party artifacts need not follow ours: artifacts with the historic name still load correctly because the reference implementation does `dlopen(filepath.Join(sub, m.Library))` — the manifest's name, never a constructed one.

The **base name** `libchtypes` and the symbol prefix `chs_` are frozen for new builds; the point of this rule is that _old_ builds outlive a rename, and rebuilding the matrix is hours of C++ compute per version.

## Loading

The reference algorithm (`chtypes.NewRegistry` in `go/chtypes/multiversion.go`):

1. `ReadDir(registry)`. For each entry that is a directory:
2. Read `manifest.json`. **If it is missing or unparseable, skip the directory silently** — a registry may legitimately contain scratch directories, and a `.DS_Store` is not a version.
3. `dlopen(dir/<manifest.library>, RTLD_NOW | RTLD_LOCAL)`. A failure here IS an error and MUST abort with the path and the `dlerror()` text: a directory that has a manifest and does not load is broken, not absent.
4. Resolve the `chs_*` symbols by name. Four are **mandatory everywhere** — `chs_clickhouse_version`, `chs_init`, `chs_schema_compile`, `chs_rows`. If any is missing, `dlclose` and reject the library: it is not a chtypes artifact. **The four bindings do not agree beyond that**, and this is a known divergence rather than a rule. The Go reference named above also requires `chs_free`, `chs_validate_type` and `chs_schema_free` — seven in all — because its C shims call those three without NULL checks, so admitting a library that lacks one would trade a clean load error for a SIGSEGV on first use. Python, TypeScript and Rust treat all three as optional and degrade to `unsupported`. No artifact this repository builds is affected: every one exports all seven. Which number is the contract is open — see issue #13.
5. Resolve `chs_abi_revision`. **Absent** -> the artifact predates the probe; record revision `0` and continue with the rules below — absence is ignorance, not incompatibility. **Present** -> call it. If it returns a value that is neither `0` nor the revision the binding was written against (`CHS_ABI_REVISION`), **reject the library**, naming both numbers: the artifact has positively stated that the binding's declarations do not describe it, and calling through them is undefined.
6. Every other symbol is **optional**. A missing one means "this artifact predates the feature" and MUST degrade to `unsupported` at call time, never to a load failure. The reference returns a private `-3` from its C shims for a missing `chs_schema_engine` / `chs_schema_ttl` and turns it into `CodeUnsupported` with `"this artifact predates engine support (rebuild it)"`; a missing `chs_row` returns `NULL`, reported as `"this artifact predates chs_row (rebuild it)"`.
7. Column introspection is **all-or-nothing**: `chs_schema_column_count`, `_name`, `_type`, `_default_expr`, `_default_kind`, `_default_is_literal` shipped together. If any is missing, treat the whole group as absent and leave the schema's column list empty rather than partially populated.
8. Ask the library its own version: `chs_clickhouse_version()`. **The library names itself; nothing is inferred from the path.** Derive the minor line from that string.
9. `chs_init(timezone, unsafe_families, out_err)`, once per library, with the contents of that version's own `unsafe_families.txt`. Each library keeps its own DateLUT and its own refuse-list. `out_err` MAY be `NULL`; when it is not and the call fails, it carries ClickHouse's own message (free with `chs_free`) — the reachable failure is an unknown timezone.
10. Index the library under **both** its exact version and its minor line.
11. If zero libraries loaded, that is an error naming the directory — an empty registry is a configuration mistake, not an empty result.

`RTLD_LOCAL` is not a detail: it is what keeps each library's ClickHouse symbols private, so two builds that both define `DB::DataTypeFactory` never collide. A loader that uses `RTLD_GLOBAL` will appear to work and answer with the wrong version's semantics.

### Discovery, for tools

The reference oracle also falls back to discovery when its linked build cannot answer for the requested version, checked in order: `$CHTYPES_REGISTRY`, then `dist/out` at several depths relative to the executable, then relative to the working directory. Crucially, **only a registry that actually has the requested version wins**, so a stale directory cannot shadow a correct startup error. A binding MAY implement the same fallback; if it does, it MUST keep that rule.

## Verification

The artifact carries its own checksum, so verification is not optional and not expensive. The build tooling's artifact-cache verifier walks each version directory, re-hashes the library, and compares against `library_sha256`. Its own comment states the reason plainly: a move that reported success and truncated a 232 MB library would look identical to one that worked.

A loader SHOULD verify the hash before `dlopen` when the artifact came from anywhere other than a local build — and MUST when it came from a network. **Two of the four bindings implement this today**: Python (`verify_hashes`) and TypeScript (`verifyChecksums`). Go has no load-time hash check, and the Rust crate says so in its own source — it takes no crypto dependency and relies on the release pipeline's verification instead. A caller pointing either at network-sourced bytes must verify before constructing the registry. This is a known divergence, not a license to skip it; see issue #13. It SHOULD also assert `chs_clickhouse_version()` against `manifest.clickhouse_version` after loading, because that catches the one class of corruption a hash cannot: the right bytes in the wrong directory.

## Platform properties a loader must respect

- **Self-contained by construction.** The library exports only `chs_*`, keeps libc++ statically inside, and links nothing but libc (plus CoreFoundation on macOS). That is why N versions can coexist in one process.
- **glibc floor 2.17** on `linux-arm64`, measured. Verify before shipping into an older base image.
- **Static-TLS exhaustion.** With the full seven-version registry on glibc, the **third** artifact fails to load with `cannot allocate memory in static TLS block`. The fix is `GLIBC_TUNABLES=glibc.rtld.optional_static_tls=131072`, baked into the artifact image and passed by `lib/tools/oracle-linux.sh` for images that predate it. A loader that dlopens three or more versions on glibc MUST either run in an environment that sets this or surface the error with the remedy, because the raw message names nothing actionable.
- **rpath.** A shipped binary needs an rpath relative to itself or its RUNPATH points at the builder's filesystem. Linux is the shipping target and cgo accepts `$ORIGIN`; a relocatable darwin binary needs `-ldflags "-r @loader_path"` because cgo's flag validator rejects `@loader_path`.
- **macOS is a development floor, not an oracle.** Its `long double` is 53-bit, so float parses diverge from a real server — the float corpus matches 395/395 on Linux and 0/395 on macOS. Float expectations MUST come from a Linux artifact or a live server. Additionally, the mandatory `new_delete` archive that routes plain `operator new` into ClickHouse's `MemoryTracker` cannot be linked on macOS, so the DEFAULT-evaluation memory ceiling is weaker there.

## The one failure mode to design against

Statically linking two ClickHouse versions into one binary **exits 0, reports zero duplicate symbols, runs, and answers with one version's semantics for both** — same binary size, one version string, no warning at any point. It is a correctness failure that presents as success, and it is the reason the artifact is one version per shared object loaded `RTLD_LOCAL`, rather than one fat binary.

The guard is a **cross-control**, and any packaging change MUST re-run it: score version A's library against version B's _server_. It has to come out near 0 %, not near 100 %. Measured: 25.3 library vs 25.3 server 97.0 %, 25.8 vs 25.8 98.0 %, and the cross-controls 1.0 % and 0.0 %. A cross-control that scores high means the versions are not actually separate, whatever the build log said.
