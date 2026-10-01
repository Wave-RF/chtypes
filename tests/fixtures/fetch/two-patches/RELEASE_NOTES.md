chtypes native artifacts — `fixtures`

Licensed under the Elastic License 2.0 (LICENSE, NOTICE beside this file).

One tarball per (ClickHouse version, platform). Unpack into
`<registry-dir>/<clickhouse_minor>/` — the layout `chtypes.NewRegistry` loads —
or let the SDK's `scripts/fetch.sh` do it, which verifies the chain for you.

| ClickHouse | platform | build | asset | bytes | library |
|---|---|---|---|---|---|
| 25.8.28.1-lts | darwin-arm64 | 0 | `chtypes-25.8.28.1-lts-darwin-arm64.tar.gz` | 472 | `libchtypes.dylib` |
| 25.8.33.5-lts | darwin-arm64 | 0 | `chtypes-25.8.33.5-lts-darwin-arm64.tar.gz` | 471 | `libchtypes.dylib` |
| 25.8.28.1-lts | linux-amd64 | 0 | `chtypes-25.8.28.1-lts-linux-amd64.tar.gz` | 466 | `libchtypes.so` |
| 25.8.33.5-lts | linux-amd64 | 0 | `chtypes-25.8.33.5-lts-linux-amd64.tar.gz` | 467 | `libchtypes.so` |
| 25.8.28.1-lts | linux-arm64 | 0 | `chtypes-25.8.28.1-lts-linux-arm64.tar.gz` | 467 | `libchtypes.so` |
| 25.8.33.5-lts | linux-arm64 | 0 | `chtypes-25.8.33.5-lts-linux-arm64.tar.gz` | 468 | `libchtypes.so` |

`build` is the chtypes wrapper build (core's commit count). Rows of the
same ClickHouse version and platform differ only in the wrapper linked
into them; take the highest build at your SDK's ABI revision. The listing
is append-only: every build ever published stays listed and fetchable, so
a lock file that pins a file name and sha256 keeps resolving.

Verification: `SHA256SUMS` covers every tarball; each tarball's
`manifest.json` carries `library_sha256` for the library inside it.
`SHA256SUMS.sig` is an ed25519 signature over the bytes of `SHA256SUMS`, key id
`d1251e468f9156ef`; every SDK's fetch verifies it before reading anything else.
`index.json` (schema 1) is the machine-readable listing.
