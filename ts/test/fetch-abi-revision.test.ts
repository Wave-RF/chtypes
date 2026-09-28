/**
 * docs/guides/fetch.md §2: fetch selects only rows at the binding's own ABI
 * revision, FIRST, and the per-user cache is keyed by that revision.
 *
 * Synthetic listings, no network and no fixtures: the revision under test is
 * always `ABI_REVISION` itself (or one past it), never a typed number, so these
 * hold at whatever revision the binding speaks.
 */

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import {
  ABI_REVISION,
  ArtifactUnpublishedError,
  cacheRegistryDir,
  defaultRegistryDir,
  fetchDestination,
  hostPlatform,
  parseVersionSpelling,
  registrySearchPath,
  selectAll,
  selectArtifact,
  type IndexArtifact,
  type ReleaseIndex,
} from '../src/index.js';
// FETCH_ABI_REVISION is the internal, test-only override, and skippedLines /
// notShown the internal helpers behind --all's and list's lines (none of them
// re-exported from index.js).
import { FETCH_ABI_REVISION, notShown, skippedLines } from '../src/fetch.js';
import { fixtureAbiRevision } from './fixture-revision.js';

const OWN = ABI_REVISION;
const OTHER = ABI_REVISION + 1;
const PLATFORM = 'linux-amd64';

function row(version: string, build: number, revision: number | null): IndexArtifact {
  return {
    os: 'linux',
    arch: 'amd64',
    file: `chtypes-${version}-${PLATFORM}-b${build}.tar.gz`,
    sha256: 'a'.repeat(64),
    bytes: 1,
    clickhouse_version: version,
    clickhouse_minor: version.split('.').slice(0, 2).join('.'),
    library: 'libchtypes.so',
    library_sha256: 'b'.repeat(64),
    build,
    core_commit: '',
    // A row that declares no revision simply has no such field.
    ...(revision === null ? {} : { abi_revision: revision }),
  };
}

const index = (...artifacts: IndexArtifact[]): ReleaseIndex => ({ schema: 1, artifacts });

const savedXdg = process.env['XDG_CACHE_HOME'];
const savedRegistry = process.env['CHTYPES_REGISTRY'];
afterEach(() => {
  FETCH_ABI_REVISION.override = null;
  if (savedXdg === undefined) delete process.env['XDG_CACHE_HOME'];
  else process.env['XDG_CACHE_HOME'] = savedXdg;
  if (savedRegistry === undefined) delete process.env['CHTYPES_REGISTRY'];
  else process.env['CHTYPES_REGISTRY'] = savedRegistry;
});

