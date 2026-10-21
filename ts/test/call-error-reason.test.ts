/**
 * The sixth field of a call error (`docs/reference/bindings-v1.md` §4): `reason`, the catalog id
 * `chs_error_reason` carries for a decline or a library-side refusal. Pure: the classes are built
 * from the fields the generic reader (`src/abi2/raw.ts`) hands them, so no library is needed.
 */

import { describe, expect, it } from 'vitest';
import { type CallErrorFields, InternalError, internalError, SchemaError, UnsupportedError, usageError, UsageError } from '../src/abi2/errors.js';
import { errorForStatus } from '../src/abi2/errmap.gen.js';
import { Status } from '../src/abi2/vocab.gen.js';

const fields = (status: number): CallErrorFields => ({
  status,
  chCode: 53,
  chName: 'TYPE_MISMATCH',
  messageBytes: Buffer.from([0x6d, 0xff]),
  column: Buffer.from([0x63, 0x00]),
  reason: 'option-not-implemented',
});

describe('a call error carries the six fields of chs_error, verbatim', () => {
  it('every class reads reason, beside the other five', () => {
    for (const [status, cls] of [
      [Status.Rejected, SchemaError],
      [Status.Declined, UnsupportedError],
      [Status.InvalidArgument, UsageError],
      [Status.Internal, InternalError],
    ] as const) {
      const e = errorForStatus(status, fields(status));
      expect(e).toBeInstanceOf(cls);
      const c = e as unknown as CallErrorFields;
      expect([c.status, c.chCode, c.chName, c.reason]).toEqual([status, 53, 'TYPE_MISMATCH', 'option-not-implemented']);
      expect([...c.messageBytes]).toEqual([0x6d, 0xff]);
      expect([...c.column]).toEqual([0x63, 0x00]);
    }
  });

  it('a status outside the closed set is an InternalError that keeps the reason', () => {
    const e = errorForStatus(99, fields(99));
    expect(e).toBeInstanceOf(InternalError);
    expect((e as unknown as CallErrorFields).reason).toBe('option-not-implemented');
  });

  it('a misuse the binding detects itself carries an empty reason, as it carries an empty name and column', () => {
    for (const e of [usageError('misuse'), internalError('bug')]) {
      expect(e.reason).toBe('');
      expect(e.chName).toBe('');
      expect(e.column).toHaveLength(0);
    }
  });
});
