/**
 * The process setup (`docs/reference/bindings-v1.md` §6): the image zone and
 * the default settings, chosen once, before traffic.
 *
 *   1. `setup` records; it does not load. Called before any library is open it
 *      records the zone spelling and the defaults. Called again with the same
 *      zone, byte for byte, and the same defaults, it is a no-op. Called with
 *      a different zone or different defaults it is a `UsageError` naming
 *      both, and the first setup stands.
 *   2. The first open commits it. If `setup` was never called, the first open
 *      records the empty setup: the empty zone, which the library reads as
 *      UTC, and no defaults. From then on `setup` succeeds only with exactly
 *      the setup in effect.
 *   3. Every image is set up at loader step 7, once, from this record
 *      (`./abi1/loader.ts`); nothing sets either again for that image.
 *
 * This is state of the isolate: every worker thread calls it identically, and
 * a worker whose zone differs is refused by the library at its first open.
 */

import { usageError } from './abi1/index.js';
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
  if (recorded === undefined) {
    recorded = next;
    return;
  }
  if (!same(recorded, next)) {
    throw usageError(`setup was already fixed at ${describe(recorded)}; it was asked for ${describe(next)}. The first setup stands: call setup before the first open, once.`);
  }
}

/** The setup in effect, committing the empty setup if none was recorded: what the first open does. */
export function commitSetup(): ProcessSetup {
  recorded ??= { timezone: '', defaults: {} };
  return recorded;
}

/** Test only: forget the recorded setup. Not part of the public API. */
export function resetSetupForTests(): void {
  recorded = undefined;
}
