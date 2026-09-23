/**
 * The quoting trio, measured against real artifacts (issue #119, from #52).
 *
 * Four things are pinned here, and none of them is a spelling:
 *
 * 1. **The per-line boundaries.** `quoteIdentifierIfNeeded` answers the LOADED
 *    BUILD's own rule, and that rule moves between ClickHouse lines. The 0.3.0
 *    CHANGELOG states which names move where; these cases execute that
 *    statement against whatever lines the registry holds, on BOTH sides of
 *    every boundary.
 * 2. **An empty identifier comes back quoted, never bare** (`include/chtypes.h`).
 * 3. **A literal carrying a NUL byte survives end to end** — the one case where
 *    the counted-input contract is load-bearing, and the reason this binding
 *    sends the input as bytes rather than as a C string.
 * 4. **Two loaded libraries answer for themselves**, asked in one process and
 *    interleaved. Per-line divergence is the entire reason these calls hang off
 *    a library rather than off the package, and one library cannot show it.
 *
 * HOW A SPELLING IS NEVER WRITTEN DOWN HERE. Every expectation is stated as one
 * of the library's OWN two answers: `quoteIdentifier` always quotes, so it IS
 * this build's quoted spelling, and the input itself is the bare spelling. A
 * case asserts `answer === library.quoteIdentifier(name)` or `answer === name`
 * and never names a quote character — which is what keeps
 * `scripts/check-quoting-passthrough.py` true and keeps these cases from
 * re-asserting the hand-written rule issue #52 deleted. Each line is first
 * checked to spell the two forms DIFFERENTLY, so "quoted" and "bare" cannot
 * both be satisfied by the same bytes.
 *
 * THE TWO ENUMERATIONS BELOW ARE EXPECTATIONS, NOT A RULE. Nothing here
 * computes which names a build quotes; the sets say what the release MEASURED,
 * per line, and a disagreement is a finding about the artifact rather than a
 * test to adjust. They are closed downward on purpose: the lines named are the
 * published lines that sit below a boundary, and any other line is at or above
 * the last one, so a line published after this was written reads as "quotes all
 * three" rather than silently dropping out of the count.
 *
 * COUNTS, AND WHY A QUIET RUN IS A FAILURE. Every case counts what it actually
 * ran and asserts the total. Without a registry the whole suite skips, loudly,
 * by name, like every other artifact-backed suite here; WITH one, a case that
 * observed only one side of a boundary — or only one library — fails by name
 * rather than passing on the half it could see. A boundary case that skips
 * forever is exactly the failure this file exists to prevent.
 */

import { beforeAll, describe, expect, it } from 'vitest';
import {
  type Library,
  looksLikeRegistry,
  Registry,
  resolveRegistryDir,
} from '../src/index.js';

const REGISTRY = resolveRegistryDir();
const HAVE_REGISTRY = REGISTRY !== null && looksLikeRegistry(REGISTRY);

if (!HAVE_REGISTRY) {
  console.warn(
    [
      '',
      '[chtypes] Quoting-boundary tests SKIPPED: no artifact registry on the search path.',
      '  Fetch the lines with scripts/fetch.sh 24.8 and scripts/fetch.sh 26.8',
      '  (docs/guides/fetch.md), or point CHTYPES_REGISTRY at a registry directory.',
      '',
    ].join('\n'),
  );
}

// The 0.3.0 CHANGELOG's claim, split into the part that holds everywhere and
// the part that moves between lines.
const QUOTED_ON_EVERY_LINE = ['all', 'distinct', 'table', 'null'] as const;
const BARE_ON_EVERY_LINE = ['where'] as const;
const MOVES_BETWEEN_LINES = ['select', 'from', 'values'] as const;
const EVERY_NAME: readonly string[] = [
  ...QUOTED_ON_EVERY_LINE,
  ...BARE_ON_EVERY_LINE,
  ...MOVES_BETWEEN_LINES,
];