describe('fetch selects only the binding’s own ABI revision (docs/guides/fetch.md §2)', () => {
  it('(a) a higher build at another revision, or a newer undeclared row, never beats the own-revision row', () => {
    expect(FETCH_ABI_REVISION.override).toBeNull();
    const mine = row('25.8.28.1-lts', 10, OWN);
    const higherBuildElsewhere = row('25.8.28.1-lts', 20, OTHER);
    const newerUndeclared = row('25.8.33.6-lts', 30, null);
    for (const order of [
      [mine, higherBuildElsewhere, newerUndeclared],
      [newerUndeclared, higherBuildElsewhere, mine],
      [higherBuildElsewhere, mine, newerUndeclared],
    ]) {
      for (const spelling of ['25.8', '25.8.28.1-lts', '25.8.28.1']) {
        expect(selectArtifact(index(...order), PLATFORM, parseVersionSpelling(spelling)).file).toBe(mine.file);
      }
      expect(selectAll(index(...order), PLATFORM).map((a) => a.file)).toEqual([mine.file]);
    }
  });

  it('(b) only another revision served is CHTYPES_ARTIFACT_UNPUBLISHED, naming both revisions', () => {
    const only = index(row('25.8.28.1-lts', 20, OTHER), row('26.7.3.19-stable', 20, OTHER));
    for (const attempt of [
      () => selectArtifact(only, PLATFORM, parseVersionSpelling('25.8')),
      () => selectArtifact(only, PLATFORM, parseVersionSpelling('25.8.28.1-lts')),
      () => selectAll(only, PLATFORM),
    ]) {
      let err: unknown;
      try {
        attempt();
      } catch (e) {
        err = e;
      }
      expect(err).toBeInstanceOf(ArtifactUnpublishedError);
      expect((err as ArtifactUnpublishedError).code).toBe('CHTYPES_ARTIFACT_UNPUBLISHED'); // exit 4, fetch.md §6
      expect((err as Error).message).toContain(`ABI revision ${OWN} (this SDK's)`);
      expect((err as Error).message).toContain(`only at ABI revision ${OTHER}`);
    }
  });

  it('(c) a row without an abi_revision is never selected, even when it is the only one', () => {
    const undeclared = index(row('25.8.28.1-lts', 0, null));
    expect(() => selectArtifact(undeclared, PLATFORM, parseVersionSpelling('25.8'))).toThrow(ArtifactUnpublishedError);
    // The reason is named — rows built before revisions were recorded — never
    // that the release serves "none".
    let err: unknown;
    try {
      selectArtifact(undeclared, PLATFORM, parseVersionSpelling('25.8'));
    } catch (e) {
      err = e;
    }
    expect((err as Error).message).toContain(
      `the release has that line for ${PLATFORM} only in rows that record no ABI revision (built before revisions were recorded)`,
    );
    expect((err as Error).message).toContain(`ABI revision ${OWN} (this SDK's)`);
    expect((err as Error).message).not.toContain('none');
    expect(() => selectAll(undeclared, PLATFORM)).toThrow(ArtifactUnpublishedError);
  });

  it('--all names every line served only at another revision, in one loud line each, oldest first', () => {
    const release = index(row('25.8.28.1-lts', 1, OWN), row('26.7.3.19-stable', 1, OTHER), row('24.8.14.39-lts', 1, null));
    expect(selectAll(release, PLATFORM).map((a) => a.clickhouse_minor)).toEqual(['25.8']);
    expect(skippedLines(release, PLATFORM)).toEqual([
      `ClickHouse line 24.8 on ${PLATFORM} is not installed: the release has that line for ${PLATFORM} only in rows that record no ABI revision (built before revisions were recorded), and this SDK speaks ABI revision ${OWN}`,
      `ClickHouse line 26.7 on ${PLATFORM} is not installed: the release has that line for ${PLATFORM} only at ABI revision ${OTHER}, and this SDK speaks ABI revision ${OWN}`,
    ]);
    expect(skippedLines(index(row('25.8.28.1-lts', 1, OWN)), PLATFORM)).toEqual([]);
  });

  it('list names what it hides in one line, and says nothing when it hid nothing', () => {
    expect(notShown(index(row('25.8.28.1-lts', 1, OWN)), PLATFORM)).toBeUndefined();
    expect(notShown(index(row('25.8.28.1-lts', 1, OWN), row('26.7.3.19-stable', 1, OTHER)), PLATFORM)).toBe(
      `1 row(s) at ABI revision(s) ${OTHER} not shown; this SDK speaks ${OWN}`,
    );
    expect(
      notShown(index(row('25.8.28.1-lts', 1, OWN), row('26.7.3.19-stable', 1, OTHER), row('24.8.14.39-lts', 1, null)), PLATFORM),
    ).toBe(
      `1 row(s) at ABI revision(s) ${OTHER} and 1 row(s) that record no ABI revision (built before revisions were recorded) not shown; this SDK speaks ${OWN}`,
    );
  });

  it('the per-user cache is abi<R>/, R the binding’s own constant — and the fetch override does not move it', () => {
    process.env['XDG_CACHE_HOME'] = '/tmp/xdg';
    delete process.env['CHTYPES_REGISTRY'];
    const want = path.join('/tmp/xdg', 'chtypes', 'artifacts', `abi${ABI_REVISION}`, hostPlatform());
    expect(defaultRegistryDir()).toBe(want);
    expect(cacheRegistryDir()).toBe(want);
    expect(fetchDestination()).toBe(want);
    expect(registrySearchPath()[0]).toBe(want);
    FETCH_ABI_REVISION.override = OTHER;
    expect(defaultRegistryDir()).toBe(want);
  });
});

describe('the fixture revision is derived from the fixtures, never typed', () => {
  const write = (root: string, release: string, ...revisions: (number | null)[]): void => {
    mkdirSync(path.join(root, release), { recursive: true });
    const artifacts = revisions.map((r) => (r === null ? { file: 'x' } : { file: 'x', abi_revision: r }));
    writeFileSync(path.join(root, release, 'index.json'), JSON.stringify({ schema: 1, artifacts }));
  };

  it('one agreed revision is the answer; disagreement, a missing one, or no release is an error', () => {
    const root = mkdtempSync(path.join(tmpdir(), 'chtypes-fixture-rev-'));
    try {
      expect(() => fixtureAbiRevision(root)).toThrow();
      write(root, 'one', 7, 7);
      write(root, 'two', 7);
      expect(fixtureAbiRevision(root)).toBe(7);
      write(root, 'three', 8);
      expect(() => fixtureAbiRevision(root)).toThrow(/disagree/);
      const other = path.join(root, 'other');
      write(other, 'one', 7, null);
      expect(() => fixtureAbiRevision(other)).toThrow(/no integer abi_revision/);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});
