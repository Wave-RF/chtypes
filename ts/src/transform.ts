/**
 * Silent-transformation detection — a port of `go/chtypes/transform.go`.
 *
 * ClickHouse has no notion of "I changed your value": `readIntText` wraps mod
 * 2^N, `SerializationDateTime` truncates, `parseUUID` maps every non-hex byte to
 * 0xff, and all of it returns success. So the fact of a change has to be
 * established from outside, and this is the one part of chtypes that is derived
 * rather than executed by ClickHouse — which makes it the one part a binding can
 * get wrong without the C library noticing. Omitting it scores 0 % recall on the
 * arbiter's transformation axis regardless of everything else.
 *
 * Two independent detectors run and their union is reported:
 *
 *  1. **Supplied vs stored** — the raw input text against ClickHouse's rendering
 *     of what it kept. Models nothing; catches changes a widened type makes
 *     identically (2024-02-30 -> 2024-03-01) and types with no wider type to
 *     compare against (Float64, Int256, String).
 *  2. **Reference type** — the C layer already parsed each field a second time
 *     through a structurally identical type with widened leaves and reported it
 *     as `ref` / `ref_type`. This names the reason precisely (an overflow wrap
 *     versus a decimal truncation) and still fires where the supplied text is
 *     not comparable (a CSV field, a base64 blob).
 *
 * Neither reimplements a coercion rule: the first compares two values already in
 * hand, the second compares two ClickHouse answers.
 *
 * Both compare **bytes**. A supplied field and a stored `String` can each be any
 * byte sequence, and two different byte sequences that both decode to U+FFFD are
 * not the same value — deciding on decoded text would silently merge them, and
 * `equivalent`'s fast path would then call a real change identical.
 */

import { parseJsonValue, type Json } from './json.js';
import { denormal, isNumericText, numEqual, parseRational, trimQuotes } from './numeric.js';
import type { ColumnDoc, Transform } from './results.js';

/** The stable reason vocabulary. The harness groups on these strings. */
export const Reason = {
  OverflowWrap: 'overflow_wrap',
  NullToDefault: 'null_to_default',
  NullLoss: 'null_loss',
  DecimalTruncate: 'decimal_truncate',
  DateClamp: 'date_clamp',
  DateTimeWrap: 'datetime_wrap',
  DateShift: 'date_shift',
  UuidMangle: 'uuid_mangle',
  IpMangle: 'ip_mangle',
  FloatPrecision: 'float_precision',
  LossyNumeric: 'lossy_numeric',
  FixedStringPad: 'fixedstring_pad',
  Emptied: 'emptied',
  ElementChanged: 'element_changed',
  EnumCoerce: 'enum_coerce',
  ValueChanged: 'value_changed',
  Poisoned: 'poisoned',
  DuplicateKeyDropped: 'duplicate_key_dropped',
  Reformat: 'reformat',
  DefaultFilled: 'default_filled',
  ZeroFilled: 'zero_filled',
  DefaultMaterialized: 'default_materialized',
  TtlExpired: 'ttl_expired',
  TtlColumnExpired: 'ttl_column_expired',
} as const;

/** One of the stable reason strings — see the `Reason` constant object. */
export type Reason = (typeof Reason)[keyof typeof Reason];

/**
 * Four reasons change how a value is written, or add one the row never carried,
 * without losing information. Everything else is a warning. All of them are
 * still reported: a preview must show the tenant what the table will hold, and
 * 1700000000 becoming "2023-11-14 22:13:20" is a visible change.
 */
const NON_LOSSY = new Set<string>([
  Reason.Reformat,
  Reason.DefaultFilled,
  Reason.ZeroFilled,
  Reason.DefaultMaterialized,
]);

/**
 * Whether a transform reason means information was LOST (a warning to raise to
 * the tenant) rather than merely re-spelled or filled in. False for exactly
 * four reasons — `reformat`, `default_filled`, `zero_filled`,
 * `default_materialized` — true for everything else, unknown strings included:
 * a reason nobody has audited must degrade to a warning, never to silence.
 * Mirrors `Transform#lossy`. Never throws.
 */
