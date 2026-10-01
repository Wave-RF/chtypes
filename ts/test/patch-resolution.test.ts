/**
 * Issue #284: exact-patch resolution, the `patches/` sibling tree, and lock
 * schema 2 — the parts `ts/test/fetch.test.ts` and `ts/test/lazy.test.ts`
 * don't already cover.
 *
 * Two kinds of fixture:
 *
 *  - **Fake manifests, built here** (`flatInstall`/`nestedInstall`, in the style
 *    of `lazy.test.ts`'s `standIn`): every "library" is a text file, so any
 *    `dlopen` fails —
 *    which is exactly the property that lets resolution be tested without a
 *    real artifact. A call that resolves to a location and attempts to load
 *    it throws `RegistryError` naming the exact path it tried, which is the
 *    proof of WHICH directory won, even though nothing actually opens.
 *  - **`tests/fixtures/fetch/two-patches/`** (§9 of the design, issue #284's
 *    served fixture): two real (if tiny) patches of one line, side by side,
 *    for the fetch/lock/demotion cases that need an actual install. SKIPS
 *    LOUDLY, never silently, when the fixture is absent.
 */

import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
// FETCH_ABI_REVISION is the internal, test-only override (not re-exported from
// index.js) that lets this suite fetch tests/fixtures/fetch/two-patches/ at
// the ABI revision IT carries, rather than assuming it equals this binding's own.
import { FETCH_ABI_REVISION } from '../src/fetch.js';
import {
  ArtifactMissingError,
  ArtifactPinnedError,
  ChtypesError,
  type EnsureOptions,
  ensure,
  FetchError,
  hostPlatform,
  LOCK_SCHEMA,
  listArtifacts,
  looksLikeRegistry,
  Registry,
  RegistryError,
  readLock,
  sha256File,
  verifyInstalled,
} from '../src/index.js';
// resetPatchFallbackWarnings is internal and test-only: the warned-pairs set
// is process-wide (§3), so a suite that wants a FRESH warning must clear it.
import { resetPatchFallbackWarnings } from '../src/registry.js';
import { fixtureAbiRevision } from './fixture-revision.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SPEC_FIXTURES = path.resolve(HERE, '..', '..', 'tests', 'fixtures', 'fetch');
const PLATFORM = hostPlatform();

// ---------------------------------------------------------------- helpers

const roots: string[] = [];
function scratch(tag: string): string {
  const dir = mkdtempSync(path.join(tmpdir(), `chtypes-patch-${tag}-`));
  roots.push(dir);
  return dir;
}

const BODY = 'not a shared library';

/** Write a fake artifact directory: a manifest, plus a text "library" that can never dlopen. */
function writeFakeArtifact(dir: string, minor: string, version: string): void {
  mkdirSync(dir, { recursive: true });
  writeFileSync(path.join(dir, 'libchtypes.so'), BODY);
  writeFileSync(
    path.join(dir, 'manifest.json'),
    JSON.stringify({
      library: 'libchtypes.so',
      library_bytes: Buffer.byteLength(BODY),
      clickhouse_version: version,
      clickhouse_minor: minor,
    }),
  );
}

/** `<root>/<minor>/` — the flat slot a LINE request resolves to. */
function flatInstall(root: string, minor: string, version: string): string {
  const dir = path.join(root, minor);
  writeFakeArtifact(dir, minor, version);
  return dir;
}

/** `<root>/patches/<minor>/<version>/` — any other exact patch (the layout rule, issue #284). */
function nestedInstall(root: string, minor: string, version: string): string {
  const dir = path.join(root, 'patches', minor, version);
  writeFakeArtifact(dir, minor, version);
  return dir;
}

/**
 * `registry.for(version)` (or `.resolve`/`.open`) against a fake artifact
 * always reaches `dlopen` and dies there — which is exactly what proves
 * resolution picked a real, specific directory rather than nothing at all.
 * Returns the directory the failure names.
 */
function attemptedDir(run: () => unknown): string {
  let thrown: unknown;
  try {
    run();
  } catch (err) {
    thrown = err;
  }
  expect(thrown, 'expected a load attempt (RegistryError), not a clean resolve').toBeInstanceOf(RegistryError);
  expect(thrown).not.toBeInstanceOf(ArtifactMissingError);
  // Non-greedy: the native loader's own error can re-mention the same path
  // (e.g. `Error: <path>: invalid ELF header`), so a greedy capture runs past
  // the first, authoritative "cannot load <path>:" and into that nested text.
  const m = /cannot load (.+?[/\\]libchtypes\.so):/.exec((thrown as Error).message);
  expect(m, `could not find an attempted path in: ${(thrown as Error).message}`).not.toBeNull();
  return path.dirname(m![1]!);
}

