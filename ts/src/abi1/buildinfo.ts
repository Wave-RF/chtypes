/**
 * `BuildInfo`: the decoded form of the library's static build-info JSON
 * (`docs/reference/bindings-v1.md` §5, `BuildInfo`). Decoded once, at loader
 * step 4, where a malformed document is a `LoaderCorruptError`
 * (`build_info_malformed`): ASCII only, no duplicate key at any depth,
 * `schema` equal to 1, every required field of its type.
 */

import { LoaderCorruptError } from './errors.js';
import { DuplicateKeyError, isAsciiOnly, parseStrictJson } from './strictjson.js';

/** What the build itself says it supports, from its own format factory and document groups. */
export interface Capabilities {
  /** ClickHouse's own format names the build can read. */
  readonly inputFormats: readonly string[];
  /** ClickHouse's own format names the build can write. */
  readonly exportFormats: readonly string[];
  readonly docFlags: readonly string[];
  /** Open: an entry is a build-level feature, the first being `default_generators`. */
  readonly features: readonly string[];
}

export interface BuildInfo {
  readonly schema: number;
  readonly abi: number;
  readonly abiFingerprint: string;
  /** Four parts, no channel. */
  readonly clickhouseVersion: string;
  readonly channel: string;
  readonly clickhouseMinor: string;
  readonly clickhouseCommit: string;
  readonly coreCommit: string;
  readonly build: string;
  readonly inputsSha256: string;
  readonly os: string;
  readonly arch: string;
  /** The parsed object, uninterpreted. */
  readonly toolchain: Readonly<Record<string, unknown>>;
  readonly capabilities: Capabilities;
  /** The exact bytes the library returned. */
  readonly raw: Buffer;
}

function malformed(path: string, want: string, got: string): never {
  throw new LoaderCorruptError({ reason: 'build_info_malformed', path, want, got });
}

function kindOf(v: unknown): string {
  return v === null ? 'null' : Array.isArray(v) ? 'array' : typeof v;
}

function str(path: string, obj: Record<string, unknown>, key: string): string {
  const v = obj[key];
  if (typeof v !== 'string') malformed(path, `${key} as a string`, kindOf(v));
  return v;
}

function int(path: string, obj: Record<string, unknown>, key: string): number {
  const v = obj[key];
  if (typeof v !== 'number' || !Number.isInteger(v)) malformed(path, `${key} as an integer`, kindOf(v));
  return v;
}

function strList(path: string, obj: Record<string, unknown>, key: string): readonly string[] {
  const v = obj[key];
  if (!Array.isArray(v) || !v.every((e) => typeof e === 'string')) malformed(path, `${key} as a list of strings`, kindOf(v));
  return v as string[];
}

/** Decode the build-info text the library returned; every failure is `build_info_malformed`. */
export function decodeBuildInfo(path: string, text: string): BuildInfo {
  if (!isAsciiOnly(text)) malformed(path, 'ASCII only', 'a non-ASCII byte');
  let parsed: unknown;
  try {
    parsed = parseStrictJson(text);
  } catch (err) {
    if (err instanceof DuplicateKeyError) malformed(path, 'no duplicate keys', err.message);
    malformed(path, 'valid JSON', 'a parse error');
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) malformed(path, 'a JSON object', kindOf(parsed));
  const obj = parsed as Record<string, unknown>;
  if (obj.schema !== 1) malformed(path, 'schema === 1', JSON.stringify(obj.schema));
  const toolchain = obj.toolchain;
  if (typeof toolchain !== 'object' || toolchain === null || Array.isArray(toolchain)) {
    malformed(path, 'toolchain as an object', kindOf(toolchain));
  }
  const caps = obj.capabilities;
  if (typeof caps !== 'object' || caps === null || Array.isArray(caps)) malformed(path, 'capabilities as an object', kindOf(caps));
  const capsObj = caps as Record<string, unknown>;
  return {
    schema: 1,
    abi: int(path, obj, 'abi'),
    abiFingerprint: str(path, obj, 'abi_fingerprint'),
    clickhouseVersion: str(path, obj, 'clickhouse_version'),
    channel: str(path, obj, 'channel'),
    clickhouseMinor: str(path, obj, 'clickhouse_minor'),
    clickhouseCommit: str(path, obj, 'clickhouse_commit'),
    coreCommit: str(path, obj, 'core_commit'),
    build: str(path, obj, 'build'),
    inputsSha256: str(path, obj, 'inputs_sha256'),
    os: str(path, obj, 'os'),
    arch: str(path, obj, 'arch'),
    toolchain: toolchain as Record<string, unknown>,
    capabilities: {
      inputFormats: strList(path, capsObj, 'input_formats'),
      exportFormats: strList(path, capsObj, 'export_formats'),
      docFlags: strList(path, capsObj, 'doc_flags'),
      features: strList(path, capsObj, 'features'),
    },
    raw: Buffer.from(text, 'latin1'),
  };
}
