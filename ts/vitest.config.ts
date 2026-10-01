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
  },
});