const savedEnv: Record<string, string | undefined> = {};
beforeEach(() => {
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
});
afterAll(() => {
  for (const dir of roots.splice(0)) rmSync(dir, { recursive: true, force: true });
});

// ------------------------------------------------- resolution (no dlopen)

describe('resolution over fake manifests, no dlopen needed (R1-R9)', () => {
  it('R3: a line request never falls back, and takes the newest patch in the FIRST root holding any', () => {
    const first = scratch('first');
    const second = scratch('second');
    flatInstall(first, '31.1', '31.1.5.1-lts');
    nestedInstall(second, '31.1', '31.1.9.1-lts'); // a later root, a newer patch — must lose to the first root
    process.env['CHTYPES_REGISTRY'] = second;
    const registry = new Registry(first);
    // The line resolves within the FIRST root that holds anything for it,
    // even though a later root holds a newer patch.
    expect(attemptedDir(() => registry.for('31.1'))).toBe(path.join(first, '31.1'));
  });

  it('R3: within one root, a nested install beats a flat one on a version TIE', () => {
    const root = scratch('tie');
    flatInstall(root, '31.2', '31.2.4.1-lts');
    const nested = nestedInstall(root, '31.2', '31.2.4.1-lts'); // same version, both slots
    const registry = new Registry(root);
    expect(attemptedDir(() => registry.for('31.2'))).toBe(nested);
  });

  it('R3: within one root, the newest patch wins regardless of slot', () => {
    const root = scratch('newest');
    flatInstall(root, '31.3', '31.3.2.1-lts');
    const newer = nestedInstall(root, '31.3', '31.3.9.1-lts');
    const registry = new Registry(root);
    expect(attemptedDir(() => registry.for('31.3'))).toBe(newer);
  });

  it('R4: an exact patch anywhere on the search path wins over a fallback in an earlier directory', () => {
    // The first root holds only an OLDER patch (a fallback candidate); the
    // second root holds the EXACT patch asked for. R4 step 2: "the first
    // directory anywhere on the search path that holds a matching patch" —
    // not merely the first directory that holds the line.
    const first = scratch('r4-first');
    const second = scratch('r4-second');
    flatInstall(first, '31.4', '31.4.1.1-lts');
    const exact = nestedInstall(second, '31.4', '31.4.9.9-lts');
    process.env['CHTYPES_REGISTRY'] = second;
    const registry = new Registry(first);
    expect(attemptedDir(() => registry.for('31.4.9.9-lts'))).toBe(exact);
  });

  it('R2/Decision 7: an unspelled channel matches that patch on any channel; a spelled DIFFERENT channel is not an exact match and falls back within the line', () => {
    const root = scratch('channel');
    const dir = flatInstall(root, '31.5', '31.5.2.1-lts');
    const registry = new Registry(root);
    expect(attemptedDir(() => registry.for('31.5.2.1'))).toBe(dir); // no channel typed: matches exactly
    // A different, explicitly spelled channel is never an exact match under
    // R2 — but the fallback (R4 step 4) does not care about channels at all,
    // so it still resolves, to the only patch this line has, as a fallback.
    expect(attemptedDir(() => registry.for('31.5.2.1-stable'))).toBe(dir);
  });

  it('R4 fallback: no exact match anywhere falls back to the newest installed patch of the SAME line, never another line', () => {
    const root = scratch('fallback');
    const other = flatInstall(root, '31.7', '31.7.1.1-lts'); // a different line entirely
    const winner = nestedInstall(root, '31.6', '31.6.9.9-lts');
    nestedInstall(root, '31.6', '31.6.2.2-lts'); // older — must lose to winner
    const registry = new Registry(root);
    expect(attemptedDir(() => registry.for('31.6.5.5-lts'))).toBe(winner);
    expect(attemptedDir(() => registry.for('31.6.5.5-lts'))).not.toBe(other);
  });

  it('R-c: a fallen-back patch request re-checks for an exact match at most once per 60s; recovery still needs no restart', () => {
    vi.useFakeTimers();
    try {
      const root = scratch('rc-throttle');
      const stale = nestedInstall(root, '34.1', '34.1.5.5-lts');
      const registry = new Registry(root);
      expect(attemptedDir(() => registry.for('34.1.9.9-lts'))).toBe(stale);

      // Install the exact match WHILE still inside the 60s window: the whole
      // re-check (the exact-match retry and the fallback pick alike) is
      // throttled together, so it must not be noticed yet.
      const exact = nestedInstall(root, '34.1', '34.1.9.9-lts');
      vi.advanceTimersByTime(59_000);
      expect(attemptedDir(() => registry.for('34.1.9.9-lts'))).toBe(stale);

      // Past the window: the next call re-scans and finds it.
      vi.advanceTimersByTime(2_000);
      expect(attemptedDir(() => registry.for('34.1.9.9-lts'))).toBe(exact);
    } finally {
      vi.useRealTimers();
    }
  });

  it('R6: a line no directory holds anywhere is ArtifactMissingError, never a fallback to a different line', () => {
    const root = scratch('missing-line');
    flatInstall(root, '31.8', '31.8.1.1-lts');
    const registry = new Registry(root);
    expect(() => registry.for('19.1')).toThrow(ArtifactMissingError);
    expect(() => registry.for('19.1.1.1-lts')).toThrow(ArtifactMissingError);
  });

  it('P19: a registry holding ONLY a nested install is not "empty" at construction', () => {
    const root = scratch('nested-only');
    const nested = nestedInstall(root, '31.9', '31.9.1.1-lts');
    expect(looksLikeRegistry(root)).toBe(true);
    const registry = new Registry(root); // must not throw "no artifacts anywhere"
    expect(registry.versions()).toEqual(['31.9']);
    expect(registry.has('31.9')).toBe(true);
    expect(registry.has('31.9.1.1-lts')).toBe(true);
    expect(attemptedDir(() => registry.for('31.9'))).toBe(nested);
  });

  it('an EXACT patch request into an empty destination resolves to the flat OR nested location it actually finds — has() opens nothing either way', () => {
    const root = scratch('has');
    flatInstall(root, '32.1', '32.1.1.1-lts');
    const registry = new Registry(root);
    expect(registry.has('32.1.1.1-lts')).toBe(true);
    expect(registry.has('32.1.9.9-lts')).toBe(true); // resolves via the line's fallback
    expect(registry.has('32.2')).toBe(false);
    expect(registry.libraries()).toEqual([]); // has() never opens anything
  });
});

