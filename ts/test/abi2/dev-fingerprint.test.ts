/**
 * Rule r6's fingerprint refusal (`spec/abi-v2/docs.md`): a 2.0.0-dev SDK pins
 * its dev fingerprint and refuses a library with any other as
 * `CHTYPES_ARTIFACT_INCOMPATIBLE`, with exactly the rule's message. The
 * message is spelled out here, from the rule's text, never taken from the code
 * under test; Go's `go/chtypes/devfingerprint_test.go` is the same test.
 *
 * The mapping runs with no library; the load runs over the stub built to
 * report another fingerprint (`scripts/abi-v1/emit/_stubshared.py`,
 * `fingerprint-other`), and SKIPS LOUDLY by name without `$CHTYPES_ABI2_STUBS`.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { ABI_FINGERPRINT, ABI_STABILITY, ABI_VERSION } from '../../src/abi2/decls.gen.js';
import { ArtifactIncompatibleError } from '../../src/abi2/errors.js';
import { openAbi2, type Predicate } from '../../src/abi2/loader.js';
import { ArtifactError, openUnverified } from '../../src/index.js';

const STUBS_DIR = process.env.CHTYPES_ABI2_STUBS;
const stubsAvailable = typeof STUBS_DIR === 'string' && STUBS_DIR.length > 0;
const unstable = ABI_STABILITY === 'unstable';
const OTHER = `sha256:${'0'.repeat(64)}`;

/** Rule r6's text, with X and Y the two full fingerprints. */
function r6Message(sdk: string, library: string): string {
  return `this SDK speaks dev fingerprint ${sdk}; the library has ${library} — update your dev SDK`;
}

describe('the compiled-in identity', () => {
  it('is ABI v2 and a full sha256 fingerprint, printed for the jobs that assert it', () => {
    expect(ABI_VERSION).toBe(2);
    expect(ABI_FINGERPRINT).toMatch(/^sha256:[0-9a-f]{64}$/);
    console.log(`chtypes_abi_identity binding=ts abi=${ABI_VERSION} fingerprint=${ABI_FINGERPRINT}`);
  });
});

if (!unstable) {
  console.warn(`dev fingerprint refusal: SKIPPED. The description is ${JSON.stringify(ABI_STABILITY)}; the dev message applies only while it is unstable.`);
}

describe.skipIf(!unstable)('rule r6: a library with another fingerprint is refused with the dev message', () => {
  it('the refusal alone, with no library: CHTYPES_ARTIFACT_INCOMPATIBLE carrying exactly the rule\'s message', () => {
    const err = new ArtifactIncompatibleError({ reason: 'fingerprint', path: '/p', want: ABI_FINGERPRINT, got: OTHER });
    expect(err.message).toBe(r6Message(ABI_FINGERPRINT, OTHER));
    expect(err.code).toBe('CHTYPES_ARTIFACT_INCOMPATIBLE');
    expect(err.reason).toBe('fingerprint');
    expect(err).toBeInstanceOf(ArtifactError);
    // Every other refusal keeps the loader's own message.
    const other = new ArtifactIncompatibleError({ reason: 'abi_version', path: '/p', want: '2', got: '1' });
    expect(other.message).not.toContain('dev fingerprint');
  });

  it.skipIf(!stubsAvailable)('through a real load of the stub that reports another fingerprint, verified and unverified', () => {
    const stubs = JSON.parse(readFileSync(path.join(STUBS_DIR as string, 'stubs.json'), 'utf8')) as {
      variants: Record<string, { readonly predicate: Predicate }>;
    };
    const v = stubs.variants['fingerprint-other'];
    if (v === undefined) throw new Error('stubs.json has no fingerprint-other variant');
    const libraryPath = path.join(STUBS_DIR as string, 'fingerprint-other.so');
    const want = r6Message(ABI_FINGERPRINT, OTHER);

    let caught: unknown;
    try {
      openAbi2({ libraryPath, predicate: v.predicate, platform: `${v.predicate.os}-${v.predicate.arch}` });
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(ArtifactIncompatibleError);
    expect((caught as ArtifactIncompatibleError).reason).toBe('fingerprint');
    expect((caught as ArtifactIncompatibleError).code).toBe('CHTYPES_ARTIFACT_INCOMPATIBLE');
    expect((caught as Error).message).toBe(want);

    const saved = process.env['CHTYPES_ALLOW_UNVERIFIED_LIBRARY'];
    process.env['CHTYPES_ALLOW_UNVERIFIED_LIBRARY'] = '1';
    try {
      expect(() => openUnverified(libraryPath, { allow: true })).toThrow(want);
    } finally {
      if (saved === undefined) delete process.env['CHTYPES_ALLOW_UNVERIFIED_LIBRARY'];
      else process.env['CHTYPES_ALLOW_UNVERIFIED_LIBRARY'] = saved;
    }
  });
});

if (!stubsAvailable) {
  describe('rule r6 through a real load', () => {
    it.skip('CHTYPES_ABI2_STUBS is not set — skipping loudly (build-stubs.sh was not run for this leg)', () => {});
  });
}
