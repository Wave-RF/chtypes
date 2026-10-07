/**
 * `Server`: one ClickHouse server as a profile describes it (`chs_server`,
 * `input:server_profile`, `chs_server_create`, `chs_server_free`), made by
 * `Library.newServer` and passed to `Library.compileTable` as `server`. The
 * binding serializes the profile and validates nothing in it (`./settings.ts`).
 *
 * A server is immutable once made. `close` releases the caller's reference; a
 * schema compiled on the server holds its own counted reference inside the
 * library, so a server and its schemas close in any order. A closed server
 * raises a `UsageError` before any call (its freed handle would otherwise cross
 * as NULL, which the library takes for no server). A `FinalizationRegistry`
 * frees what the caller abandons.
 */

import type { ServerHandle } from './abi2/index.js';

export type { ServerProfile } from './settings.js';

const serverHandles = new WeakMap<Server, ServerHandle>();

/** The generated layer's handle behind a `Server`. */
export function serverHandleOf(server: Server): ServerHandle {
  return serverHandles.get(server) as ServerHandle;
}

/** One ClickHouse server, made by `Library.newServer`. */
export class Server {
  readonly #h: ServerHandle;

  /** Not for callers: use `Library.newServer`. */
  constructor(handle: ServerHandle) {
    this.#h = handle;
    serverHandles.set(this, handle);
  }

  /** Release this server; idempotent. Schemas compiled on it keep working. */
  close(): void {
    this.#h.close();
  }

  [Symbol.dispose](): void {
    this.close();
  }
}