/** Published lines below every boundary: all three of the moving names bare. */
const LINES_BELOW_EVERY_BOUNDARY: ReadonlySet<string> = new Set([
  '24.8',
  '25.3',
  '25.8',
  '25.10',
  '26.2',
]);
/** Published lines that quote `select` and not yet `from`/`values`. */
const LINES_QUOTING_SELECT_ONLY: ReadonlySet<string> = new Set(['26.3', '26.4']);

/** Whether this line is expected to spell `name` bare, per the CHANGELOG. */
function expectedBare(line: string, name: string): boolean {
  if ((QUOTED_ON_EVERY_LINE as readonly string[]).includes(name)) return false;
  if ((BARE_ON_EVERY_LINE as readonly string[]).includes(name)) return true;
  // Anything left is one of the names the release says MOVES between lines,
  // and nothing else may reach the per-line answer below.
  if (!(MOVES_BETWEEN_LINES as readonly string[]).includes(name)) {
    throw new Error(`${name} has no expectation in this file`);
  }
  if (LINES_BELOW_EVERY_BOUNDARY.has(line)) return true;
  if (LINES_QUOTING_SELECT_ONLY.has(line)) return name !== 'select';
  return false;
}

interface Loaded {
  line: string;
  library: Library;
}

describe.skipIf(!HAVE_REGISTRY)('quoting, per loaded line', () => {
  let loaded: Loaded[] = [];

  beforeAll(() => {
    const registry = new Registry(REGISTRY ?? undefined);
    loaded = [];
    for (const line of registry.versions()) {
      try {
        loaded.push({ line, library: registry.for(line) });
      } catch (err) {
        // A line that refuses to open — an artifact from an older ABI
        // revision left on the search path, say — is reported and left out
        // rather than failing every case here: whether enough lines opened is
        // decided below, by name, where the reason can be stated.
        console.warn(`[chtypes] line ${line} did not open and is not measured here: ${err}`);
      }
    }
  });

  /** (the always-quoted spelling, the if-needed answer) for one name. The
   * first is this build's own quoted form — that is what `quoteIdentifier` IS
   * — so a case never has to know what quoting looks like. */
  function bothForms(library: Library, name: string): [string, string] {
    return [library.quoteIdentifier(name), library.quoteIdentifierIfNeeded(name)];
  }

  it('answers each documented per-line boundary the way the release documents it', () => {
    let ran = 0;
    const below = loaded.filter((l) => LINES_BELOW_EVERY_BOUNDARY.has(l.line));
    const above = loaded.filter(
      (l) => !LINES_BELOW_EVERY_BOUNDARY.has(l.line) && !LINES_QUOTING_SELECT_ONLY.has(l.line),
    );
    for (const { line, library } of loaded) {
      for (const name of EVERY_NAME) {
        const [always, answer] = bothForms(library, name);
        expect(
          always,
          `${line}: the always-quoted form of ${name} is the bare name, so this case cannot tell quoted from bare`,
        ).not.toBe(name);
        if (expectedBare(line, name)) {
          expect(answer, `${line}: ${name} is documented bare`).toBe(name);
        } else {
          expect(answer, `${line}: ${name} is documented as this build's quoted form`).toBe(always);
        }
        ran++;
      }
    }
    expect(ran, 'a case was skipped inside the loop').toBe(EVERY_NAME.length * loaded.length);
    expect(ran, 'no line was examined').toBeGreaterThan(0);
    // Both sides, or nothing is being measured. CI fetches 24.8 alongside the
    // newest -lts and -stable precisely so this holds.
    expect(
      below.length,
      `the registry holds no line below every documented boundary (one of ${[...LINES_BELOW_EVERY_BOUNDARY].join(', ')}), so the boundary cannot be observed at all — scripts/fetch.sh 24.8 installs one. Lines loaded: ${loaded.map((l) => l.line).join(', ')}`,
    ).toBeGreaterThan(0);
    expect(
      above.length,
      `the registry holds no line at or above the last documented boundary, so the quoted side of it cannot be observed — scripts/fetch.sh 26.8 installs one. Lines loaded: ${loaded.map((l) => l.line).join(', ')}`,
    ).toBeGreaterThan(0);
  });

  it('gives an empty identifier back quoted, never bare', () => {
    let ran = 0;
    for (const { line, library } of loaded) {
      const [always, answer] = bothForms(library, '');
      expect(answer, `${line}: an empty identifier came back empty, which is bare`).not.toBe('');
      expect(answer, `${line}: an empty identifier must be this build's quoted form`).toBe(always);
      ran++;
    }
    expect(ran, 'no line answered for an empty identifier').toBe(loaded.length);
    expect(ran).toBeGreaterThan(0);
  });

  it('quotes a literal carrying a NUL byte, end to end', () => {
    let ran = 0;
    for (const { line, library } of loaded) {
      const plain = library.quoteLiteral('a');
      const withNul = library.quoteLiteral('a b');
      // A strlen'd input would quote just the leading "a" and say nothing.
      expect(withNul, `${line}: the input was truncated at the NUL byte`).not.toBe(plain);
      expect(withNul.length, `${line}: the answer is no longer than the plain one`).toBeGreaterThan(
        plain.length,
      );
      expect(withNul, `${line}: the byte after the NUL is missing`).toContain('b');
      // The header's other half: every byte the server escapes comes back
      // escaped, so the ANSWER is NUL-free even when the input was not.
      expect(withNul, `${line}: the answer carries a NUL byte`).not.toContain(' ');
      // Same delimiters as an ordinary value: this is one literal, not two.
      expect(withNul[0], `${line}: the answer is not delimited like a plain value`).toBe(plain[0]);
      expect(withNul[withNul.length - 1], `${line}: the answer is not delimited at the end`).toBe(
        plain[plain.length - 1],
      );
      ran++;
    }
    expect(ran, 'no line quoted a literal carrying a NUL byte').toBe(loaded.length);
    expect(ran).toBeGreaterThan(0);
  });

  it('lets two loaded libraries each answer for themselves', () => {
    expect(
      loaded.length,
      `this case needs TWO libraries in one process — a single library cannot show a cache-across-versions bug, which is the whole reason these calls hang off a library. Lines loaded: ${loaded.map((l) => l.line).join(', ')}`,
    ).toBeGreaterThanOrEqual(2);
    const older = loaded[0] as Loaded;
    const newer = loaded[loaded.length - 1] as Loaded;

    let ran = 0;
    const rounds: Array<Map<string, [string, string]>> = [new Map(), new Map()];
    // Interleaved, and then interleaved again: each library is asked the same
    // name after the other one has answered it, so an answer cached across
    // versions would show up as one library repeating the other's.
    for (const round of rounds) {
      for (const name of EVERY_NAME) {
        round.set(name, [
          older.library.quoteIdentifierIfNeeded(name),
          newer.library.quoteIdentifierIfNeeded(name),
        ]);
        ran += 2;
      }
    }
    expect(
      [...(rounds[1] as Map<string, [string, string]>)],
      `${older.line}/${newer.line}: asking one library changed the other's answer`,
    ).toEqual([...(rounds[0] as Map<string, [string, string]>)]);
    expect(ran, 'a name was skipped inside the loop').toBe(2 * 2 * EVERY_NAME.length);

    // And where the two lines sit on opposite sides of a boundary, they must
    // DISAGREE — the divergence the library-scoped API exists for.
    const divergent = EVERY_NAME.filter(
      (name) => expectedBare(older.line, name) !== expectedBare(newer.line, name),
    );
    expect(
      divergent.length,
      `${older.line} and ${newer.line} sit on the same side of every documented boundary, so no divergence can be observed — fetch a line below 26.3 (24.8) alongside the newest one`,
    ).toBeGreaterThan(0);
    for (const name of divergent) {
      const pair = (rounds[0] as Map<string, [string, string]>).get(name) as [string, string];
      expect(
        pair[0],
        `${name} is documented as differing between ${older.line} and ${newer.line}`,
      ).not.toBe(pair[1]);
    }
  });
});
