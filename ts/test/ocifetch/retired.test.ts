/**
 * A retired repository (`docs/guides/fetch-v1.md` §2, "A retired repository";
 * public issue #571): the sanitizer against the table all four bindings read,
 * and a 410 over a real loopback server, which is never retried.
 */

import { readFileSync } from 'node:fs';
import * as http from 'node:http';
import type { AddressInfo } from 'node:net';
import { describe, expect, it } from 'vitest';
import { RETIRED_BODY_MAX_BYTES } from '../../src/ocifetch/constants.gen.js';
import { exitStatusOf, type FetchV1Error, SourceRetiredError } from '../../src/ocifetch/errors.js';
import { requestBuffered, requestToSink } from '../../src/ocifetch/http.js';
import { retiredMessage } from '../../src/ocifetch/retired.js';
import type { Clock } from '../../src/ocifetch/types.js';

interface TableCase {
  readonly name: string;
  readonly body_text?: string;
  readonly body_hex?: string;
  readonly message: string | null;
}

const table = JSON.parse(
  readFileSync(new URL('../../../tests/fixtures/retired-message/cases.json', import.meta.url), 'utf8'),
) as { cases: TableCase[] };

describe('retiredMessage: the shared table (tests/fixtures/retired-message/cases.json)', () => {
  for (const c of table.cases) {
    it(c.name, () => {
      const body = c.body_text !== undefined ? Buffer.from(c.body_text, 'utf8') : Buffer.from(c.body_hex ?? '', 'hex');
      expect(retiredMessage(body)).toBe(c.message);
    });
  }

  it('holds the kinds public issue #571 names', () => {
    const names = new Set(table.cases.map((c) => c.name));
    for (const name of ['c0-escape-sequence', 'bidi-override', 'over-cap-300', 'non-bmp-is-the-256th', 'empty-string', 'message-number']) {
      expect(names.has(name)).toBe(true);
    }
  });
});

const MESSAGE = 'chtypes/v1 is retired: use chtypes/v2 (this registry no longer serves chtypes/v1)';

async function withGoneServer(body: string, run: (origin: string, requests: string[]) => Promise<void>): Promise<void> {
  const requests: string[] = [];
  const server = http.createServer((req, res) => {
    requests.push(req.url ?? '');
    res.writeHead(410, { 'content-type': 'application/json' });
    res.end(body);
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    const { port } = server.address() as AddressInfo;
    await run(`http://127.0.0.1:${port}`, requests);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}

function recordingClock(): Clock & { sleeps: number[] } {
  const sleeps: number[] = [];
  return {
    sleeps,
    now: () => 0,
    sleep: async (seconds: number) => {
      sleeps.push(seconds);
    },
  };
}

describe('a 410 Gone', () => {
  it('is SourceRetiredError, exit 10, with the URL and the registry message, after one request and no sleep', async () => {
    await withGoneServer(JSON.stringify({ errors: [{ code: 'DENIED', message: MESSAGE }] }), async (origin, requests) => {
      const clock = recordingClock();
      const url = `${origin}/v2/chtypes/v1/manifests/26.9`;
      // The request's own cap (16 bytes) does not apply to a 410's message.
      const err = await requestBuffered(url, { maxBytes: 16, clock }).then(
        () => undefined,
        (e: unknown) => e,
      );
      expect(err).toBeInstanceOf(SourceRetiredError);
      expect((err as FetchV1Error).code).toBe('CHTYPES_SOURCE_RETIRED');
      expect(exitStatusOf((err as FetchV1Error).code)).toBe(10);
      expect((err as Error).message).toBe(`chtypes: ${url} answered 410 Gone: the repository is retired; the registry says: ${MESSAGE}`);
      expect(requests).toEqual(['/v2/chtypes/v1/manifests/26.9']);
      expect(clock.sleeps).toEqual([]);
    });
  });

  it('on a streamed blob is the same, and is never retried', async () => {
    await withGoneServer('', async (origin, requests) => {
      const clock = recordingClock();
      const url = `${origin}/v2/chtypes/v1/blobs/sha256:${'0'.repeat(64)}`;
      const sink = { reset: () => {}, write: () => {} };
      const err = await requestToSink(url, { maxBytes: 1 << 20, clock }, sink).then(
        () => undefined,
        (e: unknown) => e,
      );
      expect(err).toBeInstanceOf(SourceRetiredError);
      expect((err as Error).message).toBe(`chtypes: ${url} answered 410 Gone: the repository is retired`);
      expect(requests.length).toBe(1);
      expect(clock.sleeps).toEqual([]);
    });
  });

  it('reads at most the cap of its body, so a longer one carries no message', async () => {
    const long = JSON.stringify({ errors: [{ message: 'x'.repeat(RETIRED_BODY_MAX_BYTES) }] });
    await withGoneServer(long, async (origin) => {
      const err = await requestBuffered(`${origin}/v2/x`, { maxBytes: 1024, clock: recordingClock() }).then(
        () => undefined,
        (e: unknown) => e,
      );
      expect(err).toBeInstanceOf(SourceRetiredError);
      expect((err as Error).message).not.toContain('registry says');
    });
  });
});