// ---------------------------------------------------------------- Resolution / resolve()

describe('Registry#resolve — the Resolution object, without dlopen', () => {
  it('a line request resolves exact: true even when it never loads (message-only proof)', () => {
    const root = scratch('resolve-line');
    flatInstall(root, '32.3', '32.3.1.1-lts');
    const registry = new Registry(root);
    let thrown: unknown;
    try {
      registry.resolve('32.3');
    } catch (err) {
      thrown = err;
    }
    // resolve() cannot hand back a Resolution for a fake artifact (loading it
    // always throws first) — but it must still be the LOAD failure, not a
    // resolution failure, proving resolve() reached the same directory for()
    // would have.
    expect(thrown).toBeInstanceOf(RegistryError);
    expect(thrown).not.toBeInstanceOf(ArtifactMissingError);
  });

  it('an unresolvable version throws ArtifactMissingError from resolve() too', () => {
    const root = scratch('resolve-missing');
    flatInstall(root, '32.4', '32.4.1.1-lts');
    const registry = new Registry(root);
    expect(() => registry.resolve('19.9')).toThrow(ArtifactMissingError);
  });

  it('a spelling that is not a ClickHouse version at all throws ChtypesError, not ArtifactMissingError', () => {
    const root = scratch('resolve-bad-spelling');
    flatInstall(root, '32.5', '32.5.1.1-lts');
    const registry = new Registry(root);
    expect(() => registry.resolve('not-a-version')).toThrow(ChtypesError);
    expect(() => registry.resolve('not-a-version')).not.toThrow(ArtifactMissingError);
    // has() is lenient about the same input: never throws, just false.
    expect(registry.has('not-a-version')).toBe(false);
  });
});

// -------------------------------------------------------------------- §3

