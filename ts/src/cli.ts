#!/usr/bin/env node
/**
 * `chtypes`: the one CLI every SDK carries, over the v1 fetch layer
 * (`docs/guides/fetch-v1.md`), spelled identically in all four:
 *
 *   chtypes fetch <version>... | --all  [--frozen] [--offline] [--lock <file>] [--update]
 *   chtypes verify                      re-hash every installed library against its verified record
 *   chtypes list                        what is installed, and the versions the registry publishes
 *   chtypes where                       the v1 cache root
 *
 * Options per cli-common: `fetch` takes `--platform`, `--cache`, `--lock`, `--frozen`,
 * `--offline`, `--update`; `verify`, `list` and `where` take `--cache` (`list` also
 * `--offline`); `-h/--help` anywhere, `--version` at top level. The registry base
 * comes only from `CHTYPES_ARTIFACTS_URL`.
 *
 * Exit statuses: 0 ok, 2 usage, and for every fetch error the status the
 * `errors` table of `spec/fetch-v1/constants.json` gives its code (generated
 * into `ERROR_EXIT_CODES`; no number is repeated here). A failed `verify` exits
 * with `CHTYPES_ARTIFACT_CORRUPT`'s. Progress goes to stderr; `fetch` prints one
 * installed directory per request on stdout alone, so it composes:
 *
 *   dir="$(npx @wavehouse/chtypes fetch 26.8)"
 *
 * `runCli` is exported so the suite can drive the command in-process; the file
 * runs `main` only when it is the process's entry point (the `bin`).
 */

import { realpathSync } from 'node:fs';
import { createRequire } from 'node:module';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { isChtypesError, UsageError } from './abi1/index.js';
import { withEnvironment } from './env.js';
import { ERROR_EXIT_CODES } from './ocifetch/constants.gen.js';
import {
  ArtifactUnpublishedError,
  cacheRoot,
  ensure,
  FetchV1Error,
  type FetchV1Options,
  hostPlatformKey,
  isPlatformKey,
  listInstalled,
  listTags,
  type PlatformKey,
  verifyInstalled,
} from './ocifetch/index.js';

/** The exit status of a usage error: the one status the error table does not own. */
export const EXIT_USAGE = 2;
/** Success. */
export const EXIT_OK = 0;

/** An error that is neither a fetch error nor a usage error. */
const EXIT_OTHER = 1;

/** Where the command writes; the suite captures both. */
export interface CliIo {
  readonly stdout: (text: string) => void;
  readonly stderr: (text: string) => void;
}

const USAGE = `usage: chtypes <command> [options]

  chtypes fetch <version>... | --all  [--frozen] [--offline] [--lock <file>] [--update]
  chtypes verify
  chtypes list
  chtypes where

  <version>   a ClickHouse version: 26.8, 26.8.15 or 26.8.15.10 (no "v", no channel suffix)
  --all       every line (two-part version) the registry publishes for the platform
  --frozen    fetch exactly what the lock file pins, by digest; refuse anything it does not (default lock: chtypes.lock)
  --offline   never touch the network: an installed, verified build is fine, anything else fails
  --lock      record what was installed into this lock file
  --update    re-resolve every request the lock holds and rewrite it (requires --lock; not with --frozen or --offline)
  --platform  <os>-<arch>: linux-amd64, linux-arm64 or darwin-arm64 (default: this host, or CHTYPES_TARGET)
  --cache     the cache root (default: CHTYPES_CACHE, else \${XDG_CACHE_HOME:-~/.cache}/chtypes/v1)
  -h, --help  this text;  --version  the package version

the registry base comes only from CHTYPES_ARTIFACTS_URL (default: the public registry)

exit status: 0 ok, 2 usage; a fetch error exits with its code's status in spec/fetch-v1/constants.json
environment: CHTYPES_ARTIFACTS_URL, CHTYPES_CACHE, CHTYPES_DOWNLOAD_TOKEN, CHTYPES_TRUSTED_KEYS,
             CHTYPES_ALLOW_UNSIGNED, CHTYPES_TARGET
`;

const defaultIo: CliIo = {
  stdout: (text) => process.stdout.write(text),
  stderr: (text) => process.stderr.write(text),
};

const CONFIG = {
  allowPositionals: true,
  strict: true,
  options: {
    all: { type: 'boolean' },
    frozen: { type: 'boolean' },
    offline: { type: 'boolean' },
    update: { type: 'boolean' },
    lock: { type: 'string' },
    platform: { type: 'string' },
    cache: { type: 'string' },
    help: { type: 'boolean', short: 'h' },
    version: { type: 'boolean' },
  },
} as const;

