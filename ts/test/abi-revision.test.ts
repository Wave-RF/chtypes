/**
 * The ABI-revision handshake, RUN against a fixture rather than assumed
 * (#36).
 *
 * docs/reference/artifact.md step 5 is normative: an artifact whose
 * `chs_abi_revision()` answers a value that is neither 0 nor the revision
 * this binding was written against MUST be rejected, naming BOTH numbers.
 * The fixture is a stub shared library built from the header itself by the
 * fixture generator the signed release serves as `abi-revision/gen.py`: one
 * registry root whose artifact answers `abi_revision + 1`
 * (`wrong-revision/`), one answering the header's own `abi_revision`
 * (`at-revision/`, the control — without it a binding whose own
 * `ABI_REVISION` had drifted would refuse everything and pass the refusal
 * case for entirely the wrong reason).
 *
 * Every expected number comes from the fixture's own `fixture.json`, never
 * from the exported `ABI_REVISION` — a case that read the constant under
 * test could not catch that constant being wrong. The refusal message is
 * checked with the fixture root's own text (and its realpath — macOS's
 * `/tmp` resolves to `/private/tmp`) stripped out first, so a digit that
 * happens to sit in a temp-directory name can never stand in for a digit the
 * binding itself was supposed to print.
 */

import { readFileSync, realpathSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { Registry, RegistryError } from '../src/index.js';

interface FixtureDoc {
  abi_revision: number;
  mismatch_revision: number;
  clickhouse_version: string;
  clickhouse_minor: string;
  platform?: string;
}

/**
 * The fixture root comes ONLY from this environment variable. There is no
 * fallback build here: a missing fixture means the suite is skipped LOUDLY
 * (the line below), never a silent pass.
 */
const ROOT = process.env['CHTYPES_ABI_FIXTURES'];
const HAVE_FIXTURES = ROOT !== undefined && ROOT !== '';

if (!HAVE_FIXTURES) {
  console.warn('[chtypes] abi-revision tests SKIPPED: no ABI revision fixture: $CHTYPES_ABI_FIXTURES is unset');
}

/**
 * Read and validate `fixture.json` eagerly, once, whenever a root was given.
 * A fixture that failed to generate correctly (missing, unparseable, or
 * internally inconsistent) must fail the suite loudly, naming the path —
 * never skip quietly and never let a case run against nonsense numbers.
 */
function loadFixture(root: string): FixtureDoc {
  const fixturePath = path.join(root, 'fixture.json');
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(fixturePath, 'utf8'));
  } catch (err) {
    throw new Error(`chtypes: cannot read/parse ABI fixture at ${fixturePath}: ${String(err)}`);
  }
  const doc = parsed as Partial<FixtureDoc>;
  if (typeof doc.abi_revision !== 'number' || doc.abi_revision <= 0) {
    throw new Error(
      `chtypes: ABI fixture at ${fixturePath} has no positive abi_revision (got ${JSON.stringify(doc.abi_revision)})`,
    );
  }
  if (doc.mismatch_revision !== doc.abi_revision + 1) {
    throw new Error(
      `chtypes: ABI fixture at ${fixturePath} has mismatch_revision ${JSON.stringify(doc.mismatch_revision)}, ` +
        `expected abi_revision + 1 = ${doc.abi_revision + 1}`,
    );
  }
  return doc as FixtureDoc;
}

const DOC: FixtureDoc | null = ROOT !== undefined && ROOT !== '' ? loadFixture(ROOT) : null;

/**
 * "The message says N", not "the message contains the digits of N": a
 * refusal naming revision 4 must not be satisfied by the 4 in a path like
 * `.../24.8/`, and one naming 5 must not be satisfied by 15.
 */
function namesNumber(message: string, n: number): boolean {
  return new RegExp(`(^|[^0-9])${n}([^0-9]|$)`).test(message);
}

/**
 * Strip every occurrence of the fixture root — as given, and as
 * `fs.realpathSync` resolves it, since macOS's `/tmp` is a symlink to
 * `/private/tmp` and the two spellings can both appear depending on which
 * one a caller passed in — out of a message, longest first so the longer
 * (realpath) form is never left partially replaced by a shorter prefix
 * match. A digit sitting in a fixture directory name (a temp path, a run
 * id) must never be able to stand in for a digit the refusal itself is
 * required to print.
 */
function stripFixtureRoot(message: string, root: string): string {
  let real = root;
  try {
    real = realpathSync(root);
  } catch {
    // The root not resolving is not this helper's problem to raise; the
    // caller already has a live Registry error to report if anything here
    // was actually wrong.
  }
  const candidates = [...new Set([root, real])].sort((a, b) => b.length - a.length);
  let stripped = message;
  for (const candidate of candidates) {
    stripped = stripped.split(candidate).join('<fixture>');
  }
  return stripped;
}

describe.skipIf(!HAVE_FIXTURES)('abi revision fixture', () => {
  it('wrong revision is refused naming both numbers', () => {
    const doc = DOC!;
    const root = ROOT!;
    let thrown: unknown;
    try {
      new Registry(path.join(root, 'wrong-revision'));
    } catch (err) {
      thrown = err;
    }
    expect(thrown, 'a wrong-revision artifact must be refused at construction, not accepted').toBeInstanceOf(
      RegistryError,
    );
    const message = thrown instanceof Error ? thrown.message : String(thrown);
    const stripped = stripFixtureRoot(message, root);
    for (const n of [doc.mismatch_revision, doc.abi_revision]) {
      expect(
        namesNumber(stripped, n),
        `the refusal does not name ${n} (the artifact reports ${doc.mismatch_revision}, ` +
          `this binding speaks ${doc.abi_revision}): ${message}`,
      ).toBe(true);
    }
  });

  it('matching revision loads', () => {
    const doc = DOC!;
    const root = ROOT!;
    // Construction opens nothing, so the control artifact is opened by
    // `preload` — the constructor-time spelling of that request, and the one
    // whose failure is this test's verdict.
    const registry = new Registry(path.join(root, 'at-revision'), { preload: [doc.clickhouse_minor] });
    const libs = registry.libraries();
    expect(libs).toHaveLength(1);
    const lib = libs[0]!;
    expect(lib.abiRevision).toBe(doc.abi_revision);
    expect(lib.minor).toBe(doc.clickhouse_minor);
    expect(lib.version).toBe(doc.clickhouse_version);
    registry.close();
  });
});
