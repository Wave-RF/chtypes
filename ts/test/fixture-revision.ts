/**
 * The ABI revision the shared fetch fixtures (`tests/fixtures/fetch/`) DECLARE,
 * as `expected.json`'s `fixtures_abi_revision`: the revision every fixture's
 * rows carry except `two-revisions/`'s, which deliberately spans two.
 *
 * Fetch selects only rows at the binding's own ABI revision
 * (docs/guides/fetch.md §2), and the fixtures are generated at whatever revision
 * the artifact producer was at — so the fixture suite fetches at THIS revision,
 * through the test-only override, instead of assuming it equals `ABI_REVISION`.
 * It is read from the fixtures' own declaration, never typed into a test: a
 * suite that hand-set it would be testing its author's belief about the
 * fixtures, not the fetch. A missing or non-integer field throws.
 *
 * Not a `*.test.ts` file: a helper the suites import, not a suite of its own.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';

export function fixtureAbiRevision(fixtures: string): number {
  const file = path.join(fixtures, 'expected.json');
  const doc = JSON.parse(readFileSync(file, 'utf8')) as Record<string, unknown>;
  if (!('fixtures_abi_revision' in doc)) {
    throw new Error(`${file} declares no fixtures_abi_revision — regenerate the fixtures from the artifact producer's current set`);
  }
  const rev = doc['fixtures_abi_revision'];
  if (typeof rev !== 'number' || !Number.isSafeInteger(rev)) {
    throw new Error(`${file}: fixtures_abi_revision is ${JSON.stringify(rev)}, not an integer`);
  }
  return rev;
}
