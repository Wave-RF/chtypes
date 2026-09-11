#!/usr/bin/env node
/**
 * `chtypes` — the one CLI surface every SDK carries (docs/guides/fetch.md §6),
 * spelled identically in all four:
 *
 *   chtypes fetch <line>... [--all] [--platform <os-arch>] [--dest <dir>]
 *                           [--tag <t> | --url <base>] [--lock <file>] [--frozen]
 *                           [--force] [--offline]
 *   chtypes verify [--dest <dir>]        re-hash every installed line against its manifest
 *   chtypes list   [--dest <dir>]        what is installed, and what the release offers
 *   chtypes where                        the registry directory fetch would write to
 *
 * Exit codes: 0 ok · 1 verification failed · 2 usage · 3 source unreachable ·
 * 4 not published for this platform/line. Progress goes to stderr; `fetch`
 * prints the installed directory alone on stdout, so it composes:
 *
 *   dir="$(npx @wavehouse/chtypes fetch 25.8)"
 *
 * `runCli` is exported so the suite can drive the command in-process; the
 * file runs `main` only when it is the process's entry point (the `bin`).
 */

import { realpathSync, statSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { ArtifactUnpublishedError, ChtypesError, FetchError, SourceUnreachableError } from './errors.js';
import { ensure, ensureAll, listArtifacts, resolvePlatform, verifyInstalled, type EnsureOptions, type FetchEvent } from './fetch.js';
import { fetchDestination } from './paths.js';

/** The §6 exit codes. */
export const EXIT = {
  ok: 0,
  verificationFailed: 1,
  usage: 2,
  sourceUnreachable: 3,
  unpublished: 4,
} as const;

/** Where the command writes; the suite captures both. */
export interface CliIo {
  readonly stdout: (text: string) => void;
  readonly stderr: (text: string) => void;
  /** Whether stderr is a terminal (progress is redrawn in place only then). */
  readonly tty?: boolean | undefined;
}

const USAGE = `usage: chtypes <command> [options]

  chtypes fetch <line>... [--all] [--platform <os-arch>] [--dest <dir>]
                          [--tag <t> | --url <base>] [--lock <file>] [--frozen]
                          [--force] [--offline]
  chtypes verify [--dest <dir>] [--platform <os-arch>]
  chtypes list   [--dest <dir>] [--platform <os-arch>] [--tag <t> | --url <base>] [--offline]
  chtypes where  [--dest <dir>] [--platform <os-arch>]

  <line>      a ClickHouse line (25.8) or an exact patch (25.8.28.1-lts, a hard requirement)
  --all       every line the release publishes for the platform
  --platform  <os>-<arch> (linux|darwin)-(arm64|amd64); default: this host
  --dest      the registry directory; default: CHTYPES_REGISTRY, else the per-user cache
  --tag       a release tag on the artifacts host (default: the rolling "artifacts")
  --url       any other base: https://…, file://…, or a directory
  --lock      record what was installed into this lock file (default with --frozen: chtypes.lock)
  --frozen    refuse anything the lock file does not pin
  --force     re-download an installed line
  --offline   never touch the source: installed-and-verified is fine, anything else fails
  -q, --quiet no progress on stderr
  -h, --help  this text;  -V, --version  the package version

exit codes: 0 ok · 1 verification failed · 2 usage · 3 source unreachable · 4 not published
environment: CHTYPES_ARTIFACTS_URL, CHTYPES_REGISTRY, CHTYPES_TRUSTED_KEYS, CHTYPES_ALLOW_UNSIGNED,
             CHTYPES_AUTOFETCH, CHTYPES_TARGET, XDG_CACHE_HOME
`;

const defaultIo: CliIo = {
  stdout: (text) => process.stdout.write(text),
  stderr: (text) => process.stderr.write(text),
  tty: process.stderr.isTTY === true,
};

/**
 * Run the command with `argv` (the arguments after the program name) and
 * return its exit code. Never throws for a user-facing failure: every §7
 * verdict is printed to stderr and mapped to its exit code.
 */
export async function runCli(argv: readonly string[], io: CliIo = defaultIo): Promise<number> {
  let parsed: Parsed;
  try {
    parsed = parseArgs({ ...CONFIG, args: [...argv] });
  } catch (err) {
    io.stderr(`chtypes: ${err instanceof Error ? err.message : String(err)}\n${USAGE}`);
    return EXIT.usage;
  }
  const { values, positionals } = parsed;
  if (values.help) {
    io.stdout(USAGE);
    return EXIT.ok;
  }
  if (values.version) {
    io.stdout(`${packageVersion()}\n`);
    return EXIT.ok;
  }
  const [command, ...rest] = positionals;
  if (command === undefined) {
    io.stderr(USAGE);
    return EXIT.usage;
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
        io.stderr(`chtypes: unknown command ${JSON.stringify(command)}\n${USAGE}`);
        return EXIT.usage;
    }
  } catch (err) {
    return report(err, io);
  }
}

