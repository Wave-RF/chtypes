# TypeScript API reference

```ts
import { Format, Registry, setup } from '@wavehouse/chtypes';
```

`@wavehouse/chtypes` runs on Node 22.21 or later and is **ESM only**. Native calls go through `ffi-rs`, prebuilt for darwin arm64/x64 and linux arm64/x64 (gnu and musl), so there is no build step. This page lists what the package ships and where TypeScript spells something its own way; every operation's meaning, the types, the error table and the document fields are [`bindings-v1.md`](bindings-v1.md), which is the contract and is not restated here. The cross-language gaps in 1.0 are in the "Known gaps in 1.0" section of [`limitations.md`](../limitations.md).

## How to read this

A bad **row** is never an exception: the verdict is `RowResult#outcome`, one of `accepted`, `accepted_poisoned`, `rejected`, `skipped`, `unsupported`. Exceptions are for questions that could not be asked, and there the class is the verdict:

- `SchemaError`: the server would refuse; `chCode` and `chName` are ClickHouse's own.
- `UnsupportedError`: this build declines to answer. Fall back to the server.
- `UsageError`: caller misuse, whichever side caught it (a closed object, a refused version spelling, a zone given twice).
- `InternalError`: a library bug, or a document that does not decode.

The four are **peers** under the abstract `CallError`: a decline never satisfies `instanceof SchemaError`. Loader and fetch errors are `ArtifactError`s (`ArtifactIncompatibleError`, `ArtifactCorruptError`, `ArtifactMissingError` and the rest of the fetch family); `ArtifactError` extends `ChtypesError`, as the other bindings' hierarchies have it, so catching `ChtypesError` (`e instanceof ChtypesError`) takes the fetch and loader errors along with the call errors. `ArtifactError` carries `code` (an `ErrorCode`, one of the generated `CODE_*` constants) and a loader refusal's `reason`, `path`, `want` and `got`, which a fetch error leaves `undefined`.

## Setup, registry and library

