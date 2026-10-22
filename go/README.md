# chtypes — Go SDK

> **SDK 2.x speaks ABI v2.** This binding speaks ABI v2's locked description and refuses a library with any other fingerprint. It fetches from the production channel, `https://registry.wavehouse.dev/chtypes/v2`, signed with the release key, and honors `CHTYPES_ARTIFACTS_URL`, `CHTYPES_TRUSTED_KEYS` and `CHTYPES_ALLOW_UNSIGNED` (and the matching `FetchOptions` fields), and `--lock`, `--frozen` and `--update` (and their options) ([`docs/guides/fetch-v1.md`](../docs/guides/fetch-v1.md#generation-2-after-the-lock)). Its cache is `${XDG_CACHE_HOME:-~/.cache}/chtypes/v2`, or `<CHTYPES_CACHE>/v2` under an explicit cache, which no 1.x SDK reads. Every vocabulary keeps a value it does not list as that type's `unknown(n)`: `Known` reports false for it, and it never fails a document. The rules are r1 to r9 of [`docs/reference/abi-v2.md`](../docs/reference/abi-v2.md). The 1.x module, `github.com/wave-rf/chtypes/go`, speaks ABI v1. A `2.0.0-dev.N` pre-release spoke an unstable draft of ABI v2 from the staging dev channel: it refuses the 2.0.0 library, and 2.0.0 refuses a dev build.
>
> An explicit cache directory gets the `v2` subroot whether it comes from `CHTYPES_CACHE` or from `FetchOptions.CacheDir`: the layout is under `<CacheDir>/v2`, never in the directory itself, so code that inspects or pre-populates a cache looks there; `chtypes.CacheRoot` and `chtypes.SearchDirs` return the resolved paths. `CHTYPES_OFFLINE=1` is the environment twin of `FetchOptions.Offline` and `--offline`: the cache only, no request.

**If this row were inserted into this table on this ClickHouse version, what would happen?** chtypes answers with ClickHouse's own code: the real C++ type machinery, vendored per release into a native artifact behind the frozen `chs_*` C ABI and reached here through cgo. Nothing semantic is reimplemented, so _"what does ClickHouse do with `256` into a `UInt8`?"_ is answered by ClickHouse rather than by a model of it. One peer binding among `{go, python, ts, rust}` — no language is privileged, and all four give one answer.

## Install

Two things: this package, and at least one **artifact**, the per-version native library it `dlopen`s at runtime. The registry fetches, verifies and installs artifacts for you when autofetch is on (`WithAutoFetch(true)` or `CHTYPES_AUTOFETCH=1`); the default build is **dlopen-only**: it compiles with cgo (for `dlfcn`) but links nothing and needs no build tree.

```sh
go get github.com/wave-rf/chtypes/go/v2
```

The `/v2` is Go's major-version rule: `go get github.com/wave-rf/chtypes/go` keeps resolving the 1.x module.

## Quickstart

```go
package main

import (
	"fmt"
	"log"

	"github.com/wave-rf/chtypes/go/v2/chtypes"
)

func main() {
	// Optional, and first: the image zone and default settings are fixed once per process.
	if err := chtypes.Setup(chtypes.SetupOptions{Timezone: "UTC"}); err != nil {
		log.Fatal(err)
	}
	reg, err := chtypes.NewRegistry(chtypes.WithAutoFetch(true))
	if err != nil {
		log.Fatal(err)
	}
	lib, err := reg.For("26.8") // two, three or four parts; never a nearest match
	if err != nil {
		log.Fatal(err)
	}
	schema, err := lib.CompileTable("CREATE TABLE t (x UInt8) ENGINE = MergeTree ORDER BY x")
	if err != nil {
		log.Fatal(err)
	}
	defer schema.Close()

	batch, err := schema.Rows(chtypes.JSONEachRow, []byte(`{"x":256}`))
	if err != nil {
		log.Fatal(err)
	}
	row := batch.Rows[0]
	fmt.Println(batch.Outcome)             // accepted
	fmt.Println(row.Values[0].Text)        // 0             what would actually be stored
	fmt.Println(row.Transformed[0].Reason) // overflow_wrap which is the product
}
```

