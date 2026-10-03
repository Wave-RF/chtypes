/**
 * The fetch layer's environment: the variables `docs/guides/fetch-v1.md` §2 and §4 name, read once
 * into options, with an explicit option always winning. The key id is derived here the way the
 * constants define it (sha256-first16hex over the raw key) and compared with the generated release
 * key's own id, so a change to the derivation fails a test that did not hand-set the answer.
 */

import { createHash } from 'node:crypto';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { UsageError } from '../src/abi1/errors.js';
import { withEnvironment } from '../src/env.js';
import { RELEASE_KEYS } from '../src/ocifetch/constants.gen.js';
import type { FetchV1Options } from '../src/ocifetch/index.js';

afterEach(() => {
  vi.unstubAllEnvs();
});

describe('withEnvironment', () => {
  it('leaves options alone when no variable is set', () => {
    for (const k of ['CHTYPES_DOWNLOAD_TOKEN', 'CHTYPES_TRUSTED_KEYS', 'CHTYPES_ALLOW_UNSIGNED', 'CHTYPES_TARGET', 'CHTYPES_REGISTRY']) {
      vi.stubEnv(k, '');
    }
    expect(withEnvironment({})).toEqual({});
  });

  it('reads the token, the allow-unsigned flag and the target platform', () => {
    vi.stubEnv('CHTYPES_DOWNLOAD_TOKEN', 'tok');
    vi.stubEnv('CHTYPES_ALLOW_UNSIGNED', '1');
    vi.stubEnv('CHTYPES_TARGET', 'linux-arm64');
    expect(withEnvironment({})).toMatchObject({ token: 'tok', allowUnsigned: true, platform: 'linux-arm64' });
  });

  it('an explicit option wins over its variable', () => {
    vi.stubEnv('CHTYPES_DOWNLOAD_TOKEN', 'from-env');
    vi.stubEnv('CHTYPES_ALLOW_UNSIGNED', '1');
    vi.stubEnv('CHTYPES_TARGET', 'linux-arm64');
    const out = withEnvironment({ token: 'explicit', allowUnsigned: false, platform: 'darwin-arm64' });
    expect(out).toMatchObject({ token: 'explicit', allowUnsigned: false, platform: 'darwin-arm64' });
  });

  it('CHTYPES_TRUSTED_KEYS replaces the list, and each key id is sha256-first16hex of the raw key', () => {
    const release = RELEASE_KEYS[0];
    vi.stubEnv('CHTYPES_TRUSTED_KEYS', ` ${release.ed25519Hex.toUpperCase()} , `);
    const out = withEnvironment<FetchV1Options>({});
    expect(out.trustedKeys).toEqual([{ keyid: release.keyid, ed25519Hex: release.ed25519Hex }]);
    const derived = createHash('sha256').update(Buffer.from(release.ed25519Hex, 'hex')).digest('hex').slice(0, 16);
    expect(derived).toBe(release.keyid);
  });

  it('refuses a malformed key and an unknown platform as a UsageError', () => {
    vi.stubEnv('CHTYPES_TRUSTED_KEYS', 'not-hex');
    expect(() => withEnvironment({})).toThrow(UsageError);
    vi.stubEnv('CHTYPES_TRUSTED_KEYS', '');
    vi.stubEnv('CHTYPES_TARGET', 'windows-amd64');
    expect(() => withEnvironment({})).toThrow(UsageError);
  });
});