type Parsed = ReturnType<typeof parseArgs<typeof CONFIG>>;
type Values = Parsed['values'];

/** A command-line mistake: exits with the usage status. */
class CliUsageError extends Error {}

/**
 * Run the command with `argv` (the arguments after the program name) and
 * return its exit status. Never throws for a user-facing failure: every error
 * is printed to stderr and mapped to its status.
 */
export async function runCli(argv: readonly string[], io: CliIo = defaultIo): Promise<number> {
  let parsed: Parsed;
  try {
    parsed = parseArgs({ ...CONFIG, args: [...argv] });
  } catch (err) {
    io.stderr(`chtypes: ${err instanceof Error ? err.message : String(err)}\n${USAGE}`);
    return EXIT_USAGE;
  }
  const { values, positionals } = parsed;
  if (values.help) {
    io.stdout(USAGE);
    return EXIT_OK;
  }
  if (values.version) {
    io.stdout(`${packageVersion()}\n`);
    return EXIT_OK;
  }
  const [command, ...rest] = positionals;
  if (command === undefined) {
    io.stderr(USAGE);
    return EXIT_USAGE;
  }
  try {
    switch (command) {
      case 'fetch':
        return await cmdFetch(rest, values, io);
      case 'verify':
        return await cmdVerify(rest, values, io);
      case 'list':
        return await cmdList(rest, values, io);
      case 'where':
        return cmdWhere(rest, values, io);
      default:
        throw new CliUsageError(`unknown command ${JSON.stringify(command)}`);
    }
  } catch (err) {
    return report(err, io);
  }
}

function fetchOptions(values: Values, forFetch: boolean): FetchV1Options {
  if (!forFetch && values.platform !== undefined) throw new CliUsageError('--platform applies to fetch only');
  if (values.platform !== undefined && !isPlatformKey(values.platform)) {
    throw new CliUsageError(`--platform ${JSON.stringify(values.platform)} is not a platform: use linux-amd64, linux-arm64 or darwin-arm64`);
  }
  if (values.frozen && values.update) throw new CliUsageError('--frozen and --update contradict: one reads the lock, the other rewrites it');
  if (values.update && values.offline) throw new CliUsageError('--update and --offline contradict: an update must reach the registry');
  if (values.update && values.lock === undefined) throw new CliUsageError('--update requires --lock <file>');
  const lockPath = values.lock;
  return withEnvironment({
    ...(values.cache !== undefined ? { cacheDir: values.cache } : {}),
    ...(values.platform !== undefined ? { platform: values.platform as PlatformKey } : {}),
    ...(lockPath !== undefined ? { lockPath } : {}),
    ...(forFetch
      ? {
          offline: values.offline === true,
          frozen: values.frozen === true,
          update: values.update === true,
          lockWrite: lockPath !== undefined && values.frozen !== true && values.update !== true,
        }
      : {}),
  });
}

function say(io: CliIo, _values: Values, message: string): void {
  io.stderr(`==> ${message}\n`);
}

async function cmdFetch(requests: readonly string[], values: Values, io: CliIo): Promise<number> {
  const options = fetchOptions(values, true);
  let todo: readonly string[] = requests;
  if (values.all) {
    if (requests.length > 0) throw new CliUsageError(`--all fetches every published line; drop the version argument (${requests.join(', ')})`);
    if (values.frozen) throw new CliUsageError('--all and --frozen contradict: a frozen fetch makes no discovery, so it fetches only what you name');
    todo = await allLines(options);
    if (todo.length === 0) throw new ArtifactUnpublishedError('chtypes: the registry publishes no line to fetch');
  } else if (requests.length === 0) {
    throw new CliUsageError('a version is required (or --all)');
  }
  let installed = 0;
  let skipped = 0;
  for (const request of todo) {
    try {
      say(io, values, request);
      const r = await ensure(request, options);
      io.stdout(`${r.dir}\n`);
      for (const w of r.warnings) io.stderr(`chtypes: warning: ${w}\n`);
      installed += 1;
    } catch (err) {
      // `--all` walks every line; a line this platform has no build for is a note, not a failure.
      if (values.all && err instanceof ArtifactUnpublishedError) {
        say(io, values, `${request}: no build for this platform (${err.message})`);
        skipped += 1;
        continue;
      }
      throw err;
    }
  }
  if (values.all) {
    if (installed === 0) throw new ArtifactUnpublishedError('chtypes: no published line has a build for this platform');
    say(io, values, `${installed} line(s) installed, ${skipped} without a build for this platform`);
  }
  return EXIT_OK;
}