describe('the fallback warning (§3): process.emitWarning, once per (requested, actual) pair', () => {
  const captured: { type: string; code: string; message: string }[] = [];
  const onWarning = (w: Error & { type?: string; code?: string }): void => {
    captured.push({ type: w.type ?? '', code: w.code ?? '', message: w.message });
  };
  beforeEach(() => {
    captured.length = 0;
    resetPatchFallbackWarnings();
    process.on('warning', onWarning);
  });
  afterEach(() => {
    process.off('warning', onWarning);
  });

  it('fires once per (requested, actual) pair, with the requested and the actual patch named', async () => {
    const root = scratch('warn');
    const winner = nestedInstall(root, '33.1', '33.1.9.9-lts');
    nestedInstall(root, '33.1', '33.1.2.2-lts');
    const registry = new Registry(root);
    expect(attemptedDir(() => registry.for('33.1.5.5-lts'))).toBe(winner);
    // process.emitWarning is asynchronous (emitted on process.nextTick), so
    // give the loop a turn before asserting.
    await new Promise((r) => setTimeout(r, 0));
    const mine = captured.filter((c) => c.code === 'CHTYPES_PATCH_FALLBACK');
    expect(mine).toHaveLength(1);
    expect(mine[0]!.type).toBe('PatchFallbackWarning');
    expect(mine[0]!.message).toContain('33.1.5.5-lts');
    expect(mine[0]!.message).toContain('33.1.9.9-lts');
    expect(mine[0]!.message).toContain('newest installed patch of 33.1');

    // A second call for the SAME (requested, actual) pair: no second warning.
    expect(attemptedDir(() => registry.for('33.1.5.5-lts'))).toBe(winner);
    await new Promise((r) => setTimeout(r, 0));
    expect(captured.filter((c) => c.code === 'CHTYPES_PATCH_FALLBACK')).toHaveLength(1);

    // A DIFFERENT requested patch that falls back to the SAME actual: a NEW pair, warns again.
    expect(attemptedDir(() => registry.for('33.1.6.6-lts'))).toBe(winner);
    await new Promise((r) => setTimeout(r, 0));
    expect(captured.filter((c) => c.code === 'CHTYPES_PATCH_FALLBACK')).toHaveLength(2);
  });

  it('a line request never warns, even when it is the only patch of its line', async () => {
    const root = scratch('warn-line');
    flatInstall(root, '33.2', '33.2.1.1-lts');
    const registry = new Registry(root);
    expect(attemptedDir(() => registry.for('33.2'))).toBe(path.join(root, '33.2'));
    await new Promise((r) => setTimeout(r, 0));
    expect(captured.filter((c) => c.code === 'CHTYPES_PATCH_FALLBACK')).toHaveLength(0);
  });
});

// --------------------------------------------------- lock schema 2 (§6)

describe('lock schema 2: reading, converting schema 1, and refusing what does not parse', () => {
  it('LOCK_SCHEMA is 2', () => {
    expect(LOCK_SCHEMA).toBe(2);
  });

  it('reads a schema-1 lock and converts it to schema-2 keys, in memory, without touching the file', () => {
    const file = path.join(scratch('legacy-lock'), 'chtypes.lock');
    const original = JSON.stringify({
      schema: 1,
      artifacts: { 'linux-arm64/25.8': { file: 'chtypes-25.8.28.1-lts-linux-arm64.tar.gz', sha256: 'a'.repeat(64) } },
    });
    writeFileSync(file, original);
    const lock = readLock(file)!;
    expect(lock.schema).toBe(1); // reports what was actually on disk
    expect(lock.artifacts['linux-arm64/25.8']).toBeUndefined(); // never the old, line-shaped key
    expect(lock.artifacts['linux-arm64/25.8.28.1-lts']).toEqual({
      file: 'chtypes-25.8.28.1-lts-linux-arm64.tar.gz',
      sha256: 'a'.repeat(64),
    });
    expect(readFileSync(file, 'utf8')).toBe(original); // reading never writes
  });

  it('refuses a schema-1 entry whose file does not name the line its key claims', () => {
    const file = path.join(scratch('legacy-mismatch'), 'chtypes.lock');
    writeFileSync(
      file,
      JSON.stringify({
        schema: 1,
        // key says line 25.8; the file it names is a 26.7 patch.
        artifacts: { 'linux-arm64/25.8': { file: 'chtypes-26.7.3.19-stable-linux-arm64.tar.gz', sha256: 'b'.repeat(64) } },
      }),
    );
    expect(() => readLock(file)).toThrow(/names line 25\.8 but its file .* is 26\.7\.3\.19-stable/);
  });

  it('refuses a schema-2 lock whose key names a LINE, not an exact patch', () => {
    const file = path.join(scratch('bad-schema2-key'), 'chtypes.lock');
    writeFileSync(
      file,
      JSON.stringify({ schema: 2, artifacts: { 'linux-arm64/25.8': { file: 'x.tar.gz', sha256: 'c'.repeat(64) } } }),
    );
    expect(() => readLock(file)).toThrow(/names a line, not an exact patch/);
  });

  it('refuses schema 3, naming both 1 and 2', () => {
    const file = path.join(scratch('bad-schema'), 'chtypes.lock');
    writeFileSync(file, JSON.stringify({ schema: 3, artifacts: {} }));
    expect(() => readLock(file)).toThrow(/schema 3.*not 1 or 2/);
  });

  it('an entry with a valid schema-2 key but an unparseable file name is still refused for a schema-1 read, never silently accepted', () => {
    const file = path.join(scratch('legacy-unparseable'), 'chtypes.lock');
    writeFileSync(file, JSON.stringify({ schema: 1, artifacts: { 'linux-arm64/25.8': { file: 'not-an-asset-name.tar.gz', sha256: 'd'.repeat(64) } } }));
    expect(() => readLock(file)).toThrow(/does not name a ClickHouse version/);
  });
});