export function isLossyReason(reason: string): boolean {
  return !NON_LOSSY.has(reason);
}

const EMPTY = Buffer.alloc(0);
const UNREADABLE = Buffer.from('<unreadable>', 'utf8');

/**
 * One reported change. Built from bytes, with the text spellings derived — so a
 * consumer that can take bytes never sees a U+FFFD this binding invented.
 */
function transform(column: string, input: Buffer, stored: Buffer, reason: string): Transform {
  return {
    column,
    input: input.toString('utf8'),
    stored: stored.toString('utf8'),
    inputBytes: input,
    storedBytes: stored,
    reason,
    row: 0,
    lossy: isLossyReason(reason),
  };
}

/** Classify one column of a row document. */
export function classify(c: ColumnDoc): Transform[] {
  switch (c.src) {
    case 'skipped':
    case 'default_expr_unsupported':
    case 'default_volatile_unresolved':
    case 'default_pending':
      return [];
    default:
      break;
  }

  // The accept-then-poison class: ClickHouse stores a value it cannot read back.
  // Not a changed value — a destroyed one — but silent at insert time.
  if (c.poison) {
    return [transform(c.name, c.inputBytes, UNREADABLE, Reason.Poisoned)];
  }

  const stored = c.storedBytes;

  // A duplicate key whose later value ClickHouse discarded. Reported first
  // because the value comparison below sees only the value that survived.
  if (c.dupDropped) {
    return [transform(c.name, c.inputBytes, stored, Reason.DuplicateKeyDropped)];
  }

  // A column the row did not supply that ClickHouse populated. Input is empty —
  // an absent column has none — and the reason says where the value came from.
  switch (c.src) {
    case 'default':
      return [transform(c.name, EMPTY, stored, Reason.DefaultFilled)];
    case 'default_substituted':
      return [transform(c.name, EMPTY, stored, Reason.DefaultMaterialized)];
    case 'absent':
      return [transform(c.name, EMPTY, stored, Reason.ZeroFilled)];
    default:
      break;
  }

  // ---- detector 2: reference type. Run first because it names the reason.
  let refReason = '';
  if (c.refType !== '' && c.refBytes.length > 0 && !equivalent(c.base, stored, c.refBytes)) {
    refReason = reasonFor(c.base, c.stored, c.ref);
  }

  // ---- detector 1: supplied vs stored.
  let suppliedReason = '';
  if (c.src === 'input') {
    const sup = parseJsonValue(c.inputBytes);
    const st = parseJsonValue(stored);
    if (sup !== null && st !== null) {
      if (!sameValue(sup, st)) {
        suppliedReason = severity(sup, st);
      } else if (!sameKindDeep(sup, st)) {
        // Same value, different JSON type: "1024" -> 1024, or a number inside an
        // Array(Map(String,String)) coming back as a string. Nothing is lost, but
        // the preview and the table disagree on the type, and the check has to
        // recurse: the change is often inside a container.
        suppliedReason = Reason.Reformat;
      }
    } else if (c.nullInput && !c.nullable) {
      // A null the format layer replaced with a default. In CSV/TSV the raw text
      // is `\N`, which is not JSON, so it never reaches the branch above — and
      // this is the single largest lossy class.
      suppliedReason = Reason.NullToDefault;
    }
  }

  if (refReason === '' && suppliedReason === '') return [];

  // Prefer the reference detector's reason — it can tell an overflow wrap from a
  // decimal truncation — but never let it downgrade a lossy finding to reformat.
  let reason = refReason;
  if (reason === '' || (reason === Reason.Reformat && suppliedReason !== '')) {
    reason = suppliedReason;
  }
  // An unclassified numeric/temporal leaf (no reference-ladder entry in the
  // C layer): the precise detector never ran, so a visible change cannot be
  // vouched non-lossy. Claim lossy rather than hide a possible loss.
  if (c.refUnclassified && reason === Reason.Reformat) {
    reason = Reason.ValueChanged;
  }
  return [transform(c.name, c.inputBytes, stored, reason)];
}