const CONFIG = {
  allowPositionals: true,
  strict: true,
  options: {
    all: { type: 'boolean' },
    platform: { type: 'string' },
    dest: { type: 'string' },
    tag: { type: 'string' },
    url: { type: 'string' },
    lock: { type: 'string' },
    frozen: { type: 'boolean' },
    force: { type: 'boolean' },
    offline: { type: 'boolean' },
    quiet: { type: 'boolean', short: 'q' },
    help: { type: 'boolean', short: 'h' },
    version: { type: 'boolean', short: 'V' },
  },
} as const;

type Parsed = ReturnType<typeof parseArgs<typeof CONFIG>>;
type Values = Parsed['values'];

function ensureOptions(values: Values, io: CliIo): EnsureOptions {
  if (values.url !== undefined && values.tag !== undefined) {
    throw new ChtypesError('chtypes: --url names a full base; --tag selects a release on the artifacts host — pass one');
  }
  const lock = values.lock ?? (values.frozen ? 'chtypes.lock' : undefined);
  return {
    dest: values.dest,
    platform: values.platform,
    tag: values.tag,
    url: values.url,
    lock,
    frozen: values.frozen ?? false,
    force: values.force ?? false,
    offline: values.offline ?? false,
    onProgress: values.quiet ? undefined : progressPrinter(io),
  };
}

async function cmdFetch(lines: readonly string[], values: Values, io: CliIo): Promise<number> {
  const options = ensureOptions(values, io);
  if (values.all) {
    if (lines.length > 0) {
      throw new ChtypesError(`chtypes: --all installs every published line; drop the version argument (${lines.join(', ')})`);
    }
    const results = await ensureAll(options);
    if (results.length === 0) throw new ArtifactUnpublishedError('chtypes: the release publishes nothing for this platform');
    for (const r of results) io.stdout(`${r.dir}\n`);
    say(io, `${results.length} line(s) installed into ${results[0]!.registry}`);
    return EXIT.ok;
  }
  if (lines.length === 0) {
    io.stderr(`chtypes: a ClickHouse line is required (or --all)\n${USAGE}`);
    return EXIT.usage;
  }
  for (const line of lines) {
    const r = await ensure(line, options);
    io.stdout(`${r.dir}\n`);
  }
  return EXIT.ok;
}

async function cmdVerify(rest: readonly string[], values: Values, io: CliIo): Promise<number> {
  if (rest.length > 0) throw new ChtypesError(`chtypes: verify takes no positional arguments (${rest.join(', ')})`);
  const platform = resolvePlatform(values.platform);
  const dir = fetchDestination(values.dest, platform);
  const results = await verifyInstalled(values.dest, platform);
  if (results.length === 0) {
    say(io, `nothing installed in ${dir}`);
    return EXIT.ok;
  }
  let bad = 0;
  for (const r of results) {
    if (r.ok) io.stdout(`ok   ${r.line.padEnd(6)} ${r.version.padEnd(18)} ${r.library}  sha256 ${r.actual}\n`);
    else {
      bad += 1;
      io.stdout(`BAD  ${r.line.padEnd(6)} ${r.version.padEnd(18)} ${r.library}  ${r.problem}\n`);
    }
  }
  goldensLine(dir, io);
  say(io, bad === 0 ? `${results.length} line(s) verified in ${dir}` : `${bad} of ${results.length} line(s) FAILED verification in ${dir}`);
  return bad === 0 ? EXIT.ok : EXIT.verificationFailed;
}

