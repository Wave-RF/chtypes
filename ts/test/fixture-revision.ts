/**
 * The ABI revision the shared fetch fixtures (`tests/fixtures/fetch/`) carry,
 * read off their own `index.json` rows.
 *
 * Fetch selects only rows at the binding's own ABI revision
 * (docs/guides/fetch.md §2), and the fixtures are generated at whatever revision
 * the artifact producer was at — so the fixture suite fetches at THIS revision,
 * through the test-only override, instead of assuming it equals `ABI_REVISION`.
 * It is DERIVED, never typed into a test: a suite that hand-set it would be
 * testing its author's belief about the fixtures, not the fetch. Anything short
 * of one unambiguous answer — no release, a row without an integer
 * `abi_revision`, rows that disagree — throws.
 *
 * Not a `*.test.ts` file: a helper the suites import, not a suite of its own.
 */

import { existsSync, readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';

export function fixtureAbiRevision(fixtures: string): number {
  const releases = existsSync(fixtures)
    ? readdirSync(fixtures)
        .filter((name) => existsSync(path.join(fixtures, name, 'index.json')))
        .sort()
    : [];
  if (releases.length === 0) throw new Error(`no <release>/index.json under ${fixtures}`);
  const seen = new Map<number, string[]>();
  for (const release of releases) {
    const file = path.join(fixtures, release, 'index.json');
    const doc = JSON.parse(readFileSync(file, 'utf8')) as { artifacts?: Record<string, unknown>[] };
    const rows = doc.artifacts ?? [];
    if (rows.length === 0) throw new Error(`${file} lists no artifacts`);
    rows.forEach((row, i) => {
      const rev = row['abi_revision'];
      if (typeof rev !== 'number' || !Number.isSafeInteger(rev)) {
        throw new Error(`${file}: row ${i} carries no integer abi_revision (${JSON.stringify(rev)})`);
      }
      const names = seen.get(rev) ?? [];
      if (!names.includes(release)) names.push(release);
      seen.set(rev, names);
    });
  }
  if (seen.size !== 1) {
    const parts = [...seen.entries()]
      .sort(([a], [b]) => a - b)
      .map(([rev, names]) => `${rev} in ${names.join(', ')}`)
      .join('; ');
    throw new Error(`the fixture releases under ${fixtures} disagree on abi_revision (${parts}); there is no one revision to fetch them at`);
  }
  return [...seen.keys()][0]!;
}