// ------------------------------------------------------ supplied vs stored

/** A scalar's own bytes, and whether it was a JSON string rather than a number. */
function scalar(v: Json): { bytes: Buffer; isString: boolean } {
  switch (v.kind) {
    case 'number':
      return { bytes: v.bytes, isString: false };
    case 'string':
      return { bytes: v.bytes, isString: true };
    default:
      return { bytes: EMPTY, isString: false };
  }
}

function scalarText(v: Json): { text: string; isString: boolean } {
  const s = scalar(v);
  return { text: s.bytes.toString('utf8'), isString: s.isString };
}

function isNumericValue(v: Json): boolean {
  const { text } = scalarText(v);
  return text !== '' && isNumericText(text);
}

/**
 * An object's members as a map, later keys winning — which is what a JS object,
 * Go's `map[string]any` and the arbiter's `json.loads` all do, so the comparison
 * here means the same thing as the harness's. The raw bytes keep the duplicates
 * (`rawBytes`); only this comparison collapses them.
 *
 * Keys are mapped through latin1, the one JS encoding that round-trips arbitrary
 * bytes, so two keys that differ only outside UTF-8 stay distinct.
 */
function membersOf(v: Json): Map<string, Json> {
  const out = new Map<string, Json>();
  for (let i = 0; i < v.keys.length; i++) {
    out.set(v.keys[i]!.toString('latin1'), v.items[i]!);
  }
  return out;
}

/**
 * Equal after canonicalization, where a number and its decimal string spelling
 * are the same value (5 vs "5") but a float that has thrown away 60 digits of an
 * Int256 is not. Mirrors the arbiter's `_same_value`.
 */
function sameValue(a: Json, b: Json): boolean {
  if (a.kind === 'array') {
    if (b.kind !== 'array' || a.items.length !== b.items.length) return false;
    return a.items.every((x, i) => sameValue(x, b.items[i]!));
  }
  if (a.kind === 'object') {
    if (b.kind !== 'object') return false;
    const am = membersOf(a);
    const bm = membersOf(b);
    if (am.size !== bm.size) return false;
    for (const [k, av] of am) {
      const bv = bm.get(k);
      if (bv === undefined || !sameValue(av, bv)) return false;
    }
    return true;
  }
  if (a.kind === 'null' || b.kind === 'null') return a.kind === 'null' && b.kind === 'null';
  if (a.kind === 'bool') return b.kind === 'bool' && a.raw.equals(b.raw);
  if (b.kind === 'bool') return false;

  const x = scalar(a);
  const y = scalar(b);
  if (x.isString === y.isString && x.bytes.equals(y.bytes)) return true;
  const xt = x.bytes.toString('utf8');
  const yt = y.bytes.toString('utf8');
  // Denormals: ClickHouse renders them as the strings "nan" / "inf" / "-inf".
  if (denormal(xt) !== '' || denormal(yt) !== '') {
    return denormal(xt) === denormal(yt);
  }
  const ra = parseRational(xt);
  const rb = parseRational(yt);
  if (ra === null || rb === null) return false;
  return ra.num * rb.den === rb.num * ra.den;
}

/** Mirrors the arbiter's `_severity`. */
function severity(a: Json, b: Json): string {
  if (isNumericValue(a) && isNumericValue(b)) return Reason.LossyNumeric;
  if (a.kind === 'null' && b.kind !== 'null') return Reason.NullToDefault; // the arbiter's `null_filled`
  if (a.kind !== 'null' && b.kind === 'null') return Reason.NullLoss;

  const x = scalarText(a);
  const y = scalarText(b);
  if (x.isString && y.isString) {
    if (dateish(x.text) && dateish(y.text)) {
      return x.text.slice(0, 10) !== y.text.slice(0, 10) ? Reason.DateShift : Reason.Reformat;
    }
    if (x.text !== '' && y.text === '') return Reason.Emptied;
    return Reason.Reformat;
  }
  if (y.isString && denormal(y.text) !== '') return Reason.LossyNumeric;
  return Reason.Reformat;
}

