# Changelog — `chtypes`

All notable changes to the python binding. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this package follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The four bindings in this repository are released together and give one answer,
so an entry here has a counterpart in the other three.

## [0.1.0] — 2026-09-10

First release, published to PyPI from the tag `python/v0.1.0`.

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
