/**
 * The one strict JSON entry every document decoder and the loader's build-info
 * reader share (`docs/reference/bindings-v1.md` §5, rules 1 and 3): the stock
 * `JSON.parse`, preceded by a scan that refuses a duplicate key at any nesting
 * depth, because `JSON.parse` has already silently folded a duplicate to its
 * last value by the time anything could see it.
 */

/** A key repeated within one JSON object. */
export class DuplicateKeyError extends Error {
  readonly key: string;

  constructor(key: string) {
    super(`"${key}" appears twice in one object`);
    this.name = 'DuplicateKeyError';
    this.key = key;
  }
}

/** The first key repeated within the SAME enclosing `{...}` object in `text` (any nesting depth), or null. A minimal JSON scanner: tracks string literals (with escapes) and `{`/`}` nesting; a quoted token immediately followed by `:` is unambiguously an object key in valid JSON, in both array and object contexts. */
export function firstDuplicateKey(text: string): string | null {
  const stack: Set<string>[] = [];
  let i = 0;
  const n = text.length;
  while (i < n) {
    const c = text[i];
    if (c === '"') {
      const start = i + 1;
      let j = start;
      while (j < n) {
        if (text[j] === '\\') {
          j += 2;
          continue;
        }
        if (text[j] === '"') break;
        j++;
      }
      const raw = text.slice(start, j);
      let k = j + 1;
      while (k < n && /\s/.test(text[k] as string)) k++;
      if (text[k] === ':' && stack.length > 0) {
        const top = stack[stack.length - 1] as Set<string>;
        if (top.has(raw)) return raw;
        top.add(raw);
      }
      i = j + 1;
      continue;
    }
    if (c === '{') {
      stack.push(new Set());
      i++;
      continue;
    }
    if (c === '}') {
      stack.pop();
      i++;
      continue;
    }
    i++;
  }
  return null;
}

/** `JSON.parse`, refusing a duplicate key first. A parse failure is the parser's own `SyntaxError`. */
export function parseStrictJson(text: string): unknown {
  const dup = firstDuplicateKey(text);
  if (dup !== null) throw new DuplicateKeyError(dup);
  return JSON.parse(text);
}

/** True when every character is ASCII. */
export function isAsciiOnly(text: string): boolean {
  for (let i = 0; i < text.length; i++) {
    if ((text.charCodeAt(i) as number) > 0x7f) return false;
  }
  return true;
}
