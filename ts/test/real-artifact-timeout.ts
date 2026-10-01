/**
 * The explicit test/hook timeout for a TS test that opens a real artifact —
 * constructs a `Registry` with `preload`, or otherwise reaches a `dlopen`'d
 * library — rather than the default (issue #369).
 *
 * Measured: `degradation-and-introspection.test.ts`'s introspection-trio test
 * built a `Registry` with `preload` inside its own body and took 5,600 ms on
 * one hosted-runner run against vitest's 5,000 ms default `testTimeout`, where
 * the same test took 114-607 ms on green runs. A cold first `dlopen` of a
 * tens-of-megabytes artifact on a busy runner can pass the default even though
 * nothing is actually hung.
 *
 * Defined once so every real-artifact test and `beforeAll` shares the same
 * generous deadline instead of each guessing its own number. A test that
 * never loads a library keeps vitest's default `testTimeout` / `hookTimeout`,
 * so a genuine hang there is still caught quickly.
 *
 * Not a `*.test.ts` file: a constant the suites import, not a suite of its
 * own.
 */

export const REAL_ARTIFACT_TIMEOUT_MS = 30_000;
