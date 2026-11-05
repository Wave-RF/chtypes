import { defineConfig } from 'vitest/config';

// The FinalizationRegistry backstop test (test/finalization-registry.test.ts,
// issue #302) needs `global.gc()` to force a collection deterministically
// rather than hoping one happens inside the test's timeout — Node only
// exposes that function under `--expose-gc`, and vitest runs each test file
// in its own worker (fork or thread), which does NOT inherit the CLI
// process's own V8 flags unless told to via `execArgv`.
export default defineConfig({
  test: {
    execArgv: ['--expose-gc'],
    // A test.each title keeps each interpolated value whole. vitest's default
    // cuts a value at 40 characters, so 35 conformance cases whose ids share
    // their first 40 had duplicate names, and a dropped case could not be
    // named. scripts/abi-v1/stub_census.py pins every test by its name
    // (public issue #727). Any case id is far shorter than this.
    taskTitleValueFormatTruncate: 1000,
  },
});
