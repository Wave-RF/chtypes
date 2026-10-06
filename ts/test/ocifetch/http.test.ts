/**
 * The retry table and `Retry-After` parsing (`docs/guides/fetch-v1.md` §7) —
 * driven with an injected clock, so every case here runs instantly and
 * asserts the exact sleep sequence, per the plan's "sleeps are asserted
 * exactly" rule. No network, no fixtures tree.
 */

import { readFileSync } from 'node:fs';
import * as http from 'node:http';
import type { AddressInfo } from 'node:net';
import { describe, expect, it } from 'vitest';
import { SourceUnreachableError } from '../../src/ocifetch/errors.js';
import { parseRetryAfterSeconds, RETRY_AFTER_BUDGET_S, requestBuffered, USER_AGENT, withRetry } from '../../src/ocifetch/http.js';
import type { Clock } from '../../src/ocifetch/types.js';

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

describe('withRetry', () => {
  it('succeeds without sleeping when the first attempt is ok', async () => {
    const clock = recordingClock();
    const result = await withRetry(clock, async () => ({
      outcome: { ok: true, retryable: false, status: 200, retryAfterSeconds: undefined, message: '' },
      value: 'done',
    }));
    expect(result).toBe('done');
    expect(clock.sleeps).toEqual([]);
  });

  it('retries 5xx with the doubling schedule 4,8,16,32 and then throws naming 5 attempts', async () => {
    const clock = recordingClock();
    let attempts = 0;
    await expect(
      withRetry(clock, async () => {
        attempts += 1;
        return {
          outcome: { ok: false, retryable: true, status: 503, retryAfterSeconds: undefined, message: `attempt ${attempts}` },
        };
      }),
    ).rejects.toThrow(SourceUnreachableError);
    expect(attempts).toBe(5);
    expect(clock.sleeps).toEqual([4, 8, 16, 32]);
  });

  it('stops after two retries once the third attempt succeeds (sleeps [4,8])', async () => {
    const clock = recordingClock();
    let attempts = 0;
    const result = await withRetry(clock, async () => {
      attempts += 1;
      if (attempts < 3) {
        return { outcome: { ok: false, retryable: true, status: 500, retryAfterSeconds: undefined, message: '' } };
      }
      return { outcome: { ok: true, retryable: false, status: 200, retryAfterSeconds: undefined, message: '' }, value: 'ok' };
    });
    expect(result).toBe('ok');
    expect(clock.sleeps).toEqual([4, 8]);
  });

  it('does not retry a non-retryable failure', async () => {
    const clock = recordingClock();
    let attempts = 0;
    await expect(
      withRetry(clock, async () => {
        attempts += 1;
        return { outcome: { ok: false, retryable: false, status: 400, retryAfterSeconds: undefined, message: 'bad request' } };
      }),
    ).rejects.toThrow(SourceUnreachableError);
    expect(attempts).toBe(1);
    expect(clock.sleeps).toEqual([]);
  });

  it('a Retry-After value overrides the scheduled wait for that attempt', async () => {
    const clock = recordingClock();
    let attempts = 0;
    const result = await withRetry(clock, async () => {
      attempts += 1;
      if (attempts === 1) {
        return { outcome: { ok: false, retryable: true, status: 429, retryAfterSeconds: 2, message: '' } };
      }
      return { outcome: { ok: true, retryable: false, status: 200, retryAfterSeconds: undefined, message: '' }, value: 'ok' };
    });
    expect(result).toBe('ok');
    expect(clock.sleeps).toEqual([2]);
  });

  it('refuses (never sleeps) a Retry-After past the budget, naming the value', async () => {
    const clock = recordingClock();
    const overBudget = RETRY_AFTER_BUDGET_S + 1;
    await expect(
      withRetry(clock, async () => ({
        outcome: { ok: false, retryable: true, status: 503, retryAfterSeconds: overBudget, message: '' },
      })),
    ).rejects.toThrow(/Retry-After/);
    expect(clock.sleeps).toEqual([]);
  });
});

describe('parseRetryAfterSeconds', () => {
  const clock: Clock = { now: () => Date.parse('2026-10-01T00:00:00Z'), sleep: async () => {} };

  it('parses the delta-seconds form', () => {
    expect(parseRetryAfterSeconds('2', undefined, clock)).toBe(2);
  });

  it('parses an HTTP-date relative to the response Date header', () => {
    const seconds = parseRetryAfterSeconds('Thu, 01 Oct 2026 00:00:03 GMT', 'Thu, 01 Oct 2026 00:00:00 GMT', clock);
    expect(seconds).toBe(3);
  });

  it('falls back to clock.now() when there is no response Date header', () => {
    const seconds = parseRetryAfterSeconds('Thu, 01 Oct 2026 00:00:05 GMT', undefined, clock);
    expect(seconds).toBe(5);
  });

  it('never returns a negative delay for a date already in the past', () => {
    const seconds = parseRetryAfterSeconds('Wed, 30 Sep 2026 00:00:00 GMT', undefined, clock);
    expect(seconds).toBe(0);
  });
});

describe('User-Agent', () => {
  it('is chtypes-ts/<package.json version> and matches the delivery pattern', () => {
    const manifest = JSON.parse(readFileSync(new URL('../../package.json', import.meta.url), 'utf8')) as { version: string };
    expect(USER_AGENT).toMatch(/^chtypes-(go|python|ts|rust)\/[0-9A-Za-z.+-]+$/);
    expect(USER_AGENT).toBe(`chtypes-ts/${manifest.version}`);
  });

  it('is sent on every hop of a redirect chain', async () => {
    const seen: Array<string | undefined> = [];
    const server = http.createServer((req, res) => {
      seen.push(req.headers['user-agent']);
      if (req.url === '/start') {
        res.writeHead(302, { location: '/end' });
        res.end();
        return;
      }
      res.writeHead(200);
      res.end('ok');
    });
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    try {
      const { port } = server.address() as AddressInfo;
      const clock: Clock = { now: () => 0, sleep: async () => {} };
      await requestBuffered(`http://127.0.0.1:${port}/start`, { maxBytes: 1024, clock });
      expect(seen).toEqual([USER_AGENT, USER_AGENT]);
    } finally {
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  });
});
