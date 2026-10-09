/**
 * The production generation-2 channel (`src/ocifetch/channel.ts`,
 * `PROD_V2_CHANNEL`; `docs/guides/fetch-v1.md`, "Generation 2 after the lock"),
 * built and tested but not the default. The fetch conformance cases run under
 * it in `conformance.test.ts`; these are the pins those cases do not reach.
 * Nothing here reaches the network.
 */

import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { activeChannel, channelName, DEV_KEY_HEX, useDevChannelForTests, useProdV2ForTests } from '../../src/ocifetch/channel.js';
import { RELEASE_KEYS } from '../../src/ocifetch/constants.gen.js';
import { effectiveFetchOptions } from '../../src/ocifetch/ensure.js';
import { cacheRoot, systemDirs } from '../../src/ocifetch/layout.js';

const OVERRIDE_ENV = ['CHTYPES_ARTIFACTS_URL', 'CHTYPES_TRUSTED_KEYS', 'CHTYPES_ALLOW_UNSIGNED', 'CHTYPES_CACHE', 'CHTYPES_CACHE_STRICT', 'CHTYPES_OFFLINE'];

const dirs: string[] = [];
afterEach(async () => {
  vi.unstubAllEnvs();
  for (const d of dirs.splice(0)) await rm(d, { recursive: true, force: true });
});

describe('the production generation-2 channel', () => {
  it('carries the v2 values: abi 2, schema-2 records, the v2 subroot, the release key and the production base', async () => {
    const restore = useProdV2ForTests();
    try {
      for (const k of OVERRIDE_ENV) vi.stubEnv(k, '');
      const c = activeChannel();
      expect([c.name, c.abi, c.recordSchema, c.rootLeaf, c.subroot]).toEqual(['v2', 2, 2, 'v2', 'v2']);
      expect(c.overridable && c.pinnable).toBe(true);
      expect(c.ownFingerprint).toBe('');
      const eff = effectiveFetchOptions({});
      expect(eff.bases).toEqual(['https://registry.wavehouse.dev/chtypes/v2']);
      expect(eff.trustedKeys.map((k) => k.ed25519Hex)).toEqual(RELEASE_KEYS.map((k) => k.ed25519Hex));
      expect(eff.trustedKeys[0]?.keyid).toBe('deb275922dbff76e');
      expect(eff.trustedKeys.map((k) => k.ed25519Hex)).not.toContain(DEV_KEY_HEX);
      expect(systemDirs()).toEqual(['/usr/local/share/chtypes/v2', '/opt/chtypes/v2']);
      const xdg = await mkdtemp(path.join(os.tmpdir(), 'ts-prodv2-'));
      dirs.push(xdg);
      vi.stubEnv('XDG_CACHE_HOME', xdg);
      expect(cacheRoot()).toBe(path.join(xdg, 'chtypes', 'v2'));
      expect(cacheRoot(xdg)).toBe(path.join(xdg, 'v2'));
    } finally {
      restore();
    }
  });

  it('honors a base override from the environment', () => {
    const restore = useProdV2ForTests();
    try {
      vi.stubEnv('CHTYPES_ARTIFACTS_URL', 'https://mirror.example/chtypes/v2');
      expect(effectiveFetchOptions({}).bases).toEqual(['https://mirror.example/chtypes/v2']);
    } finally {
      restore();
    }
  });

  it('is not the default: a process with no seam selected speaks the dev channel', () => {
    const restoreDev = useDevChannelForTests();
    try {
      expect(channelName()).toBe('v2-dev');
      const restore = useProdV2ForTests();
      expect(channelName()).toBe('v2');
      restore();
      expect(channelName()).toBe('v2-dev');
    } finally {
      restoreDev();
    }
  });
});