- `setup({ timezone?, defaults? })` records the image zone and the default settings once, before the first open. A second call with the same values is a no-op; a different one is a `UsageError`. It is per isolate: every worker thread calls it identically.
- `await Registry.open({ fetch?, autofetch?, preload? })` constructs a registry. It opens nothing except each `preload` request. `autofetch` defaults to `CHTYPES_AUTOFETCH`. There is no `close`: a registry owns nothing.
- `await registry.for(request)` returns the `Library` for a request (two, three or four parts, no `v` prefix and no channel suffix). It resolves against the installed set without the network, and fetches only with `autofetch` on. The same request returns the same `Library` for the registry's life.
- `await registry.fetch(request)` installs the build a request names without opening it, as `chtypes fetch` does, and returns its `Resolved`: no image is loaded and no `setup` is needed, and a concurrent `registry.for` of the request shares the one fetch (#492).
- `await registry.installed()` lists the installed builds (`Resolved[]`); `registry.libraries()` lists the libraries this registry has opened.
- `openUnverified(path, { allow: true })` opens a local build with no signed statement. It also needs `CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1`, and a `Library` it returns has no `resolved`.
- `library.buildInfo`, `.version`, `.minor`, `.path` and `.resolved` say what was loaded. A `Library` has no `close`, and no `Symbol.dispose`: an image is never unloaded.
- `library.compileTable(createTable, { settings?, sessionTimezone? })` compiles exactly one `CREATE TABLE` statement into a `Schema`. The engine, TTL and partition key are part of that statement.
- `library.validateType`, `quoteIdentifier`, `quoteIdentifierIfNeeded`, `quoteLiteral`, `errorCodes`, `discoverQuery`, `discoverColumns` and `liveHandles` are one call each over the library.

## Schema, Filter and Block

- `schema.describe()` returns the compiled columns. `schema.row(format, body, options?)` answers one row and `schema.rows(format, body, options?)` a whole body, with `RowOptions { settings?, sessionTimezone?, columns? }` and `RowsOptions` adding `rowFilter?`, `exportFormat?` and `docFlags?`.
- `schema.compileFilter(expr, { params?, settings?, sessionTimezone? })` returns a `Filter`; `filter.rows(format, body, options?)` evaluates over a body and `filter.eval(block)` over a parsed one. `schema.parseBlock(format, body, options?)` returns a `Block`.
- `close()` on a `Schema`, `Filter` or `Block` is idempotent and any order is safe. `Symbol.dispose` is also defined, but **`using` is a syntax error on Node 22**, so portable code calls `close()`. An abandoned handle is freed by its finalizer.
- Using a closed object is a `UsageError`. The per-call zone is the `session_timezone` key of `settings` and nothing more; passing both spellings is a `UsageError`.

## Bytes, settings and results

- Every name, statement, message and rendered value is a `Buffer`, never assumed to be UTF-8. Input is `BytesIn` (`Uint8Array | string`; a string is encoded as UTF-8), and a body is a `Uint8Array` and never a string.
- `Settings` is `Readonly<Record<string, string>>`: values are strings and only strings, and a number is a `TypeError`.
- Results are `RowResult`, `BatchResult`, `FilterResult`, `Value`, `Transform`, `Computed`, `EngineCell`, `Span`, `Framing`, `SchemaDescription`, `Discovery` and `ErrorCodeTable`, decoded one to one from the library's documents. `BatchResult#exportDeclined` is a `Buffer`, read from `export_declined` or `export_declined_b64`; both forms in one document is an `InternalError`.
- The vocabularies are `Format`, `Outcome`, `FilterOutcome`, `Verdict`, `Reason`, `Source`, `DefaultKind` and `DocFlags`. Their facts are free functions generated from the description: `formatChName`, `verdictAnswered`, `reasonLossy` and `sourceIsStored`.

## Fetching artifacts

The package ships the fetch layer's seam and a command; the wire contract is [`fetch-v1.md`](../guides/fetch-v1.md). A `Registry` calls the seam for you, so most programs never touch it. The command is `bin: chtypes`:

```sh
npx @wavehouse/chtypes fetch 26.8       # also: fetch --all, verify, list, where, resolve, prune
```

`fetch` takes `--platform`, `--cache`, `--lock <file>`, `--frozen`, `--offline` and `--update` (which requires `--lock` and refuses `--frozen` and `--offline`); `verify`, `list` and `where` take `--cache`, `list` also `--offline`, and `where` also `--all` (every directory searched, the cache root first, one per line). `resolve <spelling>` takes `--json`, `--cache` and `--offline`, and prints what the spelling resolves to on every platform, verified, installing nothing (#493); `prune` takes `--line`, `--keep`, `--dry-run` and `--cache`, and removes each line's superseded builds, never one a running process holds (#494). Their output is [`fetch-v1.md`'s "The command line"](../guides/fetch-v1.md#the-command-line). The registry base comes only from `CHTYPES_ARTIFACTS_URL`. Exit statuses come from the `errors` table of [`spec/fetch-v1/constants.json`](../../spec/fetch-v1/constants.json), with usage errors exiting 2. `CHTYPES_ARTIFACTS_URL`, `CHTYPES_CACHE`, `CHTYPES_DOWNLOAD_TOKEN`, `CHTYPES_TRUSTED_KEYS`, `CHTYPES_ALLOW_UNSIGNED`, `CHTYPES_TARGET` and `CHTYPES_AUTOFETCH` are read as `fetch-v1.md` says, and an option the caller passes always wins over its variable.

`chtypes --help` and `-h` print the usage to stdout and exit 0 wherever they appear; a usage error prints to stderr and exits 2. `chtypes --version` prints `chtypes <version>` and a newline (`0.0.0-dev` for an untagged build). `list` prints one flat line per entry, with no header: first `installed <version> <platform> <dir>` for each installed build, then, unless `--offline`, `published <spelling> support unknown` for each tag in the registry's `tags/list`. `--platform` belongs to `fetch` alone; on `verify`, `list` or `where` it is a usage error. `scripts/check-cli-parity.sh` holds all four bindings' commands to these rules.
