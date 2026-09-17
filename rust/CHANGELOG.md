# Changelog — `chtypes`

All notable changes to the rust binding. The format is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this package follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The four bindings in this repository are released together and give one answer, so an entry here has a counterpart in the other three.

## [0.2.2] — 2026-09-17

No public API change.

### Notes

- **The parity value check now skips `fetch::*` rows loudly when built without the `fetch` feature, instead of failing to cover them silently.** `pub mod fetch` is `#[cfg(feature = "fetch")]`, so with `--no-default-features` — the configuration `docs/install.md` tells a consumer to build for the loader without the downloader — the module and its constants do not exist in the compiled surface at all. The check now gates those rows on the feature directly, prints which ids it skipped, and asserts at least one was skipped so the skip path itself cannot go quiet; every other row, and every `fetch::*` row when the feature is on, is still asserted exactly as before. CI now also builds and clippies this crate `--no-default-features`. Test-and-CI only; no library behavior changed (#45).
- Speaks **ABI revision 4**, unchanged since 0.1.0, so no artifact needs relinking. The golden set this release was tested against is the one core serves, generated on the ClickHouse lines 24.8, 25.3, 25.8, 25.10, 26.2, 26.3, 26.4, 26.5, 26.6, 26.7 and 26.8.
- Seven facts about the compiled library's behavior that a consumer previously had to discover by experiment are now written down: `CHECK`-constraint batch rejection, binding filter parameters as `String`, single-line compact JSON array loss under `allow_errors`, the JSONCompactEachRow export field separator, the value-injection pattern and its compile-time wrap trap, `Columns` as canonical and in declaration order, and the frozen-signatures promise reworded ahead of the next ABI revision (#56). That promise is now stated once, in `docs/support.md`, and linked from everywhere else rather than restated in nine places (#69).

## [0.2.1] — 2026-09-15

### Added

- **`Registry::open(dir, RegistryOptions)`**: an eager, one-directory registry that also takes `verify_checksums`, closing the verification asymmetry with Go's `WithVerifyChecksums`, Python's `verify_hashes` and TypeScript's `verifyChecksums` — a Rust caller no longer has to switch to `from_search_path_with` to verify a single directory. `RegistryOptions.dir`, `.autofetch` and (with the `fetch` feature) `.fetch` only mean something on the search path, so `open` refuses a non-default value for any of them, naming the field and `from_search_path_with`, rather than ignoring it. `with_timezone` is now a thin wrapper over `open`; `new` and the `from_env*` constructors are unchanged (#35).

### Fixed

- **An unreadable library file now reports `Error::LibraryRead` naming the file**, where it previously reported `Error::Registry` with the file's path in the `dir` field meant for the registry directory (#34). This covers both read sites: the `library_bytes` size check and the checksum-verification hash. `Error` is `#[non_exhaustive]`, so this new variant is additive.

### Notes

- Speaks **ABI revision 4**, unchanged since 0.1.0, so no artifact needs relinking. The golden set this release was tested against is the one core serves, generated on the ClickHouse lines 24.8, 25.3, 25.8, 25.10, 26.2, 26.3, 26.4, 26.5, 26.6, 26.7 and 26.8.
- **The ABI-revision refusal is now run, not assumed.** This binding's suite points a registry at a generated artifact answering the wrong revision and asserts the load is refused naming both numbers, beside a matching-revision control that must load; CI fails the build if either case did not run (#36).

## [0.2.0] — 2026-09-15

### Changed — BREAKING

- **The TypeScript filter verdicts are the wire characters.** `Verdict.True` / `False` / `Error` / `Decline` are now `'t'` / `'f'` / `'e'` / `'d'`, which Go, Python and Rust have always rendered. TypeScript alone spelled them `'true'` / `'false'` / `'error'` / `'decline'`, so two SDKs could not share a log line, a fixture or a test. The four states and the fail-closed rule are unchanged: `'e'` and `'d'` are NOT answers, and a caller enforcing visibility must hide the row or fail the request on both — collapsing either into `false` inverts fail-closed into fail-open, the measured leak class. Only the rendered characters moved. **The parity suites now ENFORCE the shared value in all four bindings**; until this release two of the four merely declared it.

- **TypeScript `FetchError` now extends `ArtifactError` (and so `RegistryError`), where it previously extended `ChtypesError` directly.** A `try`/`catch` chain that tests `RegistryError` before `FetchError` now takes the `RegistryError` arm for the five fetch verdicts, which it did not before. Ordering a catch chain most-specific-first is unaffected.

### Added

- **Load-time checksum verification in Go and Rust**, matching what Python and TypeScript have always offered: re-hash the library against `manifest.json`'s `library_sha256` **before `dlopen`**, and refuse the load on a mismatch. Off by default in all four. Whether an artifact was re-hashed before being mapped used to depend on which binding a consumer picked, which is a security posture and not an API spelling. A manifest carrying no `library_sha256` is refused by Go, TypeScript and Rust — verification asked for and not possible is not verification — while Python's returns silently, which is what remains of issue #13's item A2.
- **One catchable artifact error in TypeScript.** `ArtifactError` is a new exported base: all six artifact conditions now extend it, where `ArtifactMissingError` previously sat under `RegistryError` and the other five under `FetchError`, so a caller had to catch two unrelated types. Every existing `code` value and message is unchanged, and `ArtifactMissingError instanceof RegistryError` still holds.
- **Eight constants that three bindings exported and the fourth did not**, so a caller no longer retypes a literal the other SDKs hand them: `Registry.SearchPath()` (go), `parse_signature_file` and `FETCH_COMMAND` (python), `RELEASE_KEY_ID` and `DEFAULT_LOCK_FILE` (ts), `fetch::LOCK_SCHEMA` (rust), and `CHTYPES_REGISTRY` / `CHTYPES_AUTOFETCH` as `EnvRegistry` / `EnvAutoFetch` (go) and `ENV_REGISTRY` / `ENV_AUTOFETCH` (ts). Each new name replaces the inline literal in its own binding's code path, so there is one definition rather than two.

### Notes

- Speaks **ABI revision 4**, unchanged since 0.1.0, so no artifact needs relinking. The golden set this release was tested against is the one core serves, generated on the ClickHouse lines 24.8, 25.3, 25.8, 25.10, 26.2, 26.3, 26.4, 26.5, 26.6, 26.7 and 26.8.
- `Library` still has no scope-based release in Python or TypeScript, while `Registry`, `Schema`, `Filter` and `Block` do. That asymmetry is deliberate — a `with library:` or `using library` closes at the end of a block, which is the mid-lifecycle teardown measured to segfault on the next open — and it is now recorded in the parity manifest so no future audit reopens it as a gap.

- **`RegistryOptions` gained a public field** (`verify_checksums`), so an exhaustive struct literal that does not end with `..Default::default()` no longer compiles. This is the only source-breaking change in the Rust crate besides the verdict characters.
- **`sha2` is now an unconditional dependency**, not one behind the `fetch` feature, and `rust/src/registry.rs` previously documented the opposite — that the crate "does not hash (it takes no crypto dependency)". That was a divergence rather than a design: a consumer who builds `--no-default-features` obtains artifacts by some path this crate never sees, often a network one, and is exactly the consumer who most needs to re-hash before `dlopen`. It is pure Rust with no system library.

## [0.1.2] — 2026-09-11

### Changed — BREAKING

- **The transform reason for a materialized DEFAULT is now `default_materialized`** — previously the same word spelled with an `s` — and the exported constant naming it is now `reason::DEFAULT_MATERIALIZED`. ClickHouse's own keyword is `MATERIALIZED`; its parser rejects the `s` spelling outright with a syntax error, so the reason naming that concept now matches the system it describes. Code comparing against the old string or the old constant name must be updated.
- This is an SDK-only change and **the ABI is untouched: it remains revision 4, and no artifact needs relinking.** The reason is derived in the binding, not received from the artifact — the library emits `default_substituted`, which each binding translates. That wire value is unchanged.

### Changed

- **The per-language README is now an orientation page, not a manual.** It carries install, a quickstart that runs, the three outcomes and the error model, and then points at `docs/` for the rest — roughly 450 lines of reference prose moved to `docs/guides/` and `docs/reference/` rather than being repeated four times and drifting four ways.
- **The published packages no longer cite a repository you cannot open.** 148 source comments — in every binding and in `include/chtypes.h`, all of which ship inside the crate, the sdist and the module — attributed the contract they implement to a private repository's specification. They now cite **the C ABI contract**, with `include/chtypes.h` as the public authority for it. Alongside those, 14 dead paths into that repository, the examples' instruction to build an artifact there, and two descriptions of the signing infrastructure are gone. What remains is every place where the two-repository split is itself the subject — describing it in prose is honest and stays legal; naming something a reader cannot open is not, and `scripts/lint-public.sh` is what enforces the difference. (An earlier draft of this entry enumerated three files. That count was never checked and was wrong by eighteen, which is the same mistake the entry above is about: the rule is checkable, a file list is not.)
- `scripts/lint-public.sh` is new and blocking in CI, because the sweep this replaces reported itself complete while 148 of its targets survived in a different shape. It refuses both the private repository's name and that exact prose pointer, and its `--selftest` proves each rule fires rather than assuming it.

## [0.1.1] — 2026-09-11

- **The golden set is served, not tracked.** `goldens/cases.json` no longer exists in this repository. Core publishes `sdk-goldens.json` in the rolling release as a row in the signed `SHA256SUMS`; `scripts/fetch.sh` installs it at `<registry>/sdk-goldens.json` and the golden test reads it offline from there (`CHTYPES_GOLDENS` overrides). A case runs only against the exact ClickHouse version `generated.exact` names for its line and skips loudly otherwise, so the set shrinks as lines are added rather than recording an answer twice.
- **Artifact identity gained a wrapper build.** Assets are `chtypes-<version>-<os>-<arch>-b<N>.tar.gz`, where `<N>` is the core commit's UNIX timestamp; an absent suffix means build 0 and stays valid. Line resolution ranks by `(version, build)`, so a rebuild of a line supersedes the older build.
- Fetching now retries the publish window, so a fetch that races a release no longer fails on sums naming a file the host has not finished serving.
- README install instructions name the published package rather than a path dependency.
- Two errors in the published README, both found by running the snippets rather than reading them: Python's `substituted` is on `RowResult`, not `BatchResult`, and TypeScript's `using schema = …` is a syntax error on Node 22, this package's own `engines` floor (`schema.close()` is portable).
- `docs/support.md` is new: which language versions, platforms and ClickHouse lines are supported, generated from the manifests and the release's own index.

## [0.1.0] — 2026-09-10

First release, published to crates.io from the tag `rust/v0.1.0`.

- Speaks **ABI revision 4** of the frozen `chs_*` C ABI (`include/chtypes.h`, 28 functions). The binding `dlopen`s a per-version artifact and reimplements no ClickHouse semantics; `Transformed` is the one derived result.
- Verified against the public golden set (`goldens/cases.json`, schema 1, 31 cases), whose expectations were generated on ClickHouse 24.8, 25.3, 25.8, 25.10, 26.5, 26.6 and 26.7 — every case answered identically on all seven lines, and any case that did not was refused by the generator.
- Pre-1.0: the artifact name `libchtypes`, the `chs_` prefix and the `enum chs_format` numbers are frozen. Function signatures freeze at this tag.
