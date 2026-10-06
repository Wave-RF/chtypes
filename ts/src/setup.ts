/**
 * The process setup (`docs/reference/bindings-v1.md` §6): the image zone and
 * the default settings, chosen once, before traffic.
 *
 *   1. `setup` records; it does not load. Called before any library is open it
 *      records the zone spelling and the defaults. Called again with the same
 *      zone, byte for byte, and the same defaults, it is a no-op. Called with
 *      a different zone or different defaults it is a `UsageError` naming
 *      both, and the first setup stands.
 *   2. It latches on the first successful step 7. If `setup` was never called,
 *      the first open records the empty setup: the empty zone, which the
 *      library reads as UTC, and no defaults. Once an image completes loader
 *      step 7 (`chs_initialize`, then `chs_set_defaults` when there are
 *      defaults), `setup` succeeds only with exactly the setup in effect
 *      (`latchSetup`). Until then, an open that attempted a load and failed,
 *      whatever failed (the fetch, the signature, an incompatible artifact, a
 *      missing symbol or step 7), keeps the record but makes it replaceable
 *      (`settleFailedOpen`): a retry with no new `setup` runs under the
 *      recorded setup, and a different `setup` replaces it. A refused version
 *      spelling or an unverified open without the caller's opt-in fails before
 *      any load is attempted, and unlocks nothing.
 *   3. Every image is set up at loader step 7, once, from this record
 *      (`./abi2/loader.ts`); nothing sets either again for that image.
 *
 * This is state of the isolate: every worker thread calls it identically, and
 * a worker whose zone differs is refused by the library at its first open.
 */

import { usageError } from './abi2/index.js';
import { type Settings, validateDefaults } from './settings.js';

/** What `setup` takes: the image zone (an IANA name) and the default settings every call starts from. */
export interface SetupOptions {
  readonly timezone?: string;
  readonly defaults?: Settings;
}

/** The setup in effect, as step 7 applies it. */
export interface ProcessSetup {
  readonly timezone: string;
  readonly defaults: Readonly<Record<string, string>>;
}

let recorded: ProcessSetup | undefined;
/** Whether any image has completed loader step 7 under the record. */
let latched = false;
/** Whether an open that attempted a load failed since the record was last set or committed, with nothing latched: a different `setup` then replaces the record instead of being refused. */
let replaceable = false;
/** Counts the records `setup` made or replaced. An open reads it when its attempt begins, and unlocks the record on failure only if it is unchanged: a failed open never unlocks a setup recorded after it began. */
let generation = 0;

function describe(s: ProcessSetup): string {
  const keys = Object.keys(s.defaults).sort();
  const defaults = keys.length === 0 ? 'no defaults' : `defaults ${JSON.stringify(Object.fromEntries(keys.map((k) => [k, s.defaults[k]])))}`;
  return `zone ${s.timezone === '' ? '(empty, UTC)' : JSON.stringify(s.timezone)} and ${defaults}`;
}

function same(a: ProcessSetup, b: ProcessSetup): boolean {
  if (a.timezone !== b.timezone) return false;
  const ak = Object.keys(a.defaults);
  const bk = Object.keys(b.defaults);
  return ak.length === bk.length && ak.every((k) => Object.hasOwn(b.defaults, k) && a.defaults[k] === b.defaults[k]);
}

/** Record the process setup, once. Call it before the first open; see this module's header for the rule. */
export function setup(options: SetupOptions = {}): void {
  const timezone = options.timezone ?? '';
  if (typeof timezone !== 'string') throw new TypeError(`chtypes: setup timezone must be a string, got ${typeof timezone}`);
  const defaults = options.defaults ?? {};
  validateDefaults(defaults);
  const next: ProcessSetup = { timezone, defaults: { ...defaults } };
  if (recorded !== undefined && same(recorded, next)) return;
  if (recorded !== undefined && (latched || !replaceable)) {
    throw usageError(`setup was already fixed at ${describe(recorded)}; it was asked for ${describe(next)}. The first setup stands: call setup before the first open, once.`);
  }
  // A first record, or a replacement after a failed open: either way the new record is locked until the next failed open.
  recorded = next;
  replaceable = false;
  generation += 1;
}

/** The setup in effect, recording the empty setup if none was recorded: what an open does before step 7. The load that follows claims the record, so it is locked again. */
export function commitSetup(): ProcessSetup {
  recorded ??= { timezone: '', defaults: {} };
  replaceable = false;
  return recorded;
}

/** Read when an open's attempt begins, for `settleFailedOpen`. */
export function setupGeneration(): number {
  return generation;
}

/** An image completed loader step 7: from then on the setup in effect stands. An open calls it synchronously after the load that committed the record, so it latches the record that load ran under. */
export function latchSetup(): void {
  latched = true;
  replaceable = false;
}

/**
 * Settle an open that attempted a load and failed, whatever failed. While no image has completed step 7 it unlocks the record: the record stays, so a retry runs under it, and a different `setup` may replace it. It leaves alone a setup recorded or replaced after the open began (`began` is `setupGeneration()` then), and once the setup has latched it changes nothing: the library's own process-once rule answers a different zone on an image that already has one.
 */
export function settleFailedOpen(began: number): void {
  if (latched || recorded === undefined || generation !== began) return;
  replaceable = true;
}

/** Test only: forget the recorded setup, the latch and the unlock. Not part of the public API. */
export function resetSetupForTests(): void {
  recorded = undefined;
  latched = false;
  replaceable = false;
}
