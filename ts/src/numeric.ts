/**
 * Exact numeric comparison. `Decimal(76, 24)`'s extra digits, a quoted 64-bit
 * integer and an `Int256` all have to compare by value and never through a
 * double: `18446744073709551615` must not become `18446744073709552000`, and an
 * `Int256` must not become `-5.78960446186581e+76`. The reference implementation
 * uses `math/big.Rat`; this is the same comparison over `BigInt`.
 */

/**
 * Whitespace is exactly JSON's four bytes (docs/reference/bindings.md §detectors) — never
 * `String.prototype.trim()`, whose `WhiteSpace` set includes `<ZWNBSP>`.
 * `trim()` here made `<BOM>42` numeric text, and the detector then reported 12
 * `reformat` transforms on CSV/TSV fields that were stored exactly as supplied.
 * The reference implementation's `big.Rat.SetString` tolerates no surrounding
 * whitespace at all, so this is the narrower of the two readings.
 */
function trimJsonSpace(s: string): string {
  let from = 0;
  let to = s.length;
  const space = (c: string): boolean => c === ' ' || c === '\t' || c === '\n' || c === '\r';
  while (from < to && space(s[from]!)) from++;
  while (to > from && space(s[to - 1]!)) to--;
  return s.slice(from, to);
}

/** Fold ClickHouse's denormal spellings onto one of "nan" / "inf" / "-inf". */
export function denormal(s: string): string {
  switch (trimJsonSpace(s).toLowerCase()) {
    case 'nan':
    case '__nan__':
    case '-nan':
      return 'nan';
    case 'inf':
    case 'infinity':
    case '__inf__':
    case '+inf':
      return 'inf';
    case '-inf':
    case '-infinity':
    case '__-inf__':
      return '-inf';
    default:
      return '';
  }
}

/**
 * A decimal (optionally exponential) literal as an exact rational, or null when
 * the text is not a number. The exponent is bounded: a gateway parses
 * tenant-supplied text, and 1e1000000000 must not become a 10^9-digit integer.
 */
const MAX_ABS_EXPONENT = 100_000;
const DECIMAL = /^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/;

export function parseRational(text: string): { num: bigint; den: bigint } | null {
  const s = trimJsonSpace(text);
  if (s === '') return null;
  const m = DECIMAL.exec(s);
  if (m === null) return null;
  const sign = m[1] ?? '';
  const int = m[2] ?? '';
  const frac = m[3] ?? '';
  const expText = m[4];
  if (int === '' && frac === '') return null;

  const exp = expText === undefined ? 0 : Number(expText);
  if (!Number.isSafeInteger(exp) || Math.abs(exp) > MAX_ABS_EXPONENT) return null;

  let num = BigInt((int === '' ? '0' : int) + frac);
  let den = 10n ** BigInt(frac.length);
  if (exp > 0) num *= 10n ** BigInt(exp);
  else if (exp < 0) den *= 10n ** BigInt(-exp);
  if (sign === '-') num = -num;
  return { num, den };
}

/** Is this text a number (or a denormal token)? */
export function isNumericText(text: string): boolean {
  if (text === '') return false;
  if (denormal(text) !== '') return true;
  return parseRational(text) !== null;
}

/** Strip the JSON quoting ClickHouse uses for 64-bit integers and datetimes. */
export function trimQuotes(s: string): string {
  return s.replace(/^"+|"+$/g, '');
}

/** Exact numeric equality of two renderings, denormals included. */
export function numEqual(a: string, b: string): boolean {
  const x = trimQuotes(a);
  const y = trimQuotes(b);
  if (x === y) return true;
  const dx = denormal(x);
  const dy = denormal(y);
  if (dx !== '' || dy !== '') return dx === dy;
  const ra = parseRational(x);
  const rb = parseRational(y);
  if (ra === null || rb === null) return false;
  return ra.num * rb.den === rb.num * ra.den;
}
