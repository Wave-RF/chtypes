/**
 * The fetch layer's environment (`docs/guides/fetch-v1.md` §2 and §4): the
 * variables that configure a fetch without code. `CHTYPES_ARTIFACTS_URL` and
 * `CHTYPES_CACHE` are read by the fetch layer itself; the rest are read here,
 * once, and an option the caller passed always wins over its variable.
 *
 *   CHTYPES_DOWNLOAD_TOKEN   `token`
 *   CHTYPES_TRUSTED_KEYS     `trustedKeys`: comma-separated raw ed25519 keys, 64 hex characters each; REPLACES the default list
 *   CHTYPES_ALLOW_UNSIGNED   `allowUnsigned`: `1`, `true`, `yes` or `on`
 *   CHTYPES_TARGET           `platform`: one of the three platform keys
 *   CHTYPES_REGISTRY         retired: one warning, otherwise ignored
 */

import { usageError } from './abi1/index.js';
import {
  ENV_ALLOW_UNSIGNED_NAME,
  ENV_BASES_NAME,
  ENV_CACHE_NAME,
  ENV_RETIRED,
  ENV_TARGET_NAME,
  ENV_TOKEN_NAME,
  ENV_TRUSTED_KEYS_NAME,
  PLATFORMS,
} from './ocifetch/constants.gen.js';
import { type FetchV1Options, isPlatformKey, keyIdOfRawKey, type TrustedKey } from './ocifetch/index.js';

const RAW_KEY = /^[0-9a-fA-F]{64}$/;
let warnedRetired = false;

function flag(name: string): boolean {
  const v = process.env[name];
  return v !== undefined && ['1', 'true', 'yes', 'on'].includes(v.trim().toLowerCase());
}

/** Fills each option the caller left unset from its environment variable. Pure over `process.env`, except the one retired-variable warning. */
export function withEnvironment<T extends FetchV1Options>(options: T): T {
  const out: { -readonly [K in keyof FetchV1Options]: FetchV1Options[K] } = { ...options };
  for (const name of ENV_RETIRED) {
    const v = process.env[name];
    if (v !== undefined && v !== '' && !warnedRetired) {
      warnedRetired = true;
      process.emitWarning(`${name} is retired and ignored; ${ENV_BASES_NAME} and ${ENV_CACHE_NAME} replace it`, { code: 'CHTYPES_ENV_RETIRED' });
    }
  }
  const token = process.env[ENV_TOKEN_NAME];
  if (out.token === undefined && token !== undefined && token !== '') out.token = token;
  if (out.allowUnsigned === undefined && flag(ENV_ALLOW_UNSIGNED_NAME)) out.allowUnsigned = true;
  const keys = process.env[ENV_TRUSTED_KEYS_NAME];
  if (out.trustedKeys === undefined && keys !== undefined && keys.trim() !== '') {
    const list: TrustedKey[] = [];
    for (const k of keys.split(',').map((x) => x.trim()).filter((x) => x !== '')) {
      if (!RAW_KEY.test(k)) {
        throw usageError(`${ENV_TRUSTED_KEYS_NAME}: ${JSON.stringify(k)} is not a raw ed25519 public key (64 hex characters)`);
      }
      list.push({ keyid: keyIdOfRawKey(k), ed25519Hex: k.toLowerCase() });
    }
    out.trustedKeys = list;
  }
  const target = process.env[ENV_TARGET_NAME];
  if (out.platform === undefined && target !== undefined && target !== '') {
    if (!isPlatformKey(target)) {
      throw usageError(`${ENV_TARGET_NAME}: ${JSON.stringify(target)} is not a platform; use one of ${PLATFORMS.map((p) => p.key).join(', ')}`);
    }
    out.platform = target;
  }
  return out as T;
}