// ---------------------------------------------------- two-patches/ (§8, §9)

const HAVE_FIXTURES = existsSync(path.join(SPEC_FIXTURES, 'two-patches', 'index.json'));
if (!HAVE_FIXTURES) {
  console.warn(`\n[chtypes] tests/fixtures/fetch/two-patches is ABSENT (${SPEC_FIXTURES}): the #284 patch-resolution fetch vectors are SKIPPED.\n`);
}

interface PatchRow {
  file: string;
  sha256: string;
  library_sha256: string;
}
interface PatchesCase {
  platform: string;
  line: string;
  patches: Record<string, PatchRow>;
  exact_miss: { requested: string; resolves_to: { clickhouse_version: string; file: string; sha256: string } };
}
interface Patches {
  fixture: string;
  line: string;
  newest: string;
  served: string[];
  abi_revision: number;
  cases: PatchesCase[];
}

/** Numeric, dot-by-dot order, channel ignored — never lexical (a fixture may carry a .9./.10.-shaped disagreement on purpose). */
function byPatchNumber(a: string, b: string): number {
  const pa = a.replace(/-[a-z]+$/, '').split('.').map(Number);
  const pb = b.replace(/-[a-z]+$/, '').split('.').map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] ?? 0) - (pb[i] ?? 0);
    if (d !== 0) return d;
  }
  return 0;
}

/** The `library` field of an already-installed patch directory's own manifest.json. */
function installedLibraryName(dir: string): string {
  return (JSON.parse(readFileSync(path.join(dir, 'manifest.json'), 'utf8')) as { library: string }).library;
}

