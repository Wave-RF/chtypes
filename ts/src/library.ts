/**
 * `Library`: one loaded image (`docs/reference/bindings-v1.md` §2, "The
 * library"). Every method is one ABI call over the generated, typed layer,
 * decoded without computing anything. There is no `close`: an image is never
 * unloaded, so nothing is owed. TypeScript runs one isolate, so every method
 * is trivially safe to call in any order.
 */

import { type BuildInfo, type Calls, checkUnverifiedAllowed, type LoadedImage, openUnverified as loadUnverified } from './abi1/index.js';
import {
  type Discovery,
  decodeDiscovery,
  decodeErrorCodes,
  decodeLiveHandles,
  type ErrorCodeTable,
} from './documents.js';
import type { Resolved } from './ocifetch/index.js';
import { Schema, type CompileOptions } from './schema.js';
import { commitSetup, latchSetup, settleFailedOpen, setupGeneration } from './setup.js';
import { type BytesIn, bytesIn, encodeSettings } from './settings.js';

export class Library {
  readonly #image: LoadedImage;
  readonly #calls: Calls;
  #errorCodes: ErrorCodeTable | undefined;

  /** The fetch record this library was opened by; undefined when opened unverified. */
  readonly resolved: Resolved | undefined;

  /** Not for callers: use `Registry.for` or `openUnverified`. */
  constructor(image: LoadedImage, resolved: Resolved | undefined) {
    this.#image = image;
    this.#calls = image.calls;
    this.resolved = resolved;
  }

  /** What the library is, read once by the loader. */
  get buildInfo(): BuildInfo {
    return this.#image.buildInfo;
  }

  /** `clickhouse_version` from the build info, read rather than derived: four parts, no channel. */
  get version(): string {
    return this.#image.buildInfo.clickhouseVersion;
  }

  /** `clickhouse_minor` from the build info, read rather than derived. */
  get minor(): string {
    return this.#image.buildInfo.clickhouseMinor;
  }

  /** The loaded file's path. */
  get path(): string {
    return this.#image.path;
  }

  /** ClickHouse's own canonical spelling of a type expression, or its own refusal. */
  validateType(typeExpr: BytesIn): Buffer {
    return this.#calls.typeValidate(bytesIn(typeExpr));
  }

  /** A name quoted the way ClickHouse's own `backQuote` quotes it: always quoted. */
  quoteIdentifier(name: BytesIn): Buffer {
    return this.#calls.backQuote(bytesIn(name));
  }

  /** A name quoted only where this build says it must be. */
  quoteIdentifierIfNeeded(name: BytesIn): Buffer {
    return this.#calls.backQuoteIfNeeded(bytesIn(name));
  }

  /** A string literal quoted the way ClickHouse quotes one. */
  quoteLiteral(text: BytesIn): Buffer {
    return this.#calls.quoteString(bytesIn(text));
  }

  /** This build's error-code table, built on the first call and kept for the library's life on success only. */
  errorCodes(): ErrorCodeTable {
    this.#errorCodes ??= decodeErrorCodes(this.#calls.errorCodes());
    return this.#errorCodes;
  }

  /** The discovery query, which the caller runs against its server with its own client, binding `{database:String}` and `{table:String}`. */
  discoverQuery(): Buffer {
    return this.#calls.discoverQuery();
  }

  /** Read a server's `system.columns` answer (`FORMAT JSONEachRow`) into column declarations. */
  discoverColumns(rows: Uint8Array): Discovery {
    return decodeDiscovery(this.#calls.discoverColumns(rows));
  }

  /** Live handle counts per kind in this image: a diagnostic. */
  liveHandles(): Readonly<Record<string, number>> {
    return decodeLiveHandles(this.#calls.liveHandles());
  }

  /** Compile exactly one `CREATE TABLE` statement. */
  compileTable(createTable: BytesIn, options: CompileOptions = {}): Schema {
    return new Schema(
      this.#calls,
      this.#calls.schemaCreate(bytesIn(createTable), encodeSettings(options.settings, options.sessionTimezone)),
    );
  }
}

const libraries = new Map<LoadedImage, Library>();

/** The one `Library` of an image: two registries, two spellings or a hardlink of one artifact share one image and one object. */
export function libraryOf(image: LoadedImage, resolved: Resolved | undefined): Library {
  let lib = libraries.get(image);
  if (lib === undefined) {
    lib = new Library(image, resolved);
    libraries.set(image, lib);
  }
  return lib;
}

/**
 * Open a local build with no signed statement, such as an unpublished library
 * under test. It refuses with a `UsageError` unless the caller passes
 * `{ allow: true }` AND `CHTYPES_ALLOW_UNVERIFIED_LIBRARY=1` is set, warns once
 * per path, skips loader steps 1 and 5, and runs step 7 under the process setup
 * like any open. It is not reachable through a registry, and its `Library` has
 * no `resolved`, unless a registry had already opened the same image.
 */
export function openUnverified(path: string, options: { readonly allow: boolean }): Library {
  // The caller's two opt-ins are checked before anything is attempted: misuse, which unlocks nothing.
  checkUnverifiedAllowed(path, options.allow);
  const began = setupGeneration();
  try {
    const setup = commitSetup();
    const image = loadUnverified(path, { allow: options.allow, timezone: setup.timezone, defaults: setup.defaults });
    latchSetup();
    return libraryOf(image, undefined);
  } catch (err) {
    // Like any open: a failed attempt unlocks the setup record while no image has completed step 7.
    settleFailedOpen(began);
    throw err;
  }
}
