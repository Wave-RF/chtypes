/**
 * The fetch layer's JSON plumbing: a parser over bytes whose values remember the
 * exact slice they were cut from, with duplicate keys kept in source order (the
 * fetch layer's own duplicate-key refusals read them). The result documents of
 * the C layer no longer use it: they are decoded with the stock `JSON.parse`
 * (`./documents.ts`), because the library's documents are valid RFC 8259 JSON.
 * It is kept for the fetch layer's use only.
 */

import { isUtf8 } from 'node:buffer';
import { ArtifactCorruptError } from './ocifetch/errors.js';

/** The six JSON kinds. `number` keeps its text; nothing is ever converted. */
export type JsonKind = 'null' | 'bool' | 'number' | 'string' | 'array' | 'object';

const EMPTY = Buffer.alloc(0);
const NO_ITEMS: readonly Json[] = [];
const NO_KEYS: readonly Buffer[] = [];

/**
 * One parsed JSON value that still knows the exact bytes it came from.
 *
 * `raw` is the equivalent of Go's `json.RawMessage` — the verbatim source slice,
 * which is what any re-rendering of a stored value must use. `bytes` is a
 * scalar's own value: a string's **unescaped** bytes, or a number's / literal's
 * text. A container carries its members in `items` and, for an object, the
 * matching unescaped `keys` — **duplicates included, in source order**.
 */
export class Json {
  constructor(
    readonly kind: JsonKind,
    readonly raw: Buffer,
    readonly bytes: Buffer = EMPTY,
    readonly items: readonly Json[] = NO_ITEMS,
    readonly keys: readonly Buffer[] = NO_KEYS,
  ) {}
}

/** True when these bytes are valid UTF-8 — i.e. when their text is exact. */
export function isValidUtf8(bytes: Uint8Array): boolean {
  return isUtf8(bytes);
}

// ------------------------------------------------------------------- the parser

const TAB = 0x09;
const LF = 0x0a;
const CR = 0x0d;
const SPACE = 0x20;
const QUOTE = 0x22;
const PLUS = 0x2b;
const COMMA = 0x2c;
const MINUS = 0x2d;
const DOT = 0x2e;
const SLASH = 0x2f;
const ZERO = 0x30;
const NINE = 0x39;
const COLON = 0x3a;
const UPPER_E = 0x45;
const ARRAY_OPEN = 0x5b;
const BACKSLASH = 0x5c;
const ARRAY_CLOSE = 0x5d;
const LOWER_B = 0x62;
const LOWER_E = 0x65;
const LOWER_F = 0x66;
const LOWER_N = 0x6e;
const LOWER_R = 0x72;
const LOWER_T = 0x74;
const LOWER_U = 0x75;
const OBJECT_OPEN = 0x7b;
const OBJECT_CLOSE = 0x7d;
const BACKSPACE = 0x08;
const FORM_FEED = 0x0c;

/** JSON's whitespace is exactly these four bytes — see `parseJsonValue`. */
function isSpace(c: number): boolean {
  return c === SPACE || c === TAB || c === LF || c === CR;
}

function isDigit(c: number): boolean {
  return c >= ZERO && c <= NINE;
}

/** U+FFFD, the only byte sequence this module ever substitutes. */
const REPLACEMENT = [0xef, 0xbf, 0xbd];

class Parser {
  private i = 0;

  constructor(private readonly b: Buffer) {}

  fail(what: string): never {
    throw new ArtifactCorruptError(`chtypes: bad result document: ${what} at offset ${this.i}`);
  }

  skipSpace(): void {
    while (this.i < this.b.length && isSpace(this.b[this.i]!)) this.i++;
  }

  atEnd(): boolean {
    return this.i >= this.b.length;
  }

  /** Parse one value, leaving the cursor just past it. */
  value(): Json {
    this.skipSpace();
    if (this.atEnd()) this.fail('unexpected end of document');
    const c = this.b[this.i]!;
    switch (c) {
      case OBJECT_OPEN:
        return this.object();
      case ARRAY_OPEN:
        return this.array();
      case QUOTE:
        return this.string();
      case LOWER_T:
        return this.literal('true', 'bool');
      case LOWER_F:
        return this.literal('false', 'bool');
      case LOWER_N:
        return this.literal('null', 'null');
      default:
        if (c === MINUS || isDigit(c)) return this.number();
        return this.fail(`unexpected byte 0x${c.toString(16)}`);
    }
  }

  private literal(word: string, kind: JsonKind): Json {
    const start = this.i;
    for (let k = 0; k < word.length; k++) {
      if (this.b[this.i + k] !== word.charCodeAt(k)) this.fail(`expected ${word}`);
    }
    this.i += word.length;
    const raw = this.b.subarray(start, this.i);
    return new Json(kind, raw, raw);
  }