/**
 * Compare JSON shape recursively. Two values can be equal by `sameValue` and
 * still differ in type somewhere inside — `[{"a": 1}]` stored as `[{"a": "1"}]`
 * is the same data with a different wire type, which a subscriber reading the
 * preview would see and the table would not have.
 */
function sameKindDeep(a: Json, b: Json): boolean {
  if (a.kind !== b.kind) return false;
  // Two strings that are "equal" only after denormal folding are still a visible
  // change: the row said "NaN" and the table holds "nan".
  if (a.kind === 'string') return a.bytes.equals(b.bytes);
  if (a.kind === 'array') {
    if (a.items.length !== b.items.length) return false;
    return a.items.every((x, i) => sameKindDeep(x, b.items[i]!));
  }
  if (a.kind === 'object') {
    const am = membersOf(a);
    const bm = membersOf(b);
    if (am.size !== bm.size) return false;
    for (const [k, av] of am) {
      const bv = bm.get(k);
      if (bv === undefined || !sameKindDeep(av, bv)) return false;
    }
    return true;
  }
  return true;
}

function dateish(s: string): boolean {
  if (s.length < 10) return false;
  for (let i = 0; i < 10; i++) {
    const c = s[i]!;
    if (i === 4 || i === 7) {
      if (c !== '-') return false;
    } else if (c < '0' || c > '9') {
      return false;
    }
  }
  return true;
}

// ---------------------------------------------------------- reference type

function reasonFor(base: string, stored: string, ref: string): string {
  if (base.startsWith('Enum')) return Reason.EnumCoerce;
  if (base.startsWith('Array') || base.startsWith('Tuple') || base.startsWith('Map')) {
    return Reason.ElementChanged;
  }
  if (base.startsWith('Decimal')) {
    return trimQuotes(ref).startsWith(trimQuotes(stored)) ? Reason.DecimalTruncate : Reason.OverflowWrap;
  }
  if (base.startsWith('DateTime')) return Reason.DateTimeWrap;
  if (base.startsWith('Date')) return Reason.DateClamp;
  // Time / Time64 (25.8+): the same truncation mechanism as DateTime64 —
  // sub-second ticks at the declared scale, whole seconds in Time.
  if (base.startsWith('Time')) return Reason.DateTimeWrap;
  if (base === 'UUID') return Reason.UuidMangle;
  if (base === 'IPv4' || base === 'IPv6') return Reason.IpMangle;
  if (base === 'Float32' || base === 'Float64' || base === 'BFloat16') return Reason.FloatPrecision;
  if (base.startsWith('FixedString')) return Reason.FixedStringPad;
  if (isIntFamily(base)) return Reason.OverflowWrap;
  return Reason.ValueChanged;
}

function isIntFamily(base: string): boolean {
  return base.startsWith('UInt') || base.startsWith('Int') || base === 'Bool';
}

/**
 * Whether two ClickHouse renderings mean the same value. Only formatting
 * differences are collapsed — anything else is a real change.
 *
 * Identity is decided on the bytes: a `FixedString` padded from `0xff` to
 * `0xff 0x00 0x00` is two different values that decode to the same lossy text,
 * and calling those equivalent would drop a real `fixedstring_pad`.
 */