The row is **accepted** and `256` is silently stored as `0`. The library reports that change; the binding only decodes it.

A DEFAULT that calls a random or UUID generator is drawn by the library and reported with `SourceDefaultGenerated`: to insert it, ask `Rows` for an export (`WithExport`) and insert the `Payload`, never the original body.

## A server profile

A table can be compiled on one ClickHouse server, described by a `ServerProfile`: its `Timezone`, the `Settings` its profile applies to every query, and its `Macros`. The schema then binds the server's zone, as the same table on that server does:

```go
srv, err := lib.NewServer(chtypes.ServerProfile{Timezone: "Asia/Tokyo"})
if err != nil {
	log.Fatal(err)
}
defer srv.Close()

schema, err := lib.CompileTable(
	"CREATE TABLE t (k UInt8, tz String DEFAULT timezone()) ENGINE = MergeTree ORDER BY k",
	chtypes.OnServer(srv),
)
if err != nil {
	log.Fatal(err)
}
defer schema.Close()

row, err := schema.Row(chtypes.JSONEachRow, []byte(`{"k":1}`))
if err != nil {
	log.Fatal(err)
}
fmt.Println(row.Values[1].Text) // Asia/Tokyo    the server's timezone(), not the image zone
```

- **Every field is optional and passed through as given.** `""` and a nil map are left out of the profile. The library judges the zone (`DateLUT`), each setting (the server's own `SET` check) and the macros (ClickHouse's own reader); a refusal is its own error, and the binding validates nothing first.
- **`Macros` nil is not `Macros` empty.** nil means the server's macros are unknown; a non-nil map, even an empty one, is the server's complete set, so a Replicated engine naming a macro it lacks is the server's own refusal.
- **A `Server` is immutable** and safe for concurrent use. A schema holds its own reference to its server, so `srv.Close()` and `schema.Close()` run in any order; a closed server passed to `OnServer` is a `*UsageError`.
- **`Schema.Describe` reports the server.** `SchemaDescription.Server` is nil exactly when the schema was compiled without one, and `SchemaDescription.Replicated` carries a Replicated engine's resolved ZooKeeper path and replica name.

The library compiles on a server. A schema whose engine reads a macro is declined (`server-macros-required`), because the server's macros are validated, not applied.

## Errors

A bad **row** is a verdict, not an error: `Outcome` becomes `Rejected` with ClickHouse's own code and message. Go errors are for the call as a whole.

- `*SchemaError`: the server refused. `ChCode` is a real ClickHouse code.
- `*UnsupportedError`: this build declines to answer, and a real server might well have accepted. **Fall back to the server**; never report a decline as a rejection. It is a peer of `*SchemaError`, never a subtype.
- `*UsageError`: misuse, such as a closed object, a refused version spelling or a conflicting `Setup`.
- `*InternalError`: a library bug, or a document that does not decode.
- `*ArtifactError`: fetch and load failures, with a `Code` and a sentinel per code for `errors.Is`.

`chtypes.AsCallError` reads the six fields every call error carries.

## Threads

Everything is safe for concurrent use and no call takes a lock. A `Server`, `Schema`, `Filter` or `Block` has a close guard: `Close` waits for the calls already inside that object, and a later call is a `*UsageError`.

## The linked build

`-tags chtypes_linked` adds `chtypes.OpenLinked()`, which links `-lchtypes` out of the artifact producer's build tree at compile time (`CGO_LDFLAGS` says where) and answers for exactly that one artifact. It is a development and rig instrument, not a consumer path.

## Tests

`go test ./...`. Every test that needs the ABI v2 test stub (`CHTYPES_ABI2_STUBS`, the `v2/` directory `scripts/abi-v1/build-stubs.sh --out DIR` writes) skips loudly by name without it.

## Documentation

The public API is specified in [`docs/reference/bindings-v1.md`](../docs/reference/bindings-v1.md); `go doc github.com/wave-rf/chtypes/go/v2/chtypes` is the same surface with the full prose.

## License

Apache 2.0. The artifacts this package loads are **Elastic License 2.0** — a separate license, shipped inside each artifact release.