  /**
   * A JSON number, strictly: no leading `+`, no bare `.5`, no `1.`, no leading
   * zeros. The strictness is load-bearing rather than pedantic: the fetch layer
   * reads signed statements and manifests with this grammar, and a lenient reading
   * would accept bytes the signature never covered.
   */
  private number(): Json {
    const start = this.i;
    if (this.b[this.i] === MINUS) this.i++;
    if (this.atEnd() || !isDigit(this.b[this.i]!)) this.fail('expected a digit');
    if (this.b[this.i] === ZERO) {
      this.i++;
    } else {
      while (!this.atEnd() && isDigit(this.b[this.i]!)) this.i++;
    }
    if (this.b[this.i] === DOT) {
      this.i++;
      if (this.atEnd() || !isDigit(this.b[this.i]!)) this.fail('expected a digit after the decimal point');
      while (!this.atEnd() && isDigit(this.b[this.i]!)) this.i++;
    }
    if (this.b[this.i] === LOWER_E || this.b[this.i] === UPPER_E) {
      this.i++;
      if (this.b[this.i] === PLUS || this.b[this.i] === MINUS) this.i++;
      if (this.atEnd() || !isDigit(this.b[this.i]!)) this.fail('expected a digit in the exponent');
      while (!this.atEnd() && isDigit(this.b[this.i]!)) this.i++;
    }
    const raw = this.b.subarray(start, this.i);
    return new Json('number', raw, raw);
  }

  /**
   * A JSON string. Its unescaped bytes are a slice of the source when it holds no
   * escape (the common case) and are assembled byte by byte when it does — never
   * through a JS string, so a `String` column's invalid UTF-8 crosses intact.
   */
  private string(): Json {
    const start = this.i;
    this.i++; // the opening quote
    let escaped = false;
    for (;;) {
      if (this.atEnd()) this.fail('unterminated string');
      const c = this.b[this.i]!;
      if (c === BACKSLASH) {
        escaped = true;
        this.i += 2;
        continue;
      }
      if (c === QUOTE) break;
      // A raw control byte is not JSON, and ClickHouse escapes every one of
      // them, so meeting one means this is not the document promised here.
      if (c < SPACE) this.fail(`raw control byte 0x${c.toString(16)} in a string`);
      this.i++;
    }
    const inner = this.b.subarray(start + 1, this.i);
    this.i++; // the closing quote
    const raw = this.b.subarray(start, this.i);
    return new Json('string', raw, escaped ? this.unescape(inner) : inner);
  }

  /** Resolve the escapes in a string body. Bytes in, bytes out. */
  private unescape(inner: Buffer): Buffer {
    const out: number[] = [];
    for (let i = 0; i < inner.length; i++) {
      const c = inner[i]!;
      if (c !== BACKSLASH) {
        out.push(c);
        continue;
      }
      i++;
      switch (inner[i]) {
        case QUOTE:
          out.push(QUOTE);
          break;
        case BACKSLASH:
          out.push(BACKSLASH);
          break;
        // ClickHouse escapes forward slashes by default
        // (output_format_json_escape_forward_slashes = 1).
        case SLASH:
          out.push(SLASH);
          break;
        case LOWER_B:
          out.push(BACKSPACE);
          break;
        case LOWER_F:
          out.push(FORM_FEED);
          break;
        case LOWER_N:
          out.push(LF);
          break;
        case LOWER_R:
          out.push(CR);
          break;
        case LOWER_T:
          out.push(TAB);
          break;
        case LOWER_U: {
          const hi = this.hex4(inner, i + 1);
          i += 4;
          let cp = hi;
          if (hi >= 0xd800 && hi <= 0xdbff && inner[i + 1] === BACKSLASH && inner[i + 2] === LOWER_U) {
            const lo = this.hex4(inner, i + 3);
            if (lo >= 0xdc00 && lo <= 0xdfff) {
              cp = 0x10000 + ((hi - 0xd800) << 10) + (lo - 0xdc00);
              i += 6;
            }
          }
          // A lone surrogate has no UTF-8 encoding; the reference's `unquote`
          // maps it to U+FFFD, and so does this.
          if (cp >= 0xd800 && cp <= 0xdfff) out.push(...REPLACEMENT);
          else for (const byte of Buffer.from(String.fromCodePoint(cp), 'utf8')) out.push(byte);
          break;
        }
        default:
          this.fail('unknown escape in a string');
      }
    }
    return Buffer.from(out);
  }

  private hex4(inner: Buffer, at: number): number {
    let n = 0;
    for (let k = 0; k < 4; k++) {
      const c = inner[at + k];
      if (c === undefined) this.fail('truncated \\u escape');
      const d =
        c >= 0x30 && c <= 0x39
          ? c - 0x30
          : c >= 0x61 && c <= 0x66
            ? c - 0x61 + 10
            : c >= 0x41 && c <= 0x46
              ? c - 0x41 + 10
              : -1;
      if (d < 0) this.fail('bad \\u escape');
      n = n * 16 + d;
    }
    return n;
  }

