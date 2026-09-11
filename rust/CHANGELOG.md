# Changelog — `chtypes`

All notable changes to the rust binding. The format is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this package follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The four bindings in this repository are released together and give one answer, so an entry here has a counterpart in the other three.

## [0.1.2] — 2026-09-11

### Changed — BREAKING

- **The transform reason for a materialized DEFAULT is now `default_materialized`** — previously the same word spelled with an `s` — and the exported constant naming it is now `reason::DEFAULT_MATERIALIZED`. ClickHouse's own keyword is `MATERIALIZED`; its parser rejects the `s` spelling outright with a syntax error, so the reason naming that concept now matches the system it describes. Code comparing against the old string or the old constant name must be updated.
- This is an SDK-only change and **the ABI is untouched: it remains revision 4, and no artifact needs relinking.** The reason is derived in the binding, not received from the artifact — the library emits `default_substituted`, which each binding translates. That wire value is unchanged.

### Changed

- **The per-language README is now an orientation page, not a manual.** It carries install, a quickstart that runs, the three outcomes and the error model, and then points at `docs/` for the rest — roughly 450 lines of reference prose moved to `docs/guides/` and `docs/reference/` rather than being repeated four times and drifting four ways.
- **The published packages no longer cite a repository you cannot open.** 148 source comments — in every binding and in `include/chtypes.h`, all of which ship inside the crate, the sdist and the module — attributed the contract they implement to a private repository's specification. They now cite **the C ABI contract**, with `include/chtypes.h` as the public authority for it. Alongside those, 14 dead paths into that repository, the examples' instruction to build an artifact there, and two descriptions of the signing infrastructure are gone. What remains naming it is the handful of places where the two-repository split is the actual subject — the README, `SECURITY.md`, and `docs/reference/README.md`, which says plainly that the full normative specification is not public.
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
