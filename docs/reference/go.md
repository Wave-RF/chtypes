# Go API reference

```go
import "github.com/wave-rf/chtypes/go/chtypes"
```

Module path `github.com/wave-rf/chtypes/go`: lowercase, the Go norm, frozen since the first tag; what stays provisional until 1.0 is in [`support-v1.md`](../support-v1.md#pre-10). `go doc github.com/wave-rf/chtypes/go/chtypes` is the same surface with the full prose; every exported symbol carries its contract.

This page lists only what is Go's own: the spellings, the options and the error types. The operations, the document shapes, the vocabularies and the error classes are the same in all four bindings and are specified once, in [`bindings-v1.md`](bindings-v1.md); the fetch wire contract is [`fetch-v1.md`](../guides/fetch-v1.md). Known limits of 1.0 are in [Known gaps in 1.0](../limitations.md#known-gaps-in-10).

## Two builds, one package

The default build is **dlopen-only**: it compiles with cgo (for `dlfcn`) but links nothing, includes no header and needs no build tree. That is what a consumer's `go get` produces.

```sh
go build ./...                        # dlopen-only: what a consumer gets
go build -tags chtypes_linked ./...   # + chtypes.OpenLinked (needs the artifact producer's build tree via CGO_LDFLAGS)
```

`OpenLinked()` is the whole linked surface: the same `Library` over the same code path, answering for the one library linked at build time. It is a development and testing instrument, not a consumer path.

## Reading the errors

Row and batch verdicts are **never** Go errors: they are `Outcome` in the result. Go errors are for the call as a whole, and each is a peer type, so a decline can never satisfy `errors.As` against the refusal type ([`bindings-v1.md` §4](bindings-v1.md#4-errors)).

| type                | class                                                                                                                     |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `*SchemaError`      | `CHS_REJECTED`: ClickHouse refused. `ChCode` is a real ClickHouse code, `ChName` its name in this build                   |
| `*UnsupportedError` | `CHS_DECLINED`: this build declines. Fall back to the server; never report it as a rejection                              |
| `*UsageError`       | `CHS_INVALID_ARGUMENT`: misuse, such as a closed object, a refused spelling or a conflicting `Setup`                      |
| `*InternalError`    | `CHS_INTERNAL`, or a document that does not decode                                                                        |
| `*ArtifactError`    | a fetch or load failure, with a `Code` (one of ten) and a sentinel per code for `errors.Is`, such as `ErrArtifactMissing` |

All four call types embed `CallError`, whose five fields (`Status`, `ChCode`, `ChName`, `Message`, `Column`) read directly; `AsCallError` reads them from any of the four. `Message` and `Column` are byte strings, and `Error()` is the one lossy display form.

## The Go spellings

Go's names for the operations in [`bindings-v1.md` §2](bindings-v1.md#2-the-operations), with the optional inputs as functional options.

| Symbol                                                                            | What it is                                                                                                                                                                                          |
| --------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Setup(SetupOptions{Timezone, Defaults})`                                         | records the process setup, once and first; a different setup later is a `*UsageError`                                                                                                               |
| `NewRegistry(opts...)`, `WithFetchOptions`, `WithAutoFetch`, `WithPreload`        | construct a registry; it opens nothing. Autofetch defaults to `CHTYPES_AUTOFETCH=1`, and is otherwise off                                                                                           |
| `Registry.For(request)`, `ForContext(ctx, request)`, `Installed()`, `Libraries()` | open a version (two, three or four parts), what the fetch layer holds, what this registry has open. No `Close`: nothing is ever unloaded                                                            |
| `OpenUnverified(path, allow)`                                                     | open a local build without the signature steps; needs `allow` AND `CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1`                                                                                              |
| `Library.{Version, Minor, Path}`, `BuildInfo()`, `Resolved()`                     | identity from `build_info`, never derived; `Resolved()` is nil for an unverified or linked open                                                                                                     |
| `Library.{ValidateType, QuoteIdentifier, QuoteIdentifierIfNeeded, QuoteLiteral}`  | the library's own canonicalization and quoting; bytes in a Go `string`                                                                                                                              |
| `Library.{ErrorCodes, DiscoverQuery, DiscoverColumns, LiveHandles}`               | this build's error-code table, the discovery query and its column reader, and the diagnostic handle counts                                                                                          |
| `Library.CompileTable(createTable, opts...)`                                      | compile exactly one `CREATE TABLE` statement; options `WithSettings`, `WithSessionTimezone`                                                                                                         |
| `Schema.{Describe, Row, Rows, CompileFilter, ParseBlock, Close}`                  | describe, one row, a whole body, a filter, a parsed block, release. Options `WithSettings`, `WithSessionTimezone`, `WithColumns`, `WithRowFilter`, `WithExport`, `WithDocFlags`, `WithFilterParams` |
| `Filter.{Rows, Eval, Close}`, `Block.Close`                                       | evaluate over a body or a block; release                                                                                                                                                            |
| `Verdict.Answered()`                                                              | true for `VerdictTrue` and `VerdictFalse` only: **fail closed on the other two**                                                                                                                    |

A string or byte-string input is a Go `string`, and a body is `[]byte`. A `Value`'s optional raw bytes (`Value.Value`, and the same field on `Computed` and `EngineCell`) are a `*string`, nil where the document carries none, so an absent value differs from an empty one; an export payload is `[]byte`. Settings and query parameters are `map[string]string`, serialized verbatim.

`Row` is sugar for a call site that semantically expects one row. On a multi-row body it returns the first and ignores the rest, so it is the wrong call for anything wire-facing; see [batches](../guides/batches.md).

## The exit status of a failure

The library carries no exit status. `ErrorCode.ExitCode()` is a method of the shared code vocabulary, generated from `spec/fetch-v1/constants.json` (the table is [`fetch-v1.md` §8](../guides/fetch-v1.md#8-errors)), and the command below is the only caller that turns an error into a process status.

## The command

`go/cmd/chtypes` is the artifact tool every binding spells identically, over the fetch layer of [`fetch-v1.md`](../guides/fetch-v1.md). It runs without installing anything:

```sh
go run github.com/wave-rf/chtypes/go/cmd/chtypes@latest fetch 26.8
```

| command                          | meaning                                                                                                                                                        |
| -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `fetch <spelling>... \| --all`   | resolve, verify and install each request (two, three or four parts), printing the installed directory of each alone on stdout. `--all` is every published line |
| `fetch ... --platform <os-arch>` | another platform than this host's, or `$CHTYPES_TARGET`                                                                                                        |
| `fetch ... --lock <file>`        | resolve normally, then write the lock (schema 3). With `--frozen`, the lock to enforce; default `chtypes.lock`                                                 |
| `fetch ... --frozen`             | fetch exactly what the lock pins, by digest, with no resolution; a request the lock does not pin is `CHTYPES_ARTIFACT_PINNED`                                  |
| `fetch ... --update`             | re-resolve every locked request and rewrite the lock; requires `--lock`, and with `--frozen` it is a usage error                                               |
| `fetch ... --offline`            | read the cache only; a miss is `CHTYPES_ARTIFACT_MISSING`                                                                                                      |
| `verify`                         | re-hash every installed library against its own recorded digests                                                                                               |
| `list`                           | what is installed, and the published lines from the registry's `tags/list`; `--offline` skips the registry. Support is unknown, never "unsupported"            |
| `where`                          | the cache root (`$CHTYPES_CACHE`, else `${XDG_CACHE_HOME:-~/.cache}/chtypes/v1`); `--cache <dir>` overrides it on every command                                |

Exit statuses: 0 for success, 2 for a usage error (an unknown command or flag, a refused spelling, `--all` with an argument), and for a failure the status of its error code in the table above (1 for the four verification failures, 3 unreachable, 4 unpublished, 5 unauthorized, 6 forbidden, 7 incompatible source, 8 incompatible artifact).

Environment: `CHTYPES_ARTIFACTS_URL` (the bases, comma separated), `CHTYPES_CACHE`, `CHTYPES_TRUSTED_KEYS` (replaces the embedded release key), `CHTYPES_ALLOW_UNSIGNED=1` (skip verification, loudly), `CHTYPES_DOWNLOAD_TOKEN`, `CHTYPES_TARGET`.

`chtypes --help` and `-h` print the usage to stdout and exit 0 wherever they appear; a usage error prints to stderr and exits 2. `chtypes --version` prints `chtypes <version>` and a newline (`0.0.0-dev` for an untagged build). `list` prints one flat line per entry, with no header: first `installed <version> <platform> <dir>` for each installed build, then, unless `--offline`, `published <spelling> support unknown` for each tag in the registry's `tags/list`. `--platform` belongs to `fetch` alone; on `verify`, `list` or `where` it is a usage error. `scripts/check-cli-parity.sh` holds all four bindings' commands to these rules.

## Concurrency and memory

Sizing a caller-side semaphore, accounting for `GOMEMLIMIT` against a `dlopen`'d artifact's own C++ heap, per-handle RSS and compile cost figures, and why `Close` does not lower RSS: [`multi-version.md`](../guides/multi-version.md#go-sizing-concurrent-handles).

## See also

- [`bindings-v1.md`](bindings-v1.md): the normative shape all four bindings implement.
- [`abi-v1.md`](abi-v1.md): the C ABI underneath.
- [`examples/go/`](../../examples/go/README.md): the tour, section for section with the other three bindings.
