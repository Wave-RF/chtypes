import { ChtypesError } from './errors.js';

/**
 * A setting value crosses the boundary as a JSON **string**, always.
 *
 * This is not cosmetic (docs/reference/c-abi.md §Settings): `chtypes_now_epoch_nanos` is a
 * 19-digit nanosecond epoch, which does not survive an IEEE double. Serialized
 * as a JSON *number* through a JS `number` it arrives as 1.7e+18 and the setting
 * is **silently ignored** — the batch keeps stamping the real wall clock and
 * nothing anywhere says so.
 *
 * The type therefore admits `string` and `bigint` and not `number`, and the
 * runtime rejects a `number` as well, because TypeScript is not present at the
 * call site of a JS consumer.
 */
export type SettingValue = string | bigint;

/**
 * A map of ClickHouse query/format settings plus the six `chtypes_*` keys
 * (clock control and admission budgets — docs/reference/c-abi.md §Settings), by name.
 *
 * Accepted everywhere settings travel: the compile profile
 * (`Library#compileDdl`), the per-call map (`Schema#row` / `Schema#rows`), the
 * process seed (`Library#setDefaultSettings`) and the engine's
 * MergeTree-namespace settings (`Schema#setEngine`). Precedence on the row
 * path: per-call > compile profile > library defaults > ClickHouse defaults —
 * except a TYPE GATE declared in the compile profile, which binds at compile
 * and then outranks the per-call map, as a real server's CREATE does.
 *
 * An unknown setting name is refused WHOLESALE with the server's own code 115
 * on every channel — nothing is applied, nothing is silently dropped.
 */
export type Settings = Readonly<Record<string, SettingValue>>;

/**
 * Serialize settings to the `settings_json` object the C ABI expects — every
 * value crossing as a JSON string.
 *
 * @param settings - the settings map; `undefined` encodes as `"{}"`, the
 *   structurally settings-free path.
 * @returns the JSON text to hand to a `chs_*` entry point.
 * @throws {ChtypesError} when a value is neither `string` nor `bigint` — a JS
 *   `number` is rejected at runtime because a 19-digit nanosecond epoch does
 *   not survive an IEEE double and the setting would be silently ignored.
 */
export function encodeSettings(settings?: Settings): string {
  if (settings === undefined) return '{}';
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(settings)) {
    if (typeof value === 'string') {
      out[key] = value;
    } else if (typeof value === 'bigint') {
      out[key] = value.toString();
    } else {
      throw new ChtypesError(
        `chtypes: setting ${JSON.stringify(key)} must be a string or a bigint, got ` +
          `${typeof value}. A JS number cannot carry a 19-digit nanosecond epoch: ` +
          `1700000000123456789 becomes 1.7e+18 and the setting is silently ignored ` +
          `(docs/reference/c-abi.md §Settings).`,
      );
    }
  }
  return JSON.stringify(out);
}
