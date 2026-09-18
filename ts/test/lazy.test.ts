/**
 * Loading is lazy, and `preload` is the one eager path.
 *
 * The sentence this file exists to hold is the same sentence in all four
 * bindings: constructing a registry reads `manifest.json` files and `dlopen`s
 * nothing, and nothing in the package opens an artifact except a request for a
 * specific version or an explicit `preload`.
 *
 * The proof needs no real artifact and inspects no code. Every "library" here
 * is a text file, so any open at all fails at `dlopen` — a registry that
 * opened one would throw from whichever call opened it. What is counted is
 * `libraries()`, which is populated by the real load path and by nothing else.
 */

import { createHash } from 'node:crypto';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { ArtifactMissingError, ChtypesError, Registry, RegistryError } from '../src/index.js';

const BODY = 'not a shared library';

const roots: string[] = [];
const savedEnv: Record<string, string | undefined> = {};

function scratch(tag: string): string {
  const dir = mkdtempSync(path.join(tmpdir(), `chtypes-lazy-${tag}-`));
  roots.push(dir);
  return dir;
}

/** `<root>/<line>/` with a manifest and a "library" that is plain text. */
function standIn(root: string, line: string, overrides: Record<string, unknown> = {}): string {
  const sub = path.join(root, line);
  mkdirSync(sub, { recursive: true });
  writeFileSync(path.join(sub, 'libchtypes.so'), BODY);
  writeFileSync(
    path.join(sub, 'manifest.json'),
    JSON.stringify({
      library: 'libchtypes.so',
      library_bytes: Buffer.byteLength(BODY),
      clickhouse_version: `${line}.1.1`,
      clickhouse_minor: line,
      ...overrides,
    }),
  );
  return sub;
}

/** Three stand-in artifacts, with this machine's own registry off the path. */
function standInRegistry(lines: readonly string[] = ['25.8', '25.10', '26.7']): string {
  const root = scratch('reg');
  for (const line of lines) standIn(root, line);
  return root;
}

beforeEach(() => {
  // This machine's real cache must not join the search path: `versions()` has
  // to answer about the directory under test and nothing else.
  for (const name of ['CHTYPES_REGISTRY', 'CHTYPES_AUTOFETCH', 'XDG_CACHE_HOME']) {
    savedEnv[name] = process.env[name];
    delete process.env[name];
  }
  process.env['XDG_CACHE_HOME'] = scratch('xdg');
});

afterEach(() => {
  for (const [name, value] of Object.entries(savedEnv)) {
    if (value === undefined) delete process.env[name];
    else process.env[name] = value;
  }
  for (const dir of roots.splice(0)) rmSync(dir, { recursive: true, force: true });
});