  private array(): Json {
    const start = this.i;
    this.i++;
    const elements: Json[] = [];
    this.skipSpace();
    if (this.b[this.i] === ARRAY_CLOSE) {
      this.i++;
      return new Json('array', this.b.subarray(start, this.i), EMPTY, elements);
    }
    for (;;) {
      elements.push(this.value());
      this.skipSpace();
      const c = this.b[this.i];
      if (c === COMMA) {
        this.i++;
        continue;
      }
      if (c === ARRAY_CLOSE) {
        this.i++;
        break;
      }
      this.fail('expected , or ] in an array');
    }
    return new Json('array', this.b.subarray(start, this.i), EMPTY, elements);
  }

  private object(): Json {
    const start = this.i;
    this.i++;
    const keys: Buffer[] = [];
    const values: Json[] = [];
    this.skipSpace();
    if (this.b[this.i] === OBJECT_CLOSE) {
      this.i++;
      return new Json('object', this.b.subarray(start, this.i), EMPTY, values, keys);
    }
    for (;;) {
      this.skipSpace();
      if (this.b[this.i] !== QUOTE) this.fail('expected a quoted key in an object');
      // Duplicate keys are kept: ClickHouse can store a Map with a repeated key,
      // and collapsing it here would report bytes the table does not hold.
      keys.push(this.string().bytes);
      this.skipSpace();
      if (this.b[this.i] !== COLON) this.fail('expected : after an object key');
      this.i++;
      values.push(this.value());
      this.skipSpace();
      const c = this.b[this.i];
      if (c === COMMA) {
        this.i++;
        continue;
      }
      if (c === OBJECT_CLOSE) {
        this.i++;
        break;
      }
      this.fail('expected , or } in an object');
    }
    return new Json('object', this.b.subarray(start, this.i), EMPTY, values, keys);
  }
}

/**
 * Is this byte string **one JSON value**? It is only if a strict parse consumes
 * **all** of it, with whitespace being exactly JSON's four (space, tab, LF, CR).
 * A numeric *prefix* does not count (`1.2.3.4` is not `1.2`), and U+FEFF is
 * **not** whitespace, though JS's own `String.prototype.trim()` counts it as one.
 *
 * Returns `null` when the bytes are not a single JSON value.
 */
export function parseJsonValue(bytes: Buffer): Json | null {
  if (bytes.length === 0) return null;
  try {
    const p = new Parser(bytes);
    const v = p.value();
    p.skipSpace();
    return p.atEnd() ? v : null;
  } catch {
    return null;
  }
}

// ------------------------------------------------------------------ accessors
//
// Every field of a result document is optional: a rejected row's document omits
// `unknown_fields`, `unsupported_settings` and `computed` entirely and carries
// `"cols":[]` (the C ABI contract §Top-level fields). A binding must therefore treat
// each field as optional-with-a-default rather than requiring it.

/**
 * One member of an object, by key. The **last** wins, which is what a JS object,
 * a Go map and the arbiter's own `json.loads` all do — a document field is never
 * repeated, and a stored value is read through `rawBytes`, never by key.
 */
export function field(value: Json | undefined, key: string): Json | undefined {
  if (value === undefined || value.kind !== 'object') return undefined;
  let found: Json | undefined;
  for (let i = 0; i < value.keys.length; i++) {
    if (keyIs(value.keys[i]!, key)) found = value.items[i];
  }
  return found;
}

/**
 * Compare a raw key against a field name without allocating: this runs a dozen
 * times per column and the document's own field names are ASCII. A non-ASCII
 * name falls back to a real decode rather than answering wrongly.
 */
function keyIs(key: Buffer, want: string): boolean {
  for (let i = 0; i < want.length; i++) {
    // One non-ASCII character and the byte length no longer matches the string
    // length, so the fast path cannot answer at all.
    if (want.charCodeAt(i) > 0x7f) return key.toString('utf8') === want;
  }
  if (key.length !== want.length) return false;
  for (let i = 0; i < want.length; i++) {
    if (key[i] !== want.charCodeAt(i)) return false;
  }
  return true;
}

/** The elements of an array (or an object's values); `[]` for anything else. */
export function items(value: Json | undefined): readonly Json[] {
  return value === undefined ? NO_ITEMS : value.items;
}

/** A JSON string's text, decoded as UTF-8. `''` for any other kind. */
export function asString(value: Json | undefined): string {
  return value !== undefined && value.kind === 'string' ? value.bytes.toString('utf8') : '';
}