describe.skipIf(!HAVE_FIXTURES)('tests/fixtures/fetch/two-patches (issue #284, P1-P10 + demotion)', () => {
  const expected = JSON.parse(readFileSync(path.join(SPEC_FIXTURES, 'expected.json'), 'utf8')) as { patches?: Patches };
  const patches = expected.patches;
  if (patches === undefined) {
    throw new Error('expected.json carries no "patches" block — regenerate the fixtures (issue #284, #289)');
  }
  const fixtureCase = patches.cases.find((c) => c.platform === PLATFORM) ?? patches.cases[0]!;
  const LINE = fixtureCase.line;
  const patchVersions = Object.keys(fixtureCase.patches);
  if (patchVersions.length !== 2) {
    throw new Error(`two-patches/ expected.json names ${patchVersions.length} patch(es) for ${fixtureCase.platform}, not 2`);
  }
  const [OLD, NEW] = [...patchVersions].sort(byPatchNumber) as [string, string];
  if (NEW !== patches.newest) {
    throw new Error(`two-patches/ expected.json: newest is ${JSON.stringify(patches.newest)}, numeric order picked ${JSON.stringify(NEW)}`);
  }

  let fixtureRevision: number;
  beforeAll(() => {
    fixtureRevision = fixtureAbiRevision(SPEC_FIXTURES);
    FETCH_ABI_REVISION.override = fixtureRevision;
  });
  afterAll(() => {
    FETCH_ABI_REVISION.override = null;
  });

  const url = pathToFileURL(path.join(SPEC_FIXTURES, 'two-patches')).href;
  const fixtureOpts = (dest: string, more: Partial<EnsureOptions> = {}): EnsureOptions => ({
    url,
    dest,
    platform: PLATFORM,
    allowUnsigned: true, // the vectors' signature chain is fetch.test.ts's job; this file is about resolution and layout
    ...more,
  });

  async function codeOf(p: Promise<unknown>): Promise<string> {
    try {
      await p;
    } catch (err) {
      if (err instanceof FetchError) return err.code;
      throw err;
    }
    throw new Error('expected a FetchError');
  }

  it('P1/P2: a line fetch installs the NEWEST patch flat; an exact fetch of the older one installs it under patches/, never flat', async () => {
    const dest = scratch('p1-p2');
    const r1 = await ensure(OLD, fixtureOpts(dest));
    expect(r1.version).toBe(OLD);
    expect(r1.dir).toBe(path.join(dest, 'patches', LINE, OLD));
    expect(existsSync(path.join(dest, LINE))).toBe(false); // flat is untouched by a patch request

    const r2 = await ensure(LINE, fixtureOpts(dest));
    expect(r2.version).toBe(NEW);
    expect(r2.dir).toBe(path.join(dest, LINE)); // the line's pick installs flat

    // Both patches now coexist, each hashing its own library_sha256 — the
    // second fetch did not touch the first.
    const oldDir = path.join(dest, 'patches', LINE, OLD);
    const newDir = path.join(dest, LINE);
    expect(await sha256File(path.join(oldDir, installedLibraryName(oldDir)))).toBe(fixtureCase.patches[OLD]!.library_sha256);
    expect(await sha256File(path.join(newDir, installedLibraryName(newDir)))).toBe(fixtureCase.patches[NEW]!.library_sha256);
  });

  it("an EXACT request for the line's newest patch, into an empty destination, still installs under patches/ — never flat (cross-binding placement rule)", async () => {
    const dest = scratch('exact-newest-empty');
    const r = await ensure(NEW, fixtureOpts(dest));
    expect(r.dir).toBe(path.join(dest, 'patches', LINE, NEW));
    expect(existsSync(path.join(dest, LINE))).toBe(false);
    expect(readdirSync(dest)).toEqual(['patches']);
  });

  it('demotion: a line fetch that changes the flat occupant demotes the old one into patches/, byte-identical, never deleting it', async () => {
    const dest = scratch('demote');
    // Seed flat with OLD by pointing a LINE request at a release that offers
    // only OLD, then fetch the real two-patches line — which now picks NEW —
    // against the destination that already has OLD sitting flat.
    const oldOnly = pathToFileURL(path.join(SPEC_FIXTURES, 'two-patches')).href;
    await ensure(OLD, { url: oldOnly, dest, platform: PLATFORM, allowUnsigned: true }); // installs OLD under patches/ (an exact request)
    // Promote OLD into the flat slot the way a real deployment would end up
    // there: a line fetch against a release that (at the time) served only
    // OLD is not reproducible from this one release, so instead assert the
    // documented invariant directly — fetch the LINE now (which serves NEW)
    // and confirm OLD, already installed under patches/, is left untouched.
    const oldDirBefore = path.join(dest, 'patches', LINE, OLD);
    const beforeHash = await sha256File(path.join(oldDirBefore, installedLibraryName(oldDirBefore)));

    await ensure(LINE, fixtureOpts(dest));
    expect(existsSync(path.join(dest, LINE, 'manifest.json'))).toBe(true);
    const newDir = path.join(dest, LINE);
    expect(await sha256File(path.join(newDir, installedLibraryName(newDir)))).toBe(fixtureCase.patches[NEW]!.library_sha256);

    // The older install is untouched, still under patches/, still byte-identical.
    const oldDirAfter = path.join(dest, 'patches', LINE, OLD);
    expect(existsSync(path.join(oldDirAfter, 'manifest.json'))).toBe(true);
    expect(await sha256File(path.join(oldDirAfter, installedLibraryName(oldDirAfter)))).toBe(beforeHash);
  });

  it('demotion, driven from the flat slot directly: fetching a DIFFERENT patch than the one already flat moves the outgoing one into patches/', async () => {
    // This is the literal design scenario: something is flat, a line fetch
    // picks a DIFFERENT patch, and the outgoing install is demoted rather
    // than deleted. Build it with a synthetic index limited to OLD, install
    // it as a normal LINE fetch (so it lands flat), then point the same
    // destination at the real two-patches release, whose line request picks
    // NEW — the flat occupant must change from OLD to NEW, and OLD must
    // reappear, byte-identical, under patches/.
    const dest = scratch('demote-flat-swap');
    const solo = scratch('demote-flat-swap-solo-release');
    // A one-artifact release built by copying the real OLD row's files, so
    // its bytes match fixtureCase.patches[OLD] exactly.
    const realDir = path.join(SPEC_FIXTURES, 'two-patches');
    const oldRow = fixtureCase.patches[OLD]!;
    for (const name of ['index.json', 'SHA256SUMS', 'SHA256SUMS.sig', oldRow.file]) {
      writeFileSync(path.join(solo, name), readFileSync(path.join(realDir, name)));
    }
    const realIndex = JSON.parse(readFileSync(path.join(realDir, 'index.json'), 'utf8')) as {
      schema: number;
      artifacts: Record<string, unknown>[];
    };
    const oldRowsOnly = realIndex.artifacts.filter((a) => (a as { file: string }).file === oldRow.file);
    writeFileSync(path.join(solo, 'index.json'), JSON.stringify({ ...realIndex, artifacts: oldRowsOnly }));
    // SHA256SUMS/.sig cover the whole real release; a subset index still
    // verifies against them since every listed hash is still correct.
    await ensure(LINE, { url: pathToFileURL(solo).href, dest, platform: PLATFORM, allowUnsigned: true });
    expect(existsSync(path.join(dest, LINE, 'manifest.json'))).toBe(true);
    expect(await sha256File(path.join(dest, LINE, installedLibraryName(path.join(dest, LINE))))).toBe(oldRow.library_sha256);
    expect(existsSync(path.join(dest, 'patches'))).toBe(false); // nothing demoted yet: OLD is the only thing that ever existed

    // Now the real two-patches release: the line's pick is NEW, which differs
    // from what is flat (OLD) — demote OLD, then NEW takes the flat slot.
    await ensure(LINE, fixtureOpts(dest));
    expect(await sha256File(path.join(dest, LINE, installedLibraryName(path.join(dest, LINE))))).toBe(fixtureCase.patches[NEW]!.library_sha256);
    const demotedDir = path.join(dest, 'patches', LINE, OLD);
    expect(existsSync(path.join(demotedDir, 'manifest.json'))).toBe(true);
    expect(await sha256File(path.join(demotedDir, installedLibraryName(demotedDir)))).toBe(oldRow.library_sha256);
    // An exact request for OLD now resolves to the demoted copy with no fetch.
    const stillThere = await ensure(OLD, fixtureOpts(dest, { offline: true }));
    expect(stillThere.dir).toBe(demotedDir);
  });

  it('P3: fetching the older patch WITHOUT its channel matches it (Decision 7); a genuinely missing patch is CHTYPES_ARTIFACT_UNPUBLISHED and never falls back', async () => {
    const bare = OLD.replace(/-[a-z]+$/, '');
    const dest = scratch('p3');
    const r = await ensure(bare, fixtureOpts(dest));
    expect(r.version).toBe(OLD);
    expect(await codeOf(ensure(fixtureCase.exact_miss.requested, fixtureOpts(scratch('p3-miss'))))).toBe('CHTYPES_ARTIFACT_UNPUBLISHED');
  });

  it('P4: fetching the older patch then the line with --lock records BOTH patches, schema 2, and removes neither', async () => {
    const dest = scratch('p4');
    const lock = path.join(scratch('p4-lock'), 'chtypes.lock');
    await ensure(OLD, fixtureOpts(dest, { lock }));
    await ensure(LINE, fixtureOpts(dest, { lock }));
    const read = readLock(lock)!;
    expect(read.schema).toBe(LOCK_SCHEMA);
    expect(Object.keys(read.artifacts).sort()).toEqual([`${PLATFORM}/${NEW}`, `${PLATFORM}/${OLD}`].sort());
    expect(read.artifacts[`${PLATFORM}/${OLD}`]!.abi_revision).toBe(fixtureRevision);
    expect(read.artifacts[`${PLATFORM}/${NEW}`]!.abi_revision).toBe(fixtureRevision);
  });

  it('P5 (the headline case): a lock pinning only the OLDER patch installs it under --frozen, while the newer patch is also served — no CHTYPES_ARTIFACT_PINNED', async () => {
    const lock = path.join(scratch('p5-lock'), 'chtypes.lock');
    await ensure(OLD, fixtureOpts(scratch('p5-seed'), { lock }));
    expect(Object.keys(readLock(lock)!.artifacts)).toEqual([`${PLATFORM}/${OLD}`]);

    const dest = scratch('p5-dest');
    const r = await ensure(LINE, fixtureOpts(dest, { lock, frozen: true }));
    expect(r.version).toBe(OLD);
    expect(r.installed).toBe(true);
  });

  it('P6: an exact fetch of the OLDER patch installs under --frozen; the NEWER patch is CHTYPES_ARTIFACT_PINNED (the lock pins nothing for it)', async () => {
    const lock = path.join(scratch('p6-lock'), 'chtypes.lock');
    await ensure(OLD, fixtureOpts(scratch('p6-seed'), { lock }));
    const r = await ensure(OLD, fixtureOpts(scratch('p6-dest'), { lock, frozen: true }));
    expect(r.installed).toBe(true);
    const refused = await ensure(NEW, fixtureOpts(scratch('p6-dest2'), { lock, frozen: true })).catch((e: unknown) => e);
    expect(refused).toBeInstanceOf(ArtifactPinnedError);
  });

  it('P7: a schema-1 lock pinning the older patch is read, installs under --frozen, and a re-lock rewrites it as schema 2 holding both patches', async () => {
    const legacy = path.join(scratch('p7-lock'), 'chtypes.lock');
    const oldRow = fixtureCase.patches[OLD]!;
    writeFileSync(legacy, JSON.stringify({ schema: 1, artifacts: { [`${PLATFORM}/${LINE}`]: { file: oldRow.file, sha256: oldRow.sha256 } } }));
    const dest = scratch('p7-dest');
    const r = await ensure(LINE, fixtureOpts(dest, { lock: legacy, frozen: true }));
    expect(r.version).toBe(OLD);
    // Now re-lock (non-frozen): rewrites the WHOLE file as schema 2, and the
    // line request re-pins to the NEWEST — the newer patch also gets an entry.
    await ensure(LINE, fixtureOpts(dest, { lock: legacy }));
    const relocked = readLock(legacy)!;
    expect(relocked.schema).toBe(LOCK_SCHEMA);
    expect(Object.keys(relocked.artifacts).sort()).toEqual([`${PLATFORM}/${NEW}`, `${PLATFORM}/${OLD}`].sort());
  });

  it('P8: --all --frozen installs only what the lock pins (the older patch), not the newer one the release also serves', async () => {
    const lock = path.join(scratch('p8-lock'), 'chtypes.lock');
    await ensure(OLD, fixtureOpts(scratch('p8-seed'), { lock }));
    const { ensureAll } = await import('../src/index.js');
    const results = await ensureAll(fixtureOpts(scratch('p8-dest'), { lock, frozen: true }));
    expect(results.map((r) => r.version)).toEqual([OLD]);
  });

  it('P9: offline resolves the bare-channel spelling to the older patch, the line to the newer, and a genuinely missing patch is CHTYPES_SOURCE_UNREACHABLE; --offline --frozen with no lock file still succeeds', async () => {
    const dest = scratch('p9');
    await ensure(OLD, fixtureOpts(dest));
    await ensure(LINE, fixtureOpts(dest));
    const bare = OLD.replace(/-[a-z]+$/, '');
    const r1 = await ensure(bare, fixtureOpts(dest, { offline: true }));
    expect(r1.version).toBe(OLD);
    const r2 = await ensure(LINE, fixtureOpts(dest, { offline: true }));
    expect(r2.version).toBe(NEW);
    expect(await codeOf(ensure(fixtureCase.exact_miss.requested, fixtureOpts(dest, { offline: true })))).toBe('CHTYPES_SOURCE_UNREACHABLE');
    // --offline --frozen with no lock file at all: Decision 6, the lock is
    // never read offline, so this still succeeds instead of CHTYPES_ARTIFACT_PINNED.
    const r3 = await ensure(LINE, fixtureOpts(dest, { offline: true, frozen: true, lock: path.join(scratch('p9-no-lock'), 'absent.lock') }));
    expect(r3.version).toBe(NEW);
  });

  it('P10: list and verify report BOTH patches, one record each', async () => {
    const dest = scratch('p10');
    await ensure(OLD, fixtureOpts(dest));
    await ensure(LINE, fixtureOpts(dest));
    const verified = await verifyInstalled(dest, PLATFORM);
    expect(verified.map((v) => v.version).sort()).toEqual([NEW, OLD].sort());
    expect(verified.every((v) => v.ok)).toBe(true);
    const listing = await listArtifacts(fixtureOpts(dest, { offline: true }));
    expect(listing.installed.map((i) => i.version).sort()).toEqual([NEW, OLD].sort());
  });

  it('resolution against two real installed patches, without dlopen: Registry#for/#resolve reach the right directory', async () => {
    // Every fixture "library" is a fake placeholder (hashes correctly, can
    // never dlopen) — exactly the property that proves WHICH directory
    // resolution picked, the same way the no-dlopen section above does.
    const dest = scratch('registry-two-patches');
    await ensure(OLD, fixtureOpts(dest));
    await ensure(LINE, fixtureOpts(dest));
    const registry = new Registry(dest);
    const oldDir = path.join(dest, 'patches', LINE, OLD);
    const newDir = path.join(dest, LINE);

    let thrownOld: unknown;
    try {
      registry.for(OLD);
    } catch (err) {
      thrownOld = err;
    }
    expect(thrownOld).toBeInstanceOf(RegistryError);
    expect((thrownOld as Error).message).toContain(oldDir);

    let thrownNew: unknown;
    try {
      registry.for(LINE);
    } catch (err) {
      thrownNew = err;
    }
    expect(thrownNew).toBeInstanceOf(RegistryError);
    expect((thrownNew as Error).message).toContain(newDir);

    // resolve()'s fallback path reaches the newest patch too, for a patch the
    // release does not publish (strictly between OLD and NEW in patch number).
    let thrownMiss: unknown;
    try {
      registry.resolve(fixtureCase.exact_miss.requested);
    } catch (err) {
      thrownMiss = err;
    }
    expect(thrownMiss).toBeInstanceOf(RegistryError);
    expect((thrownMiss as Error).message).toContain(newDir);
  });
});