describe('lazy loading', () => {
  it('construction and every listing open nothing', () => {
    const dir = standInRegistry();
    const registry = new Registry(dir);
    expect(registry.libraries()).toEqual([]);

    // versions() answers from the manifest scan, not from what is open — the
    // one meaning all four bindings now share: loaded OR discoverable.
    expect(registry.versions()).toEqual(['25.8', '25.10', '26.7']);
    // ... and it did not open anything to answer.
    expect(registry.libraries()).toEqual([]);
    expect(registry.has('25.8')).toBe(true);
    expect(registry.libraries()).toEqual([]);

    // The first request for a line is what opens it — and here that open
    // reaches dlopen and dies there, which is the proof it reached dlopen at
    // all rather than being skipped.
    expect(() => registry.for('25.8')).toThrow(ChtypesError);
  });

  it('versions() is loaded OR discovered, which is what makes it survive laziness', () => {
    // Before this change TypeScript's versions() listed what was LOADED. That
    // was invisible only because construction had loaded everything; a fresh
    // lazy registry would have answered [] for a directory full of artifacts.
    const dir = standInRegistry(['24.8']);
    expect(new Registry(dir).versions()).toEqual(['24.8']);
  });

  it('preload opens exactly the named lines, at construction', () => {
    const dir = standInRegistry();
    // The dlopen failure of a text file arrives from the CONSTRUCTOR.
    expect(() => new Registry(dir, { preload: ['25.10'] })).toThrow(/25\.10/);
    // A different entry proves it is the LIST that decides which line opens,
    // not the directory listing.
    expect(() => new Registry(dir, { preload: ['26.7'] })).toThrow(/26\.7/);
    // An empty list is exactly the default, not a special case.
    expect(new Registry(dir, { preload: [] }).libraries()).toEqual([]);
  });

  it('a preload entry no directory holds is the §7 error, at construction', () => {
    const dir = standInRegistry(['25.8']);
    let thrown: unknown;
    try {
      new Registry(dir, { preload: ['26.7'] });
    } catch (e) {
      thrown = e;
    }
    expect(thrown).toBeInstanceOf(ArtifactMissingError);
    expect((thrown as ArtifactMissingError).line).toBe('26.7');
    expect(String((thrown as Error).message)).toContain(dir);
  });

  it('preload never fetches, even with autofetch on', () => {
    // The constructor is synchronous and `ensure()` is not, and autofetch is a
    // first-use behavior in all four bindings: a constructor is a worse place
    // than a request to begin a 250 MB download.
    const dir = standInRegistry(['25.8']);
    expect(() => new Registry(dir, { autofetch: true, preload: ['26.7'] })).toThrow(ArtifactMissingError);
  });

  it('a checksum is computed immediately before a dlopen and at no other time', () => {
    // Preloaded: the refusal arrives from the constructor. Lazily opened: the
    // refusal arrives from the call that asks. Never asked for: never hashed.
    // Reading the option as "only the preload list is verified" would let a
    // lazily-opened line load UNHASHED under verifyChecksums.
    const dir = scratch('verify');
    standIn(dir, '25.8', { library_sha256: '00'.repeat(32) });
    standIn(dir, '26.7', { library_sha256: '00'.repeat(32) });

    expect(() => new Registry(dir, { verifyChecksums: true, preload: ['25.8'] })).toThrow(
      /does not match manifest/,
    );

    const registry = new Registry(dir, { verifyChecksums: true });
    expect(registry.libraries()).toEqual([]);
    expect(() => registry.for('26.7')).toThrow(/does not match manifest/);

    // With the option off the hash is not consulted at all, which is what
    // proves the OPTION produced the verdicts above rather than the file.
    const honest = new Registry(dir);
    let thrown: unknown;
    try {
      honest.for('25.8');
    } catch (e) {
      thrown = e;
    }
    expect(String((thrown as Error).message)).not.toMatch(/does not match manifest/);
  });

  it('an honest line in a directory with a corrupt neighbour still serves', () => {
    const dir = scratch('mixed');
    standIn(dir, '25.8', { library_sha256: '00'.repeat(32) }); // corrupt
    standIn(dir, '26.7', { library_sha256: createHash('sha256').update(BODY).digest('hex') });

    // A registry over a directory holding one broken line and one good one
    // used to fail at construction and serve neither.
    const registry = new Registry(dir, { verifyChecksums: true });
    expect(registry.versions()).toEqual(['25.8', '26.7']);
    let thrown: unknown;
    try {
      registry.for('26.7'); // passes its hash, then dies at dlopen
    } catch (e) {
      thrown = e;
    }
    expect(thrown).toBeDefined();
    expect(String((thrown as Error).message)).not.toMatch(/does not match manifest/);
  });

  it('the typo guard survives: a named directory that does not exist still fails at construction', () => {
    const missing = path.join(scratch('named'), 'chtyeps');
    expect(() => new Registry(missing)).toThrow(RegistryError);
    expect(() => new Registry(missing)).toThrow(new RegExp(missing.replaceAll('.', '\\.')));
  });

  it('a search path holding no manifest at all still fails at construction', () => {
    const empty = scratch('empty');
    expect(() => new Registry(empty)).toThrow(/no artifacts in any registry directory/);
    // Unless a fetch is going to populate it.
    expect(new Registry(empty, { autofetch: true }).versions()).toEqual([]);
  });
});
