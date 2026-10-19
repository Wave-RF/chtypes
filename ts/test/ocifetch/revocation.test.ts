/**
 * Build-level revocation (`docs/guides/fetch-v1.md`, "Build-level revocation";
 * public issue #625): the parts the conformance cases cannot reach. The 410 table
 * every binding reads, the v1 contract doing nothing, the override being dropped
 * for a lock and an update, and the installed-build check's in-process memo and
 * 5 s deadline (shortened here).
 */

import { readFileSync } from 'node:fs';
import * as http from 'node:http';
import type { AddressInfo } from 'node:net';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { useFetchV1ForTests, useProdV2ForTests } from '../../src/ocifetch/channel.js';
import { ERROR_EXIT_CODES, REVOCATION_CHECK_S } from '../../src/ocifetch/constants.gen.js';
import { checkInstalled, revocationContext } from '../../src/ocifetch/ensure.js';
import { ArtifactRevokedError, SourceRetiredError, SourceUnreachableError } from '../../src/ocifetch/errors.js';
import { goneError } from '../../src/ocifetch/retired.js';
import { resetRevocationMemoForTests, setRevocationCheckDeadlineForTests } from '../../src/ocifetch/revocation.js';
import type { Clock, Resolved } from '../../src/ocifetch/types.js';

interface TableRow {
  readonly name: string;
  readonly body_text?: string;
  readonly body_hex?: string;
  readonly code: string;
  readonly revoked?: {
    readonly text: string;
    readonly advisories: readonly {
      readonly id: string;
      readonly url: string;
      readonly class: string;
      readonly summary: string;
      readonly build: string;
      readonly superseded_by: string;
    }[];
  };
}

const table = JSON.parse(readFileSync(new URL('../../../tests/fixtures/revoked-410/cases.json', import.meta.url), 'utf8')) as {
  cases: TableRow[];
};

const URL_410 = 'https://registry.example/v2/chtypes/v2/manifests/26.9.8.3';

function bodyOf(row: TableRow): Buffer {
  return row.body_text !== undefined ? Buffer.from(row.body_text, 'utf8') : Buffer.from(row.body_hex ?? '', 'hex');
}

describe('goneError: the shared table (tests/fixtures/revoked-410/cases.json)', () => {
  let restore: () => void;
  beforeEach(() => {
    restore = useProdV2ForTests();
  });
  afterEach(() => restore());

  it('has rows', () => {
    expect(table.cases.length).toBeGreaterThan(0);
    expect(table.cases.some((c) => c.revoked !== undefined)).toBe(true);
  });

  for (const row of table.cases) {
    it(row.name, () => {
      const err = goneError(URL_410, 410, bodyOf(row), true);
      expect(err.code).toBe(row.code);
      if (row.revoked === undefined) {
        expect(err).toBeInstanceOf(SourceRetiredError);
        return;
      }
      expect(err).toBeInstanceOf(ArtifactRevokedError);
      expect(err.message).toBe(`chtypes: ${URL_410} answered 410 Gone: ${row.revoked.text}`);
      const rev = (err as ArtifactRevokedError).revocation;
      expect(rev.signed).toBe(false);
      expect(rev.manifest).toBe('');
      expect(rev.advisories.map((a) => ({ id: a.id, url: a.url, class: a.class, summary: a.summary, build: a.build, superseded_by: a.supersededBy }))).toEqual(
        row.revoked.advisories,
      );
    });
  }

  it('is a retired repository for every row under the v1 contract, which has no revocation', () => {
    for (const row of table.cases) {
      const err = goneError(URL_410, 410, bodyOf(row), false);
      expect(err).toBeInstanceOf(SourceRetiredError);
      expect(err.code).toBe('CHTYPES_SOURCE_RETIRED');
    }
  });
});

describe('the exit status', () => {
  it('is 11 for a revoked build', () => {
    expect(ERROR_EXIT_CODES['CHTYPES_ARTIFACT_REVOKED']).toBe(11);
  });
});

