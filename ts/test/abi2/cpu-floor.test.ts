/**
 * The CPU floor, loader step 2b (`spec/abi-v2/docs.md`, "Loading"): a library
 * below its floor opens, nothing of it has run, and the loader refuses it as an
 * ordinary error, carrying the library's three answers verbatim. They are
 * spelled out here, not taken from the code under test.
 *
 * The stubs plant it on every leg with no emulation
 * (`scripts/abi-v1/emit/_stubshared.py`, `CPU_FLOOR`). In `cpu-floor-refused` the
 * setup call answers `CHS_INTERNAL`, so a loader that reached it reads as an
 * `InternalError`, never as this refusal. SKIPS LOUDLY by name without
 * `$CHTYPES_ABI2_STUBS`.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { ABI_FINGERPRINT } from '../../src/abi2/decls.gen.js';
import { loaderRefusalCodeFor } from '../../src/abi2/errmap.gen.js';
import { ArtifactIncompatibleError, InternalError } from '../../src/abi2/errors.js';
import { openAbi2, type Predicate } from '../../src/abi2/loader.js';
import { ArtifactError, CODE_ARTIFACT_INCOMPATIBLE, CODE_CPU_UNSUPPORTED, openUnverified } from '../../src/index.js';
import { ERROR_EXIT_CODES } from '../../src/ocifetch/constants.gen.js';

const STUBS_DIR = process.env.CHTYPES_ABI2_STUBS;
const stubsAvailable = typeof STUBS_DIR === 'string' && STUBS_DIR.length > 0;

const WANT = 'x86-64-v3+pclmul';
const GOT = 'avx2,bmi2';
const SENTENCE = 'this CPU cannot run this library: missing avx2,bmi2 (the 26.8 linux-amd64 build requires x86-64-v3+pclmul). Use a CPU at or above that floor.';

function input(variant: string): { libraryPath: string; predicate: Predicate; platform: string } {
  const doc = JSON.parse(readFileSync(path.join(STUBS_DIR as string, 'stubs.json'), 'utf8')) as {
    variants: Record<string, { predicate: Predicate }>;
  };
  const v = doc.variants[variant];
  if (v === undefined) throw new Error(`no stub variant ${variant}`);
  return { libraryPath: path.join(STUBS_DIR as string, `${variant}.so`), predicate: v.predicate, platform: `${v.predicate.os}-${v.predicate.arch}` };
}

function refusalOf(open: () => unknown): ArtifactIncompatibleError {
  let caught: unknown;
  try {
    open();
  } catch (err) {
    caught = err;
  }
  expect(caught).not.toBeInstanceOf(InternalError);
  expect(caught).toBeInstanceOf(ArtifactIncompatibleError);
  return caught as ArtifactIncompatibleError;
}

function withUnverified<T>(run: () => T): T {
  const saved = process.env['CHTYPES_ALLOW_UNVERIFIED_LIBRARY'];
  process.env['CHTYPES_ALLOW_UNVERIFIED_LIBRARY'] = '1';
  try {
    return run();
  } finally {
    if (saved === undefined) delete process.env['CHTYPES_ALLOW_UNVERIFIED_LIBRARY'];
    else process.env['CHTYPES_ALLOW_UNVERIFIED_LIBRARY'] = saved;
  }
}

describe('the code, with no library', () => {
  it('is the generated table, and the class stays the one a handler of a failed open catches', () => {
    expect(CODE_CPU_UNSUPPORTED).toBe('CHTYPES_CPU_UNSUPPORTED');
    expect(ERROR_EXIT_CODES[CODE_CPU_UNSUPPORTED]).toBe(12);
    expect(loaderRefusalCodeFor('cpu_floor')).toBe(CODE_CPU_UNSUPPORTED);
    expect(loaderRefusalCodeFor('dlopen')).toBeUndefined();
    const err = new ArtifactIncompatibleError({ reason: 'cpu_floor', path: '/p', want: WANT, got: GOT, message: SENTENCE }, CODE_CPU_UNSUPPORTED);
    expect(err).toBeInstanceOf(ArtifactError);
    expect(err.code).toBe(CODE_CPU_UNSUPPORTED);
    expect(err.message).toBe(`chtypes: /p: ${SENTENCE}`);
    expect(new ArtifactIncompatibleError({ reason: 'dlopen', path: '/p' }).code).toBe(CODE_ARTIFACT_INCOMPATIBLE);
    // A library with the list and no sentence still refuses, naming the list.
    expect(new ArtifactIncompatibleError({ reason: 'cpu_floor', path: '/p', want: WANT, got: GOT, message: '' }, CODE_CPU_UNSUPPORTED).message).toContain(GOT);
  });
});

describe.skipIf(!stubsAvailable)('step 2b over the stubs', () => {
  it('refuses a library below its floor, verified and unverified, with the three answers verbatim', () => {
    const v = input('cpu-floor-refused');
    for (
      const err of [
        refusalOf(() => openAbi2(v)),
        withUnverified(() => refusalOf(() => openUnverified(v.libraryPath, { allow: true }))),
      ]
    ) {
      expect(err).toBeInstanceOf(ArtifactError);
      expect(err.code).toBe('CHTYPES_CPU_UNSUPPORTED');
      expect(err.reason).toBe('cpu_floor');
      expect(err.want).toBe(WANT);
      expect(err.got).toBe(GOT);
      expect(err.path).toBe(v.libraryPath);
      expect(err.message).toBe(`chtypes: ${v.libraryPath}: ${SENTENCE}`);
    }
  });

  it('reads a library from before the check as the fingerprint refusal: not cpu_floor, and not a missing symbol', () => {
    const err = refusalOf(() => openAbi2(input('cpu-floor-absent')));
    expect(err.code).toBe(CODE_ARTIFACT_INCOMPATIBLE);
    expect(err.reason).toBe('fingerprint');
    expect(err.want).toBe(ABI_FINGERPRINT);
    expect(err.got).toBe(`sha256:${'0'.repeat(64)}`);
  });

  it('opens the ok stub, which answers ""', () => {
    expect(openAbi2(input('ok')).buildInfo.abiFingerprint).toBe(ABI_FINGERPRINT);
  });
});

if (!stubsAvailable) {
  describe('step 2b over the stubs', () => {
    it.skip('CHTYPES_ABI2_STUBS is not set — skipping loudly (build-stubs.sh was not run for this leg)', () => {});
  });
}
