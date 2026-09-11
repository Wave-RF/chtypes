# chtypes — Go SDK

**If this row were inserted into this table on this ClickHouse version, what would happen?** chtypes answers with ClickHouse's own code: the real C++ type machinery, vendored per release into a native artifact behind the frozen `chs_*` C ABI and reached here through cgo. Nothing semantic is reimplemented, so *"what does ClickHouse do with `256` into a `UInt8`?"* is answered by ClickHouse rather than by a model of it. One peer binding among `{go, python, ts, rust}` — no language is privileged, and all four give one answer.

## Install

Two things: this package, and at least one **artifact** — the per-version native library it `dlopen`s at runtime.

```sh
go get github.com/wave-rf/chtypes/go
go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest fetch 25.8
```

The fetch lands in `~/.cache/chtypes/artifacts/<os>-<arch>/25.8/` — the per-user cache every chtypes binding reads by default — after checking an ed25519 signature over the release and the sha256 of every byte. `$CHTYPES_REGISTRY` overrides it.

The default build is **dlopen-only**: it compiles with cgo (for `dlfcn`) but links nothing, includes no header and needs no build tree. That is what `go get` gives you.

## Quickstart

```go
package main

import (
	"fmt"
	"log"

	"github.com/wave-rf/chtypes/go/chtypes"
)

func main() {
	reg, err := chtypes.NewRegistry("") // "" = walk the search path
	if err != nil {
		log.Fatal(err)
	}
	lib, err := reg.For("25.8") // a line or an exact patch; never a nearest match
	if err != nil {
		log.Fatal(err)
	}
	schema, err := lib.CompileDDL("x UInt8, ts DateTime DEFAULT now()")
	if err != nil {
		log.Fatal(err)
	}
	defer schema.Close()

	batch, err := schema.Rows(chtypes.JSONEachRow, []byte(`{"x":256}`), nil)
	if err != nil {
		log.Fatal(err)
	}
	row := batch.Rows[0]
	fmt.Println(batch.Outcome)             // accepted
	fmt.Println(row.Values[0].Text)        // 0             — what would actually be stored
	fmt.Println(row.Transformed[0].Reason) // overflow_wrap — which is the product
	fmt.Println(row.Substituted)           // ts: send it explicitly, or preview != stored
}
```

The row is **accepted** and `256` is silently stored as `0`. That report — `Transformed` — is the one derived answer in the system and the reason it exists.

`ts` was substituted rather than stored: send every substituted column as an explicit value in the real INSERT, or the server re-evaluates `now()` at its own instant and your preview is not what landed.

## Three outcomes, and conflating any two is a bug

A bad **row** is a verdict, not an error: `Outcome` becomes `Rejected` with ClickHouse's own code and message. Go errors are for schema-level answers and for the machinery.

- `*SchemaError` — the server refused. `Code` is a real ClickHouse code.
- `*UnsupportedError` — this build declines to answer, and a real server might well have accepted. **Fall back to the server**; never report a decline as a rejection.
- a plain `error` — usage or environment: a closed handle, a bad artifact directory.

## Documentation

| | |
|---|---|
| [Quickstart](https://github.com/wave-rf/chtypes/blob/main/docs/quickstart.md) | the same program in all four languages |
| [Go API reference](https://github.com/wave-rf/chtypes/blob/main/docs/reference/go.md) | every symbol, the C entry point under it, what it returns and what it errors with |
| [Artifacts](https://github.com/wave-rf/chtypes/blob/main/docs/guides/artifacts.md) | getting one, where it lands, verifying and pinning it |
| [Batches](https://github.com/wave-rf/chtypes/blob/main/docs/guides/batches.md) | always `Rows`, and the two bad-row policies |
| [Transformations](https://github.com/wave-rf/chtypes/blob/main/docs/guides/transformations.md) | the silent-change report, and the DEFAULTs you must echo back |
| [Settings](https://github.com/wave-rf/chtypes/blob/main/docs/guides/settings.md) · [Discovery](https://github.com/wave-rf/chtypes/blob/main/docs/guides/discovery.md) | the four channels; asking a real server what profile to validate under |
| [Filters](https://github.com/wave-rf/chtypes/blob/main/docs/guides/filters.md) · [Multi-version](https://github.com/wave-rf/chtypes/blob/main/docs/guides/multi-version.md) | boolean expressions over rows; several ClickHouse versions in one process |
| [Support matrix](https://github.com/wave-rf/chtypes/blob/main/docs/support.md) · [Limitations](https://github.com/wave-rf/chtypes/blob/main/docs/limitations.md) | what works where; what chtypes declines to answer |

`go doc github.com/wave-rf/chtypes/go/chtypes` is the same surface with the full prose — every exported symbol carries its contract.

## Two things specific to this binding

**The linked build is not a consumer path.** `-tags chtypes_linked` adds package-level `CompileDDL`, `ValidateType`, `ParseSchema`, `BuiltVersion` and `SetDefaultSettings`, linking `-lchtypes` out of the core repository's build tree at compile time (`CGO_LDFLAGS` says where) and answering for exactly that one artifact. It is a development and rig instrument. `undefined: chtypes.CompileDDL` means the tag is missing.

**`SetDefaultSettings` exists only there.** It replaces a process-global that the row path reads by reference, so the ABI requires it to exclude everything else on the image — and a dlopen'd `Library` deliberately does not carry the symbol in its function-pointer table. Put those settings in the compile profile and the per-call map instead.

```sh
go build ./...                        # dlopen-only: what a consumer gets
go build -tags chtypes_linked ./...   # + the linked path
scripts/check-standalone.sh           # proves the first from a bare copy of go/
```

## Tests

`go test ./...`. Every test that needs an artifact **skips loudly by name** without a registry on the search path, and a suite that ran nothing fails — `scripts/check-standalone.sh` reads its verdict off a `go test -json` census rather than an exit code. `scripts/fetch.sh 25.8` fills the cache and the plain command then runs everything.

## License

Apache 2.0. The artifacts this package loads are **Elastic License 2.0** — a separate license, shipped inside each artifact release.
