/**
 * Rule r6's fingerprint refusal (`spec/abi-v2/docs.md`). A library whose
 * fingerprint is not this SDK's is refused as `CHTYPES_ARTIFACT_INCOMPATIBLE`,
 * naming both. While the description is unstable (a 2.0.0-dev SDK) the
 * message is exactly the rule's; once it is locked, which this SDK's is, the
 * refusal keeps the loader's own message, never the dev one. The dev side is
 * selected explicitly (`incompatibleMessageAt`); the locked side is this SDK
 * as built. The messages are spelled out here, from the rule's text and the
 * loader's shape, never taken from the code under test; Go's
 * `go/chtypes/devfingerprint_test.go` and `go/internal/abi2/devfingerprint_test.go`
 * are the same tests.
 *
 * The mapping runs with no library; the load runs over the stub built to
 * report another fingerprint (`scripts/abi-v1/emit/_stubshared.py`,
 * `fingerprint-other`), and SKIPS LOUDLY by name without `$CHTYPES_ABI2_STUBS`.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { ABI_FINGERPRINT, ABI_STABILITY, ABI_VERSION } from '../../src/abi2/decls.gen.js';
import { ArtifactIncompatibleError, incompatibleMessageAt } from '../../src/abi2/errors.js';
import { openAbi2, type Predicate } from '../../src/abi2/loader.js';
import { ArtifactError, openUnverified } from '../../src/index.js';

const STUBS_DIR = process.env.CHTYPES_ABI2_STUBS;
const stubsAvailable = typeof STUBS_DIR === 'string' && STUBS_DIR.length > 0;
const OTHER = `sha256:${'0'.repeat(64)}`;

/** Rule r6's text, with X and Y the two full fingerprints. */
function r6Message(sdk: string, library: string): string {
  return `this SDK speaks dev fingerprint ${sdk}; the library has ${library} — update your dev SDK`;
}

/** The loader's own fingerprint refusal, after its path: the locked SDK's. */
function loaderTail(sdk: string, library: string): string {
  return `: incompatible artifact: fingerprint (want ${sdk}, got ${library})`;
}

describe('the compiled-in identity', () => {
  it('is ABI v2 and a full sha256 fingerprint, printed for the jobs that assert it', () => {
    expect(ABI_VERSION).toBe(2);
    expect(ABI_FINGERPRINT).toMatch(/^sha256:[0-9a-f]{64}$/);
    console.log(`chtypes_abi_identity binding=ts abi=${ABI_VERSION} fingerprint=${ABI_FINGERPRINT}`);
  });
});

describe('rule r6: a library with another fingerprint is refused', () => {
  it("each side selected explicitly: rule r6's exact message while unstable, the loader's own once locked", () => {
    const f = { reason: 'fingerprint', path: '/p', want: ABI_FINGERPRINT, got: OTHER };
    expect(incompatibleMessageAt('unstable', f)).toBe(r6Message(ABI_FINGERPRINT, OTHER));
    expect(incompatibleMessageAt('locked', f)).toBe(`chtypes: /p${loaderTail(ABI_FINGERPRINT, OTHER)}`);
    // Every other refusal keeps the loader's own message at both.
    const abi = { reason: 'abi_version', path: '/p', want: '2', got: '1' };
    for (const stability of ['unstable', 'locked']) {
      expect(incompatibleMessageAt(stability, abi)).toBe('chtypes: /p: incompatible artifact: abi_version (want 2, got 1)');
    }
  });

  it("this SDK as built is locked: the refusal alone is CHTYPES_ARTIFACT_INCOMPATIBLE with the loader's own message", () => {
    expect(ABI_STABILITY).toBe('locked');
    const err = new ArtifactIncompatibleError({ reason: 'fingerprint', path: '/p', want: ABI_FINGERPRINT, got: OTHER });
    expect(err.message).toBe(`chtypes: /p${loaderTail(ABI_FINGERPRINT, OTHER)}`);
    expect(err.code).toBe('CHTYPES_ARTIFACT_INCOMPATIBLE');
    expect(err.reason).toBe('fingerprint');
    expect(err.want).toBe(ABI_FINGERPRINT);
    expect(err.got).toBe(OTHER);
    expect(err).toBeInstanceOf(ArtifactError);
  });

  it.skipIf(!stubsAvailable)('through a real load of the stub that reports another fingerprint, verified and unverified', () => {
    expect(ABI_STABILITY).toBe('locked');
    const stubs = JSON.parse(readFileSync(path.join(STUBS_DIR as string, 'stubs.json'), 'utf8')) as {
      variants: Record<string, { readonly predicate: Predicate }>;
    };
    const v = stubs.variants['fingerprint-other'];
    if (v === undefined) throw new Error('stubs.json has no fingerprint-other variant');
    const libraryPath = path.join(STUBS_DIR as string, 'fingerprint-other.so');

    let caught: unknown;
    try {
      openAbi2({ libraryPath, predicate: v.predicate, platform: `${v.predicate.os}-${v.predicate.arch}` });
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(ArtifactIncompatibleError);
    const refused = caught as ArtifactIncompatibleError;
    expect(refused.reason).toBe('fingerprint');
    expect(refused.code).toBe('CHTYPES_ARTIFACT_INCOMPATIBLE');
    expect(refused.want).toBe(ABI_FINGERPRINT);
    expect(refused.got).toBe(OTHER);
    expect(refused.message).toBe(`chtypes: ${libraryPath}${loaderTail(ABI_FINGERPRINT, OTHER)}`);
    expect(refused.message).not.toContain('dev fingerprint');

    const saved = process.env['CHTYPES_ALLOW_UNVERIFIED_LIBRARY'];
    process.env['CHTYPES_ALLOW_UNVERIFIED_LIBRARY'] = '1';
    try {
      expect(() => openUnverified(libraryPath, { allow: true })).toThrow(loaderTail(ABI_FINGERPRINT, OTHER));
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
