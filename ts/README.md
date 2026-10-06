# chtypes — TypeScript SDK

**If this row were inserted into this table on this ClickHouse version, what would happen?** chtypes answers with ClickHouse's own code: the real C++ type machinery, vendored per release into a native library behind the frozen `chs_*` C ABI and reached here through `ffi-rs`. Nothing semantic is reimplemented, so _"what does ClickHouse do with `256` into a `UInt8`?"_ is answered by ClickHouse rather than by a model of it. One peer binding among `{go, python, ts, rust}` — no language is privileged, and all four give one answer.

Node ≥ 22, ESM only. `ffi-rs` ships prebuilt for darwin arm64/x64 and linux arm64/x64 (gnu and musl), so there is **no build step**.

## Install

Two things: this package, and at least one **artifact**, the per-version native library it `dlopen`s at runtime. The fetch layer (`src/ocifetch`) resolves a version request to a signed artifact in its cache, verifies the signature and the library's digest, and hands the result to the loader; a `Registry` turns on fetching with `autofetch` (or `CHTYPES_AUTOFETCH=1`).

```sh
pnpm add @wavehouse/chtypes
```

## Quickstart

```ts
import { Format, Registry, setup } from '@wavehouse/chtypes';

setup({ timezone: 'UTC' });                  // optional; once, before the first open
const registry = await Registry.open({ autofetch: true });
const lib = await registry.for('26.8');      // two, three or four parts; never a nearest match
const schema = lib.compileTable('CREATE TABLE t (x UInt8, ts DateTime DEFAULT now()) ENGINE = Memory');

const batch = schema.rows(Format.JSONEachRow, Buffer.from('{"x":256}\n'));
const row = batch.rows[0];
console.log(batch.outcome);                    // accepted
console.log(row?.values[0]?.text.toString());  // 0             what would actually be stored
console.log(row?.transformed[0]?.reason);      // overflow_wrap which is the product

schema.close();
```

The row is **accepted** and `256` is silently stored as `0`. That report, `transformed`, comes from the library, which is the only place a ClickHouse rule lives.

## What a result is

Every field of a result is a field of the document the library returned, decoded one to one with the stock `JSON.parse`; nothing is computed in this package. Every name, SQL text, message and rendered value is **bytes** (`Buffer`), never assumed to be UTF-8: ClickHouse round-trips column names containing NUL and invalid UTF-8. `Value.isStored`, `Transform.lossy` and `verdictAnswered` are the description's own facts for the value a document carries.

## Errors

A bad **row** is a verdict, not an exception: `outcome` becomes `'rejected'` with ClickHouse's own code and message. The calls that can fail throw one of four peers, under the abstract `CallError`:

- `SchemaError`: the server would refuse; `chCode` and `chName` are ClickHouse's own.
- `UnsupportedError`: this build declines to answer, and a real server might well have accepted. **Fall back to the server.**
- `UsageError`: caller misuse, whichever side caught it (a closed object, a zone given twice, a refused version spelling).
- `InternalError`: a library bug, or a document that does not decode.

The four are **peers**: a decline never satisfies `instanceof SchemaError`. Loader and fetch errors are `ArtifactError`s: `ArtifactIncompatibleError`, `ArtifactCorruptError` (the fetch layer's and the loader's step 5 are one class), `ArtifactMissingError` and the rest of the fetch family.

## Documentation

|                                                        |                                                                                |
| ------------------------------------------------------ | ------------------------------------------------------------------------------ |
| [The v1 binding API](../docs/reference/bindings-v1.md) | every operation, type, error class and document decoder, in all four languages |
| [Fetching artifacts](../docs/guides/fetch-v1.md)       | resolving, verifying and caching an artifact                                   |

## Things specific to this binding

**`using` is a syntax error on Node 22**, this package's own floor. Explicit resource management needs TypeScript to downlevel it for you, or plain JavaScript on Node >= 24. `close()` works everywhere and every disposable here has both, so portable examples use `close()`.

**Settings values are strings, and only strings.** The map becomes a JSON object of string values and is passed verbatim; a `number` or a `boolean` is a `TypeError`. The per-call zone is the `session_timezone` key of the call's settings: pass `sessionTimezone`, or the key, never both.

**`Registry.open`, `for` and `installed` are async**, because the fetch layer is. Construction opens nothing; `for` resolves from the cache first and fetches only when `autofetch` is on.

**`worker_threads` is the boundary.** Every call runs synchronously on one thread, so there is nothing to share inside an isolate. A handle cannot cross to another worker: each worker opens its own objects over the shared image, and every worker must call `setup` identically.

## Tests

`pnpm test`. The stub-backed suites under `test/abi1` need `$CHTYPES_ABI1_STUBS` and skip loudly by name without it; the decoder, settings and setup suites need nothing.

Working against a checkout: build `dist/` first (`pnpm install && pnpm build`), which is what `package.json#exports` serves.

## License

Apache 2.0. The artifacts this package loads are **Elastic License 2.0**, a separate license shipped inside each artifact release.