/** Every line: the two-part spellings the registry publishes (or, offline, the lines already installed for the platform). */
async function allLines(options: FetchV1Options): Promise<readonly string[]> {
  if (options.offline === true) {
    const platform = options.platform ?? hostPlatformKey(os.platform(), os.arch());
    const lines = new Set<string>();
    for (const r of await listInstalled(options)) if (r.platform === platform) lines.add(r.predicate.clickhouse_minor);
    return [...lines];
  }
  return (await listTags(options)).filter((t) => t.split('.').length === 2);
}

async function cmdVerify(rest: readonly string[], values: Values, io: CliIo): Promise<number> {
  noArguments('verify', rest);
  const options = fetchOptions(values, false);
  const root = cacheRoot(options.cacheDir);
  const results = await verifyInstalled(options);
  if (results.length === 0) return EXIT_OK;
  let bad = 0;
  for (const r of results) {
    if (!r.ok) {
      bad += 1;
      io.stderr(`BAD  ${r.platform}  ${r.dir}  ${r.detail}\n`);
    }
  }
  if (bad > 0) io.stderr(`${bad} of ${results.length} build(s) FAILED verification in ${root}\n`);
  return bad === 0 ? EXIT_OK : (ERROR_EXIT_CODES['CHTYPES_ARTIFACT_CORRUPT'] ?? EXIT_OTHER);
}

async function cmdList(rest: readonly string[], values: Values, io: CliIo): Promise<number> {
  noArguments('list', rest);
  const options = fetchOptions(values, false);
  const root = cacheRoot(options.cacheDir);
  const installed = await listInstalled(options);
  io.stdout(`installed in ${root}:\n`);
  if (installed.length === 0) io.stdout('  (nothing)\n');
  for (const r of installed) io.stdout(`  ${r.version.padEnd(14)} ${r.platform.padEnd(13)} build ${r.build}\n`);
  if (values.offline) return EXIT_OK;
  const tags = await listTags(options);
  io.stdout('published (a tag names a version; whether it has a build for your platform is support unknown until you fetch it):\n');
  if (tags.length === 0) io.stdout('  (nothing)\n');
  for (const t of tags) io.stdout(`  ${t}\n`);
  return EXIT_OK;
}

function cmdWhere(rest: readonly string[], values: Values, io: CliIo): number {
  noArguments('where', rest);
  io.stdout(`${cacheRoot(fetchOptions(values, false).cacheDir)}\n`);
  return EXIT_OK;
}

function noArguments(command: string, rest: readonly string[]): void {
  if (rest.length > 0) throw new CliUsageError(`${command} takes no positional arguments (${rest.join(', ')})`);
}

/** Print an error and return its exit status. */
function report(err: unknown, io: CliIo): number {
  if (err instanceof CliUsageError) {
    io.stderr(`chtypes: ${err.message}\n${USAGE}`);
    return EXIT_USAGE;
  }
  if (err instanceof FetchV1Error) {
    io.stderr(`${err.message} [${err.code}]\n`);
    return err.exitStatus;
  }
  if (err instanceof UsageError) {
    io.stderr(`${err.message}\n`);
    return EXIT_USAGE;
  }
  if (isChtypesError(err)) {
    io.stderr(`${err.message}\n`);
    return EXIT_OTHER;
  }
  io.stderr(`chtypes: ${err instanceof Error ? (err.stack ?? err.message) : String(err)}\n`);
  return EXIT_OTHER;
}

function packageVersion(): string {
  try {
    const pkg = createRequire(import.meta.url)('../package.json') as { version?: string };
    return pkg.version ?? '0.0.0';
  } catch {
    return '0.0.0';
  }
}

function invokedDirectly(): boolean {
  const entry = process.argv[1];
  if (entry === undefined) return false;
  try {
    return realpathSync(entry) === realpathSync(fileURLToPath(import.meta.url));
  } catch {
    return false;
  }
}

if (invokedDirectly()) {
  runCli(process.argv.slice(2)).then(
    (code) => {
      process.exitCode = code;
    },
    (err: unknown) => {
      process.stderr.write(`chtypes: ${err instanceof Error ? (err.stack ?? err.message) : String(err)}\n`);
      process.exitCode = EXIT_OTHER;
    },
  );
}
