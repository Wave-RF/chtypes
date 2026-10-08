# chtypes — TypeScript SDK

> **2.0.0-dev: UNSTABLE, staging only, not for production.** This is the ABI v2 development binding (public issue #511). It speaks ABI v2's unstable description, pins its dev fingerprint and refuses a library with any other ("update your dev SDK"). It fetches only from the staging dev channel (`https://registry-staging.wavehouse.dev/chtypes/v2-dev`) and trusts only the staging key; `CHTYPES_ARTIFACTS_URL`, `CHTYPES_TRUSTED_KEYS` and `CHTYPES_ALLOW_UNSIGNED` are ignored, each with one warning; `--lock`, `--frozen` and `--update` (and the `frozen`, `lockPath`, `lockWrite` and `update` options) are refused, because a dev build is replaceable and a superseded one expires after 14 days. Its cache is `${XDG_CACHE_HOME:-~/.cache}/chtypes/v2-dev`, or `<CHTYPES_CACHE>/v2-dev` under an explicit cache, which no 1.x SDK reads. A dev SDK prefers the newest build of its own fingerprint: it resolves `<tag>--fp-<its fingerprint>` first and the tag only when no such build exists, and its cache lookups ignore another dev SDK's builds in a shared cache, so a newer dev fingerprint never strands it (§3 of [`docs/guides/fetch-v1.md`](../docs/guides/fetch-v1.md)). Every vocabulary has rule r3's `unknown(n)` member: an unlisted value is kept as it came, and its `*Known` function (`outcomeKnown`, `verdictKnown`, ...) is false for it. The rules are r1 to r6 of [`docs/reference/abi-v2.md`](../docs/reference/abi-v2.md). For production, use the 1.x package, `@wavehouse/chtypes@latest`.
>
> An explicit cache directory gets the `v2-dev` subroot whether it comes from `CHTYPES_CACHE` or from the `cacheDir` fetch option: the layout is under `<cacheDir>/v2-dev`, never in the directory itself, so code that inspects or pre-populates a cache looks there; `cacheRoot` and `searchDirs` return the resolved paths. `CHTYPES_OFFLINE=1` is the environment twin of the `offline` fetch option and `--offline`: the cache only, no request.
>
> <!-- remove-at-v2-lock -->v2-dev builds before the one that carries this contract (its release notice names it) can answer `ok` with `t` verdicts after a refused row. On those builds, when a body or block holds more than one row, treat every verdict as `d` if `errors` is non-empty.<!-- /remove-at-v2-lock -->

**If this row were inserted into this table on this ClickHouse version, what would happen?** chtypes answers with ClickHouse's own code: the real C++ type machinery, vendored per release into a native library behind the frozen `chs_*` C ABI and reached here through `ffi-rs`. Nothing semantic is reimplemented, so _"what does ClickHouse do with `256` into a `UInt8`?"_ is answered by ClickHouse rather than by a model of it. One peer binding among `{go, python, ts, rust}` — no language is privileged, and all four give one answer.

Node ≥ 22, ESM only. `ffi-rs` ships prebuilt for darwin arm64/x64 and linux arm64/x64 (gnu and musl), so there is **no build step**.

## Install

Two things: this package, and at least one **artifact**, the per-version native library it `dlopen`s at runtime. The fetch layer (`src/ocifetch`) resolves a version request to a signed artifact in its cache, verifies the signature and the library's digest, and hands the result to the loader; a `Registry` turns on fetching with `autofetch` (or `CHTYPES_AUTOFETCH=1`).

```sh
pnpm add @wavehouse/chtypes@dev
```

The dev builds are published under npm's `dev` dist-tag, so a plain `pnpm add @wavehouse/chtypes` never installs one: name the tag, or the exact `2.0.0-dev.N` version you want.

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

`pnpm test`. The stub-backed suites under `test/abi2` need `$CHTYPES_ABI2_STUBS` (the `v2/` directory `scripts/abi-v1/build-stubs.sh --out DIR` writes) and skip loudly by name without it; the decoder, settings, setup and dev-channel suites need nothing.

Working against a checkout: build `dist/` first (`pnpm install && pnpm build`), which is what `package.json#exports` serves.

## License

Apache 2.0. The artifacts this package loads are **Elastic License 2.0**, a separate license shipped inside each artifact release.