async function cmdList(rest: readonly string[], values: Values, io: CliIo): Promise<number> {
  if (rest.length > 0) throw new ChtypesError(`chtypes: list takes no positional arguments (${rest.join(', ')})`);
  const options = ensureOptions(values, io);
  const listing = await listArtifacts({ ...options, onProgress: undefined });
  io.stdout(`installed in ${listing.registry} (${listing.platform}):\n`);
  if (listing.installed.length === 0) io.stdout('  (nothing)\n');
  for (const a of listing.installed) {
    io.stdout(`  ${a.line.padEnd(6)} ${a.version.padEnd(18)} ${a.library}${a.ok ? '' : `  (${a.problem})`}\n`);
  }
  if (listing.offered !== null) {
    const have = new Set(listing.installed.filter((a) => a.ok).map((a) => a.version));
    io.stdout(`release ${listing.source} offers for ${listing.platform}:\n`);
    if (listing.offered.length === 0) io.stdout('  (nothing)\n');
    for (const a of listing.offered) {
      io.stdout(`  ${a.clickhouse_minor.padEnd(6)} ${a.clickhouse_version.padEnd(18)} b${String(a.build).padEnd(3)} ${a.file}  ${a.bytes} bytes${have.has(a.clickhouse_version) ? '  (installed)' : ''}\n`);
    }
  }
  return EXIT.ok;
}

/**
 * The served golden set sits beside the artifacts, so "where is my registry" and
 * "is my registry sound" are both moments someone wants to know whether it is
 * there — a missing one is why the golden tests skip.
 */
function goldensLine(dir: string, io: CliIo): void {
  const g = path.join(dir, 'sdk-goldens.json');
  let size: number | null = null;
  try {
    size = statSync(g).size;
  } catch {
    size = null;
  }
  io.stdout(size === null ? `${g}  (golden set: not fetched — the golden tests will skip)\n` : `${g}  (golden set, ${size} bytes)\n`);
}

function cmdWhere(rest: readonly string[], values: Values, io: CliIo): number {
  if (rest.length > 0) throw new ChtypesError(`chtypes: where takes no positional arguments (${rest.join(', ')})`);
  const dir = fetchDestination(values.dest, resolvePlatform(values.platform));
  io.stdout(`${dir}\n`);
  goldensLine(dir, io);
  return EXIT.ok;
}

/** Map an error to its §6 exit code, after printing it. */
function report(err: unknown, io: CliIo): number {
  if (err instanceof FetchError) {
    io.stderr(`${err.message} [${err.code}]\n`);
    if (err instanceof SourceUnreachableError) return EXIT.sourceUnreachable;
    if (err instanceof ArtifactUnpublishedError) return EXIT.unpublished;
    return EXIT.verificationFailed;
  }
  if (err instanceof ChtypesError) {
    io.stderr(`${err.message}\n`);
    return EXIT.usage;
  }
  io.stderr(`chtypes: ${err instanceof Error ? (err.stack ?? err.message) : String(err)}\n`);
  return EXIT.verificationFailed;
}

function say(io: CliIo, message: string): void {
  io.stderr(io.tty ? `[1m==> ${message}[0m\n` : `==> ${message}\n`);
}

/** Status lines as `==> …`; download progress redrawn in place on a terminal, at 10 % steps otherwise. */
function progressPrinter(io: CliIo): (event: FetchEvent) => void {
  let lastStep = -1;
  let drawing = false;
  return (event) => {
    if (event.type === 'status') {
      if (drawing) {
        io.stderr('\n');
        drawing = false;
      }
      say(io, event.message);
      return;
    }
    const pct = event.total > 0 ? Math.min(100, Math.floor((event.received * 100) / event.total)) : 0;
    const mb = (n: number): string => (n / 1e6).toFixed(1);
    if (io.tty) {
      io.stderr(`\r    ${event.file}  ${mb(event.received)} / ${mb(event.total)} MB  ${pct}%`);
      drawing = true;
      if (event.received >= event.total) {
        io.stderr('\n');
        drawing = false;
      }
      return;
    }
    const step = Math.floor(pct / 10);
    if (step !== lastStep) {
      lastStep = step;
      io.stderr(`    ${event.file}  ${mb(event.received)} / ${mb(event.total)} MB  ${pct}%\n`);
    }
  };
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
      process.exitCode = EXIT.verificationFailed;
    },
  );
}