function equivalent(base: string, storedBytes: Buffer, refBytes: Buffer): boolean {
  if (storedBytes.equals(refBytes)) return true;
  const stored = storedBytes.toString('utf8');
  const ref = refBytes.toString('utf8');
  // Bool renders as true/false; its Int256 reference renders as 1/0.
  if (base === 'Bool') return boolNorm(stored) === boolNorm(ref);
  // DateTime64 references widen the scale: ".123" vs ".123000000" — and so
  // do Time64's (Time/Time64 arrived in 25.8; same rendering shape).
  if (base.startsWith('DateTime') || base.startsWith('Time')) return trimFrac(stored) === trimFrac(ref);
  // Decimal references widen the scale: "2.50" vs "2.5000000".
  if (base.startsWith('Decimal') || isIntFamily(base) || base === 'Float32' || base === 'Float64' || base === 'BFloat16') {
    return numEqual(stored, ref);
  }
  if (base.startsWith('Array') || base.startsWith('Tuple') || base.startsWith('Map')) {
    return compactJson(stored) === compactJson(ref) || numListEqual(stored, ref);
  }
  return false;
}

function boolNorm(s: string): string {
  switch (s.replace(/^"|"$/g, '')) {
    case 'true':
    case '1':
      return '1';
    case 'false':
    case '0':
      return '0';
    default:
      return s;
  }
}

/**
 * Drop trailing zeros (and a bare dot) from a datetime's fractional part so
 * DateTime64(3) and its DateTime64(9) reference compare equal.
 */
function trimFrac(s: string): string {
  const i = s.lastIndexOf('.');
  if (i < 0) return s;
  let end = s.length;
  const quoted = end > 0 && s[end - 1] === '"';
  if (quoted) end--;
  const frac = s.slice(i + 1, end).replace(/0+$/, '');
  let out = s.slice(0, i);
  if (frac !== '') out += `.${frac}`;
  if (quoted) out += '"';
  return out;
}

/**
 * Compare two rendered containers element-wise as numbers, so [1,2] and
 * [1.000,2.000] agree while [1,0,3] and [1,256,3] do not.
 */
function numListEqual(a: string, b: string): boolean {
  const ta = tokenizeNums(a);
  const tb = tokenizeNums(b);
  if (ta.length !== tb.length || ta.length === 0) return false;
  for (let i = 0; i < ta.length; i++) {
    if (!numEqual(ta[i]!, tb[i]!)) return false;
  }
  return skeleton(a) === skeleton(b);
}

/** Pull the numeric literals out of a rendered container. */
function tokenizeNums(s: string): string[] {
  const out: string[] = [];
  let i = 0;
  while (i < s.length) {
    const c = s[i]!;
    if (c === '"') {
      i++;
      while (i < s.length && s[i] !== '"') {
        if (s[i] === '\\') i++;
        i++;
      }
      i++;
      continue;
    }
    if (c === '-' || c === '+' || (c >= '0' && c <= '9')) {
      let j = i + 1;
      while (j < s.length) {
        const d = s[j]!;
        if (d === '.' || d === 'e' || d === 'E' || d === '-' || d === '+' || (d >= '0' && d <= '9')) j++;
        else break;
      }
      out.push(s.slice(i, j));
      i = j;
      continue;
    }
    i++;
  }
  return out;
}

/**
 * Keep only structural punctuation, so two renderings that agree numerically but
 * differ in shape are still reported as different.
 */
function skeleton(s: string): string {
  let out = '';
  let inString = false;
  for (let i = 0; i < s.length; i++) {
    const c = s[i]!;
    if (inString) {
      if (c === '\\') i++;
      else if (c === '"') {
        inString = false;
        out += '"';
      }
      continue;
    }
    if (c === '"') {
      inString = true;
      out += '"';
      continue;
    }
    if ('[]{}(),:'.includes(c)) out += c;
  }
  return out;
}

function compactJson(s: string): string {
  let out = '';
  let inString = false;
  for (let i = 0; i < s.length; i++) {
    const c = s[i]!;
    if (inString) {
      out += c;
      if (c === '\\' && i + 1 < s.length) {
        i++;
        out += s[i]!;
      } else if (c === '"') {
        inString = false;
      }
      continue;
    }
    if (c === ' ' || c === '\t' || c === '\n' || c === '\r') continue;
    if (c === '"') inString = true;
    out += c;
  }
  return out;
}
