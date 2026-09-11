# Changelog — `@wavehouse/chtypes`

All notable changes to the ts binding. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this package follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The four bindings in this repository are released together and give one answer,
so an entry here has a counterpart in the other three.

## [Unreleased]

- **The golden set is served, not tracked.** `goldens/cases.json` no longer
  exists in this repository. Core publishes `sdk-goldens.json` in the rolling
  release as a row in the signed `SHA256SUMS`; `scripts/fetch.sh` installs it at
  `<registry>/sdk-goldens.json` and the golden test reads it offline from there
  (`CHTYPES_GOLDENS` overrides). A case runs only against the exact ClickHouse
  version `generated.exact` names for its line and skips loudly otherwise, so
  the set shrinks as lines are added rather than recording an answer twice.
- **Artifact identity gained a wrapper build.** Assets are
  `chtypes-<version>-<os>-<arch>-b<N>.tar.gz`, where `<N>` is the core commit's
  UNIX timestamp; an absent suffix means build 0 and stays valid. Line
  resolution ranks by `(version, build)`, so a rebuild of a line supersedes the
  older build.
- Fetching now retries the publish window, so a fetch that races a release no
  longer fails on sums naming a file the host has not finished serving.
- README install instructions name the published package rather than a path
  dependency.

## [0.1.0] — 2026-09-10

First release, published to npm from the tag `ts/v0.1.0`.

- Speaks **ABI revision 4** of the frozen `chs_*` C ABI
  (`include/chtypes.h`, 28 functions). The binding `dlopen`s a per-version
  artifact and reimplements no ClickHouse semantics; `Transformed` is the one
  derived result.
- Verified against the public golden set (`goldens/cases.json`, schema 1,
  31 cases), whose expectations were generated on ClickHouse
  24.8, 25.3, 25.8, 25.10, 26.5, 26.6 and 26.7 — every case answered
  identically on all seven lines, and any case that did not was refused by
  the generator.
- Pre-1.0: the artifact name `libchtypes`, the `chs_` prefix and the
  `enum chs_format` numbers are frozen. Function signatures freeze at this tag.
