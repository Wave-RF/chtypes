# chtypes — TypeScript SDK

**If this row were inserted into this table on this ClickHouse version, what would happen?** chtypes answers with ClickHouse's own code: the real C++ type machinery, vendored per release into a native library behind the frozen `chs_*` C ABI and reached here through `ffi-rs`. Nothing semantic is reimplemented, so *"what does ClickHouse do with `256` into a `UInt8`?"* is answered by ClickHouse rather than by a model of it. One peer binding among `{go, python, ts, rust}` — no language is privileged, and all four give one answer.

Node ≥ 22, ESM only. `ffi-rs` ships prebuilt for darwin arm64/x64 and linux arm64/x64 (gnu and musl), so there is **no build step**.

## Install

Two things: this package, and at least one **artifact** — the per-version native library it `dlopen`s at runtime.

```sh
pnpm add @wavehouse/chtypes
npx @wavehouse/chtypes fetch 25.8
```

The fetch lands in `~/.cache/chtypes/artifacts/<os>-<arch>/25.8/` — the per-user cache every chtypes binding reads by default — after checking an ed25519 signature over the release and the sha256 of every byte. `$CHTYPES_REGISTRY` overrides it.

## Quickstart

```ts
import { Format, Registry } from '@wavehouse/chtypes';

const registry = new Registry();     // walks the search path
const lib = registry.for('25.8');    // a line or an exact patch; never a nearest match
const schema = lib.compileDdl('x UInt8, ts DateTime DEFAULT now()');

const batch = schema.rows(Format.JSONEachRow, Buffer.from('{"x":256}'));
const row = batch.rows[0];
console.log(batch.outcome);              // accepted
console.log(row.values[0].text);         // 0             — what would actually be stored
console.log(row.transformed[0]?.reason); // overflow_wrap — which is the product
console.log(row.substituted[0]?.column); // ts — send it explicitly in the real INSERT

schema.close();
```

The row is **accepted** and `256` is silently stored as `0`. That report — `transformed` — is the one derived answer in the system and the reason it exists.

`ts` was substituted rather than stored: send every substituted column as an explicit value in the real INSERT, or the server re-evaluates `now()` at its own instant and your preview is not what landed.

## Three outcomes, and conflating any two is a bug

A bad **row** is a verdict, not an exception: `outcome` becomes `'rejected'` with ClickHouse's own code and message. Exceptions are for schema-level answers — `compileDdl`, `setEngine`, `setTtl`, `validateType` — and there the class *is* the verdict.

- `SchemaError` — the server refused, and `code` is a real ClickHouse code.
- `UnsupportedError` — chtypes declines to guess, and a real server might well have accepted. **Fall back to the server.**
- The two are **peers**: a decline never satisfies `instanceof SchemaError`.

## Documentation

| | |
|---|---|
| [Quickstart](https://github.com/wave-rf/chtypes/blob/main/docs/quickstart.md) | the same program in all four languages |
| [TypeScript API reference](https://github.com/wave-rf/chtypes/blob/main/docs/reference/ts.md) | every symbol, the C entry point under it, what it returns and what it throws |
| [Artifacts](https://github.com/wave-rf/chtypes/blob/main/docs/guides/artifacts.md) | getting one, where it lands, verifying and pinning it |
| [Batches](https://github.com/wave-rf/chtypes/blob/main/docs/guides/batches.md) | always `rows`, and the two bad-row policies |
| [Transformations](https://github.com/wave-rf/chtypes/blob/main/docs/guides/transformations.md) | the silent-change report, and the DEFAULTs you must echo back |
| [Settings](https://github.com/wave-rf/chtypes/blob/main/docs/guides/settings.md) · [Discovery](https://github.com/wave-rf/chtypes/blob/main/docs/guides/discovery.md) | the four channels; asking a real server what profile to validate under |
| [Filters](https://github.com/wave-rf/chtypes/blob/main/docs/guides/filters.md) · [Multi-version](https://github.com/wave-rf/chtypes/blob/main/docs/guides/multi-version.md) | boolean expressions over rows; several ClickHouse versions in one process |
| [Support matrix](https://github.com/wave-rf/chtypes/blob/main/docs/support.md) · [Limitations](https://github.com/wave-rf/chtypes/blob/main/docs/limitations.md) | what works where; what chtypes declines to answer |

## Four things specific to this binding

**`using` is a syntax error on Node 22**, this package's own floor. Explicit resource management needs TypeScript to downlevel it for you, or plain JavaScript on Node ≥ 24. `close()` works everywhere and every disposable here has both, so portable examples use `close()`.

**A settings value may be a `string` or a `bigint`, never a `number`.** The runtime rejects a `number`, because TypeScript is not present at a JS consumer's call site and a 19-digit `chtypes_now_epoch_nanos` does not survive an IEEE double — sent as a JSON number the setting would be silently ignored.

**`for()` never fetches; `open()` can.** `Registry#for(v)` is synchronous and resolves from the search path alone. `await registry.open(v)` is its async twin, and with `{ autofetch: true }` (or `CHTYPES_AUTOFETCH=1`) it fetches a missing line first. Off by default: a production process must not begin a 250 MB download inside a request.

**`worker_threads` is the boundary.** Two JS threads share one dlopen'd image and one set of C globals, which no per-isolate counter can see. Seed default settings before starting workers, and do not share a `Schema` across them.

## Tests

`pnpm test`. Tests that need an artifact **skip loudly by name** without a registry on the search path, and a suite that ran nothing fails. The fetch suite runs offline against the miniature releases in `tests/fixtures/fetch/`.

Working against a checkout: build `dist/` first (`pnpm install && pnpm build`) — that is what `package.json#exports` serves. If a linked copy goes stale, note that `pnpm install --force` does **not** relink; remove `node_modules` and reinstall.

## License

Apache 2.0. The artifacts this package loads are **Elastic License 2.0** — a separate license, shipped inside each artifact release, and `fetch` says so once.