describe('the override', () => {
  it('is dropped for a lock and an update, and kept alone', () => {
    const restore = useProdV2ForTests();
    try {
      expect(revocationContext({ allowRevoked: true, lockWrite: true }).allowRevoked).toBe(false);
      expect(revocationContext({ allowRevoked: true, update: true }).allowRevoked).toBe(false);
      expect(revocationContext({ allowRevoked: true }).allowRevoked).toBe(true);
      expect(revocationContext({}).allowRevoked).toBe(false);
      expect(revocationContext({ requireRevocationCheck: true }).requireRevocationCheck).toBe(true);
    } finally {
      restore();
    }
  });
});

const RESOLVED = {
  abiGeneration: 2,
  platform: 'linux-amd64',
  request: '26.9',
  version: '26.9.1.1',
  build: '20261001.000001',
  libraryPath: '',
  dir: '',
  digests: { manifest: `sha256:${'a'.repeat(64)}`, layer: `sha256:${'b'.repeat(64)}`, bundle: '' },
  predicate: {},
  signedBy: '',
  source: 'cache',
  alreadyInstalled: true,
  warnings: [],
} as unknown as Resolved;

const clock: Clock = { now: () => Date.now(), sleep: async () => {} };

/** A registry that accepts every request and never answers it. */
async function withStalledRegistry(run: (base: string, requests: string[]) => Promise<void>): Promise<void> {
  const requests: string[] = [];
  const server = http.createServer((req) => {
    requests.push(req.url ?? '');
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const { port } = server.address() as AddressInfo;
    await run(`http://127.0.0.1:${port}/chtypes/v2`, requests);
  } finally {
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}

describe('the installed-build check', () => {
  let restore: () => void;
  beforeEach(() => {
    restore = useProdV2ForTests();
    resetRevocationMemoForTests();
    setRevocationCheckDeadlineForTests(0.3);
  });
  afterEach(() => {
    setRevocationCheckDeadlineForTests(undefined);
    resetRevocationMemoForTests();
    restore();
  });

  it('has the guide\'s 5 s deadline', () => {
    expect(REVOCATION_CHECK_S).toBe(5);
  });

  it('passes its deadline as one warning, and then asks nothing more of that build in this process', async () => {
    await withStalledRegistry(async (base, requests) => {
      const options = { bases: [base], clock };
      const first = await checkInstalled(RESOLVED, options);
      expect(first.error).toBeUndefined();
      expect(first.warnings.length).toBe(1);
      expect(first.warnings[0]).toContain(`chtypes: revocation status not checked for manifest ${RESOLVED.digests.manifest} (bases: ${base}): the check passed its 0.3s deadline`);
      expect(requests.length).toBe(1);

      // The kept "not checked" answers a call without strict mode: no request, no second warning.
      const second = await checkInstalled(RESOLVED, options);
      expect(second.error).toBeUndefined();
      expect(second.warnings).toEqual([]);
      expect(requests.length).toBe(1);
    });
  });

  it('is the transport\'s error in strict mode, which never takes a kept "not checked"', async () => {
    await withStalledRegistry(async (base, requests) => {
      const options = { bases: [base], clock };
      expect((await checkInstalled(RESOLVED, options)).error).toBeUndefined();
      expect(requests.length).toBe(1);

      const strict = await checkInstalled(RESOLVED, { ...options, requireRevocationCheck: true });
      expect(strict.error).toBeInstanceOf(SourceUnreachableError);
      expect(strict.error?.code).toBe('CHTYPES_SOURCE_UNREACHABLE');
      expect(strict.error?.message).toContain('revocation status not checked for manifest');
      expect(requests.length).toBe(2);
    });
  });

  it('makes no request offline', async () => {
    await withStalledRegistry(async (base, requests) => {
      const checked = await checkInstalled(RESOLVED, { bases: [base], clock, offline: true });
      expect(checked).toEqual({ warnings: [], error: undefined });
      expect(requests).toEqual([]);
    });
  });
});

describe('the v1 contract', () => {
  it('does nothing', async () => {
    const restore = useFetchV1ForTests();
    try {
      await withStalledRegistry(async (base, requests) => {
        const checked = await checkInstalled(RESOLVED, { bases: [base], clock });
        expect(checked).toEqual({ warnings: [], error: undefined });
        expect(requests).toEqual([]);
      });
    } finally {
      restore();
    }
  });
});
