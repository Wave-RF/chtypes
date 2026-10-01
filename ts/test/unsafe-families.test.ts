/**
 * The crash-guard refuse-list's source (docs/reference/artifact.md step 9):
 * `unsafe_families.txt` beside the library when that file is present, even
 * empty; otherwise the manifest's own `unsafe_families` field when IT is
 * present, even empty; neither present is refused rather than a silent empty
 * guard. `readUnsafeFamilies` is exercised directly — it takes a directory
 * and an already-parsed `Manifest`, so none of this needs a real artifact or
 * a `dlopen`.
 */

import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { RegistryError } from '../src/index.js';
import { type Manifest, readUnsafeFamilies } from '../src/registry.js';

const roots: string[] = [];

afterEach(() => {
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true });
});

function scratch(): string {
  const dir = mkdtempSync(path.join(tmpdir(), 'chtypes-reg-'));
  roots.push(dir);
  return dir;
}

const BASE_MANIFEST: Manifest = { library: 'libchtypes.so' };

describe('readUnsafeFamilies: which source wins', () => {
  it('falls back to the manifest field when unsafe_families.txt is absent', () => {
    const dir = scratch();
    const manifest: Manifest = { ...BASE_MANIFEST, unsafe_families: 'Array,Map' };
    expect(readUnsafeFamilies(dir, manifest)).toBe('Array,Map');
  });

  it('refuses when neither unsafe_families.txt nor the manifest field is present', () => {
    const dir = scratch();
    expect(() => readUnsafeFamilies(dir, BASE_MANIFEST)).toThrow(RegistryError);
    expect(() => readUnsafeFamilies(dir, BASE_MANIFEST)).toThrow(/unsafe_families\.txt/);
    expect(() => readUnsafeFamilies(dir, BASE_MANIFEST)).toThrow(/manifest\.json/);
  });

  it('prefers a present-but-empty unsafe_families.txt over a non-empty manifest field', () => {
    const dir = scratch();
    writeFileSync(path.join(dir, 'unsafe_families.txt'), '');
    const manifest: Manifest = { ...BASE_MANIFEST, unsafe_families: 'Array,Map' };
    expect(readUnsafeFamilies(dir, manifest)).toBe('');
  });
});
