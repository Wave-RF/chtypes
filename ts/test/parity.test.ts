/**
 * The binding parity contract, checked against this binding.
 *
 * `tests/parity/manifest.json` at the repository root is the ONE machine-readable
 * source of truth for what every binding must expose; `spec/bindings.md` is the
 * prose that explains why. This file is TypeScript's half of the enforcement: it
 * resolves every spelling the manifest assigns to the `ts` column against the real
 * package index, and fails by NAME when one is missing — naming the bindings that
 * DO have it, because "ts is missing `verifyInstalled`, which go/python/rust all
 * expose" is the sentence that gets the gap fixed.
 *
 * Three rules this file holds:
 *
 * * **It cannot pass by doing nothing.** A manifest that parsed to zero
 *   capabilities, or fewer than its own declared floors, FAILS — as does a run in
 *   which nothing resolved.
 * * **It needs no artifact.** Parity is a claim about the API surface, not about
 *   dlopening a library, so all of it runs in the artifact-free CI job. Nothing
 *   here constructs a `Registry`.
 * * **A deliberate gap is still written down.** A binding that should not have a
 *   capability declares `{"absent": "<why>"}`; an absence with no reason fails the
 *   manifest's own integrity check.
 *
 * TypeScript erases types, so a `type`-only export has no runtime value to find.
 * Those fall back to the export list of `src/index.ts` — this binding's own source,
 * always beside the test — rather than being silently skipped.
 */

import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import * as chtypes from '../src/index.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const TS_ROOT = path.resolve(HERE, '..');
const REPO = path.resolve(TS_ROOT, '..');
const MANIFEST = path.join(REPO, 'tests', 'parity', 'manifest.json');
const GO_COPY = path.join(REPO, 'go', 'chtypes', 'testdata', 'parity.json');

const LANG = 'ts';
const OTHERS = ['go', 'python', 'rust'] as const;

// --------------------------------------------------------------------------
// the manifest

interface Column {
  symbol?: string;
  absent?: string;
  value_check?: boolean;
  why?: string;
}
interface Capability {
  id: string;
  group: string;
  kind: string;
  what: string;
  value?: string | number;
  [lang: string]: unknown;
}
interface Manifest {
  schema: number;
  languages: string[];
  groups: Record<string, string>;
  floors: { capabilities: number; valued: number; per_group: number };
  capabilities: Capability[];
  unlisted: Record<string, Record<string, string>>;
}

const manifest: Manifest = JSON.parse(readFileSync(MANIFEST, 'utf8')) as Manifest;
const caps = manifest.capabilities;

function column(cap: Capability, lang: string): Column {
  const raw = cap[lang];
  if (typeof raw === 'string') return { symbol: raw };
  if (raw && typeof raw === 'object') return raw as Column;
  return {};
}
const spelling = (cap: Capability, lang: string): string | undefined => column(cap, lang).symbol;
function alsoIn(cap: Capability): string {
  const have = OTHERS.filter((l) => spelling(cap, l));
  return have.length ? have.join('/') : 'no other binding';
}

// --------------------------------------------------------------------------
// this binding's own source, for what the runtime cannot see

/** Every name `src/index.ts` re-exports, types included. */
function indexExports(): Set<string> {
  const src = readFileSync(path.join(TS_ROOT, 'src', 'index.ts'), 'utf8');
  const names = new Set<string>();
  for (const block of src.matchAll(/export\s*\{([^}]*)\}\s*from/g)) {
    for (const raw of (block[1] ?? '').split(',')) {
      const piece = raw.trim().replace(/^type\s+/, '');
      if (!piece) continue;
      const alias = piece.split(/\s+as\s+/);
      const name = (alias[alias.length - 1] ?? '').trim();
      if (name) names.add(name);
    }
  }
  for (const decl of src.matchAll(/export\s+(?:const|function|class|type)\s+(\w+)/g)) {
    names.add(decl[1] as string);
  }
  return names;
}

/** `Class -> its declared members`, read off the source: a `readonly x` or a
 *  constructor-assigned field is an own-property of instances and invisible on
 *  the prototype, and several contract entries are exactly that shape. */
