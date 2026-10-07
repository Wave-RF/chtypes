/**
 * Loader step 7 and the re-check of an already-open image
 * (`docs/reference/bindings-v1.md` §6), against the stub library
 * (`$CHTYPES_ABI2_STUBS`; every case SKIPS LOUDLY by name without it).
 *
 * A failure of `chs_initialize` is the CALL's own error, mapped by the status
 * table, never a loader refusal reason: a zone the library refuses outright is
 * a `SchemaError`, and a different spelling than the image already holds
 * (`CHS_INVALID_ARGUMENT`) is a `UsageError` naming the zone asked for. The
 * stub forces a status when an input starts with `!S:<status>:<code>:<name>:<message>`.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { ArtifactIncompatibleError, LoaderCorruptError, SchemaError, UsageError } from '../../src/abi2/errors.js';
import { type LoadInput, openAbi2, type Predicate } from '../../src/abi2/loader.js';

const STUBS_DIR = process.env.CHTYPES_ABI2_STUBS;
const stubsAvailable = typeof STUBS_DIR === 'string' && STUBS_DIR.length > 0;

function input(variant: string, extra: Partial<LoadInput> = {}, predicate?: Partial<Predicate>): LoadInput {
  const doc = JSON.parse(readFileSync(path.join(STUBS_DIR as string, 'stubs.json'), 'utf8')) as {
    variants: Record<string, { predicate: Predicate }>;
  };
  const v = doc.variants[variant];
  if (v === undefined) throw new Error(`no stub variant ${variant}`);
  const p = { ...v.predicate, ...predicate } as Predicate;
  return { libraryPath: path.join(STUBS_DIR as string, `${variant}.so`), predicate: p, platform: `${v.predicate.os}-${v.predicate.arch}`, ...extra };
}

describe.skipIf(!stubsAvailable)('loader step 7 and the re-check of an open image', () => {
  it('maps a zone the library refuses to a SchemaError, and a conflicting spelling to a UsageError naming the zone asked for', () => {
    expect(() => openAbi2(input('ok-b', { timezone: '!S:CHS_REJECTED:77:TEST_CODE:no such zone' }))).toThrow(SchemaError);
    let caught: unknown;
    try {
      openAbi2(input('ok-b', { timezone: '!S:CHS_INVALID_ARGUMENT:77:TEST_CODE:a different zone' }));
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(UsageError);
    expect((caught as UsageError).message).toContain('!S:CHS_INVALID_ARGUMENT');
    expect((caught as UsageError).message).toContain('a different zone');
    expect(caught).not.toBeInstanceOf(ArtifactIncompatibleError);
  });

  it('opens once the setup is acceptable, with defaults seeded after the zone', () => {
    const image = openAbi2(input('ok-b', { timezone: 'Europe/Berlin', defaults: { input_format_defaults_for_omitted_fields: '1' } }));
    expect(image.version).toBe('26.8.15.10');
  });

  it('refuses a new request whose signed statement disagrees with the open image, and leaves the image open for the requests it matches', () => {
    const open = openAbi2(input('ok-b'));
    expect(() => openAbi2(input('ok-b', {}, { build: '19990101.000000' }))).toThrow(LoaderCorruptError);
    let caught: unknown;
    try {
      openAbi2(input('ok-b', {}, { core_commit: 'f'.repeat(40) }));
    } catch (err) {
      caught = err;
    }
    expect((caught as LoaderCorruptError).reason).toBe('build_info_mismatch:core_commit');
    expect(openAbi2(input('ok-b'))).toBe(open);
  });

  it.skipIf(process.platform !== 'linux')('re-checks the glibc floor on an open image too', () => {
    expect(() => openAbi2(input('ok-b', {}, { glibc_floor: '99.0' }))).toThrow(ArtifactIncompatibleError);
  });
});