function declaredMembers(): Map<string, Set<string>> {
  const out = new Map<string, Set<string>>();
  const dir = path.join(TS_ROOT, 'src');
  for (const file of readdirSync(dir).filter((f) => f.endsWith('.ts'))) {
    const src = readFileSync(path.join(dir, file), 'utf8');
    for (const hit of src.matchAll(/export\s+class\s+(\w+)[^{]*\{/g)) {
      const name = hit[1] as string;
      // Walk braces from the class body's opening brace to its match.
      let depth = 0;
      let end = hit.index! + hit[0].length;
      for (let i = hit.index! + hit[0].length - 1; i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') {
          depth--;
          if (depth === 0) {
            end = i;
            break;
          }
        }
      }
      const body = src.slice(hit.index! + hit[0].length, end);
      const members = out.get(name) ?? new Set<string>();
      for (const m of body.matchAll(/^\s*(?:readonly\s+|static\s+|get\s+|async\s+|\*)*([A-Za-z_$][\w$]*)\s*[(:<]/gm)) {
        members.add(m[1] as string);
      }
      out.set(name, members);
    }
  }
  return out;
}

const EXPORTS = indexExports();
const MEMBERS = declaredMembers();

type Resolved = { found: boolean; value?: unknown; runtime: boolean };

/** Resolve a spelling the way a consumer would: at runtime first, then — for a
 *  type-only export or a constructor-assigned field, neither of which survives
 *  to runtime — off this binding's own source. */
function resolve(cap: Capability, spelled: string): Resolved {
  const parts = spelled.split('.');
  const head = parts[0] as string;
  const mod = chtypes as unknown as Record<string, unknown>;

  if (parts.length === 1) {
    if (head in mod && mod[head] !== undefined) return { found: true, value: mod[head], runtime: true };
    return { found: EXPORTS.has(head), runtime: false };
  }

  const tail = parts[parts.length - 1] as string;
  const owner = mod[head];
  if (cap.kind === 'method' && typeof owner === 'function') {
    const proto = (owner as { prototype?: Record<string, unknown> }).prototype;
    if (proto && typeof proto[tail] === 'function') return { found: true, value: proto[tail], runtime: true };
  }
  if (owner && (typeof owner === 'object' || typeof owner === 'function')) {
    const value = (owner as Record<string, unknown>)[tail];
    if (value !== undefined) return { found: true, value, runtime: true };
  }
  // A declared member that only exists on an instance, or a type.
  return { found: MEMBERS.get(head)?.has(tail) === true && EXPORTS.has(head), runtime: false };
}

// --------------------------------------------------------------------------

describe('the parity manifest', () => {
  it('meets its own floors — a manifest that loaded nothing must FAIL', () => {
    expect(manifest.schema, 'unknown parity manifest schema').toBe(1);
    const { capabilities: floor, valued: valuedFloor, per_group: groupFloor } = manifest.floors;
    expect(
      caps.length,
      `the parity manifest declares ${caps.length} capabilities, below its own floor of ${floor}. ` +
        `A shrinking contract is how this check passes by doing nothing.`,
    ).toBeGreaterThanOrEqual(floor);
    const valued = caps.filter((c) => 'value' in c);
    expect(
      valued.length,
      `only ${valued.length} capabilities carry a shared value, below the floor of ${valuedFloor}`,
    ).toBeGreaterThanOrEqual(valuedFloor);
    for (const group of Object.keys(manifest.groups)) {
      const n = caps.filter((c) => c.group === group).length;
      expect(n, `group '${group}' has ${n} capabilities, below the floor of ${groupFloor}`).toBeGreaterThanOrEqual(
        groupFloor,
      );
    }
  });

  it('declares all four languages for every capability, or says in writing why not', () => {
    const problems: string[] = [];
    const seen = new Set<string>();
    for (const cap of caps) {
      if (seen.has(cap.id)) problems.push(`${cap.id}: duplicate id`);
      seen.add(cap.id);
      if (!cap.what?.trim()) problems.push(`${cap.id}: no 'what'`);
      if (!(cap.group in manifest.groups)) problems.push(`${cap.id}: unknown group '${cap.group}'`);
      let absent = 0;
      for (const lang of manifest.languages) {
        const col = column(cap, lang);
        if (!col.symbol && col.absent === undefined) {
          problems.push(`${cap.id}: ${lang} column has neither a symbol nor a declared absence`);
          continue;
        }
        if (col.absent !== undefined) {
          absent++;
          if (!col.absent.trim())
            problems.push(
              `${cap.id}: ${lang} is declared absent with no reason. A gap nobody had to justify in ` +
                `writing is how parity rots.`,
            );
        }
      }
      if (absent === manifest.languages.length)
        problems.push(`${cap.id}: absent in every binding — that is a note, not a contract`);
    }
    expect(problems, `the parity manifest is not internally consistent:\n  ${problems.join('\n  ')}`).toEqual([]);
  });

  it("keeps Go's embedded copy byte-identical", () => {
    // scripts/check-standalone.sh runs the Go suite from a bare copy of go/ with
    // no repository root beside it, so Go embeds the manifest. This asserts the
    // cache has not drifted from the one source of truth.
    const canonical = readFileSync(MANIFEST);
    const copy = readFileSync(GO_COPY);
    expect(
      copy.equals(canonical),
      `${GO_COPY} has drifted from ${MANIFEST}. Re-sync it:\n\n` +
        `    cp tests/parity/manifest.json go/chtypes/testdata/parity.json\n`,
    ).toBe(true);
  });
});

describe('typescript against the contract', () => {
  it('exposes every capability the contract assigns it', () => {
    const missing: string[] = [];
    let resolved = 0;
    for (const cap of caps) {
      const spelled = spelling(cap, LANG);
      if (!spelled || cap.kind === 'cli') continue;
      if (resolve(cap, spelled).found) resolved++;
      else missing.push(`${cap.id}: ts is missing \`${spelled}\`, which ${alsoIn(cap)} expose — ${cap.what}`);
    }
    expect(resolved, 'no ts spelling resolved at all — the check asserted nothing').toBeGreaterThan(0);
    expect(
      missing,
      `ts does not carry ${missing.length} capability/capabilities the parity contract assigns it:\n  ` +
        missing.join('\n  '),
    ).toEqual([]);
  });

  it('answers the same values as the other bindings', () => {
    const wrong: string[] = [];
    let checked = 0;
    for (const cap of caps) {
      if (!('value' in cap) || cap.kind === 'cli') continue;
      const col = column(cap, LANG);
      if (!col.symbol || col.value_check === false) continue;
      const got = resolve(cap, col.symbol);
      if (!got.found || !got.runtime) continue; // absence is the other test's failure
      const want = cap.value;
      const actual = typeof want === 'number' ? Number(got.value) : String(got.value);
      checked++;
      if (actual !== want)
        wrong.push(`${cap.id}: ts \`${col.symbol}\` is ${JSON.stringify(actual)}, the contract says ${JSON.stringify(want)}`);
    }
    expect(
      checked,
      `only ${checked} shared values were compared, below the floor of ${manifest.floors.valued} — ` +
        `a value check that checks nothing is not a check`,
    ).toBeGreaterThanOrEqual(manifest.floors.valued);
    expect(wrong, `ts answers differently from the contract:\n  ${wrong.join('\n  ')}`).toEqual([]);
  });

  it('offers every CLI subcommand the contract names', () => {
    const src = readFileSync(path.join(TS_ROOT, 'src', 'cli.ts'), 'utf8');
    const commands = caps.filter((c) => c.kind === 'cli');
    expect(commands.length, 'the contract names no CLI subcommands').toBeGreaterThan(0);
    const missing = commands
      .filter((c) => spelling(c, LANG) && !src.includes(`'${c.value}'`) && !src.includes(`"${c.value}"`))
      .map((c) => `${c.id}: the ts CLI has no \`${c.value}\` subcommand, which ${alsoIn(c)} offer`);
    expect(missing, missing.join('\n  ')).toEqual([]);
  });

  it('lets no public name escape the contract', () => {
    const declared = new Set(
      caps.map((c) => spelling(c, LANG)).filter((s): s is string => !!s).map((s) => s.split('.')[0] as string),
    );
    const allowed = new Set(Object.keys(manifest.unlisted[LANG] ?? {}).map((s) => s.split('.')[0] as string));
    const undeclared = [...EXPORTS].filter((n) => !declared.has(n) && !allowed.has(n)).sort();
    expect(
      undeclared,
      `ts exports ${undeclared.length} public name(s) the parity contract has never heard of:\n  ` +
        undeclared.join('\n  ') +
        `\n\nEither give each one a capability in tests/parity/manifest.json (if the other bindings ` +
        `should have it too) or list it under \`unlisted.ts\` with a reason.`,
    ).toEqual([]);
  });

  it('keeps its unlisted allowlist honest', () => {
    const gone: string[] = [];
    for (const name of Object.keys(manifest.unlisted[LANG] ?? {}).sort()) {
      const parts = name.split('.');
      if (parts.length === 1) {
        if (!EXPORTS.has(name)) gone.push(name);
      } else if (!MEMBERS.get(parts[0] as string)?.has(parts[1] as string)) {
        gone.push(name);
      }
    }
    expect(
      gone,
      '`unlisted.ts` in the parity manifest excuses names this binding no longer exports:\n  ' + gone.join('\n  '),
    ).toEqual([]);
  });
});
