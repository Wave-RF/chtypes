/**
 * The registry over the real fetch derivation (`docs/reference/bindings-v1.md` §6):
 * the stub library, packaged as an OCI layout signed with the fixtures' TEST
 * key, resolved by `resolveInstalled` with that key trusted, handed through the
 * `Resolved -> LoadInput` adapter and loaded. A hand-built `LoadInput` cannot
 * test the adapter, so nothing here builds one.
 *
 * Needs `$CHTYPES_ABI2_STUBS` (the stub libraries); without it every case here
 * SKIPS LOUDLY by name. The layout is built at test time, by the same recipe
 * the fixture generator uses: a one-file tar, zstd-compressed, a manifest, a
 * predicate-carrying in-toto statement signed as a DSSE envelope, and a
 * Sigstore bundle attached as a referrer manifest.
 *
 * The fetch layer speaks the ABI v2 dev channel (`src/ocifetch/channel.ts`):
 * abi-2 predicates, schema-2 records, the v2-dev cache subroot, no pinning,
 * and no base or trust override. These cases need the test key trusted, so the
 * file runs under `allowOverridesForTests` (exactly those overrides honored,
 * nothing else changed), and the layout is written where the fetch layer
 * itself says it reads an explicit cache: `<cache>/v2-dev` (rule r5). The last
 * case switches to the dev channel exactly and proves the trust option is
 * ignored there, with its warning (rule r6).
 */

import { createHash, createPrivateKey, sign } from 'node:crypto';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';
import { zstdCompress } from 'node:zlib';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import type { Predicate } from '../../src/abi2/loader.js';
import {
  ArtifactCorruptError,
  ArtifactError,
  ArtifactMissingError,
  Registry,
  UsageError,
} from '../../src/index.js';
import {
  ARTIFACT_TYPE,
  DSSE_PAYLOAD_TYPE,
  MEDIA_TYPE_BUNDLE,
  MEDIA_TYPE_CONFIG,
  MEDIA_TYPE_EMPTY_CONFIG,
  MEDIA_TYPE_LAYER,
  MEDIA_TYPE_MANIFEST,
  PREDICATE_TYPE_ARTIFACT,
  STATEMENT_TYPE,
  TEST_KEYS,
} from '../../src/ocifetch/constants.gen.js';
import {
  allowOverridesForTests,
  captureIgnoredForTests,
  DEV_CACHE_DIR,
  ignoredSettingsWarned,
  useDevChannelForTests,
} from '../../src/ocifetch/channel.js';
import { cacheRoot } from '../../src/ocifetch/layout.js';
import { resetSetupForTests } from '../../src/setup.js';

// The dev channel, with the test key's trust override honored (see the module comment).
allowOverridesForTests();

/** Where the fetch layer reads the explicit cache `cache`: its v2-dev subroot (rule r5), asked of the fetch layer rather than spelled here. */
function layoutRootOf(cache: string): string {
  const root = cacheRoot(cache);
  if (root !== path.join(path.resolve(cache), DEV_CACHE_DIR)) {
    throw new Error(`the fetch layer reads the explicit cache ${cache} at ${root}, not its ${DEV_CACHE_DIR} subroot (rule r5)`);
  }
  return root;
}

const STUBS_DIR = process.env.CHTYPES_ABI2_STUBS;
const stubsAvailable = typeof STUBS_DIR === 'string' && STUBS_DIR.length > 0;

const KEY_PEM = path.resolve(import.meta.dirname, '../../../tests/fixtures/fetch-v1/test-key/private.pem');
const TEST_KEY = TEST_KEYS[0];
const zstd = promisify(zstdCompress);

const sha256 = (b: Uint8Array): string => createHash('sha256').update(b).digest('hex');

// ---------------------------------------------------------------- tar, by hand

const BLOCK = 512;

function ustarHeader(name: string, size: number, typeflag: string): Buffer {
  const header = Buffer.alloc(BLOCK);
  header.write(name, 0, 'utf8');
  header.write('0000755\0', 100, 'ascii');
  header.write('0000000\0', 108, 'ascii');
  header.write('0000000\0', 116, 'ascii');
  header.write(`${size.toString(8).padStart(11, '0')}\0`, 124, 'ascii');
  header.write('00000000000\0', 136, 'ascii');
  header.write('        ', 148, 'ascii');
  header.write(typeflag, 156, 'ascii');
  header.write('ustar\0', 257, 'ascii');
  header.write('00', 263, 'ascii');
  let sum = 0;
  for (let i = 0; i < BLOCK; i++) sum += header[i] as number;
  header.write(`${sum.toString(8).padStart(6, '0')}\0 `, 148, 'ascii');
  return header;
}

function tarOfOneFile(name: string, content: Buffer): Buffer {
  const rem = content.length % BLOCK;
  const padded = rem === 0 ? content : Buffer.concat([content, Buffer.alloc(BLOCK - rem)]);
  return Buffer.concat([ustarHeader(name, content.length, '0'), padded, Buffer.alloc(BLOCK * 2)]);
}

// ------------------------------------------------------------ the signed layout

interface Built {
  readonly cacheDir: string;
  readonly predicate: Record<string, unknown>;
}

interface StubsDoc {
  readonly variants: Record<string, { readonly predicate: Predicate }>;
}

/** Package the stub as a signed OCI layout in the explicit cache `cache` (at its v2-dev subroot), whose predicate carries `patch` on top of the stub's own. */
async function buildLayout(cache: string, patch: Record<string, unknown> = {}): Promise<Built> {
  const root = layoutRootOf(cache);
  const stubs = JSON.parse(readFileSync(path.join(STUBS_DIR as string, 'stubs.json'), 'utf8')) as StubsDoc;
  const variant = stubs.variants.ok;
  if (variant === undefined) throw new Error('stubs.json has no ok variant');
  const library = readFileSync(path.join(STUBS_DIR as string, 'ok.so'));
  const libName = process.platform === 'darwin' ? 'libchtypes.dylib' : 'libchtypes.so';

  const blobs = path.join(root, 'blobs', 'sha256');
  mkdirSync(blobs, { recursive: true });
  const put = (mediaType: string, bytes: Buffer): { mediaType: string; digest: string; size: number } => {
    const hex = sha256(bytes);
    writeFileSync(path.join(blobs, hex), bytes);
    return { mediaType, digest: `sha256:${hex}`, size: bytes.length };
  };

  const layer = put(MEDIA_TYPE_LAYER, await zstd(tarOfOneFile(libName, library)));
  const predicate: Record<string, unknown> = {
    ...variant.predicate,
    clickhouse_minor: '26.8',
    clickhouse_commit: 'a'.repeat(40),
    library: libName,
    library_sha256: sha256(library),
    library_bytes: library.length,
    ...patch,
  };
  const config = put(MEDIA_TYPE_CONFIG, Buffer.from(`${JSON.stringify(predicate)}\n`));
  const manifest = put(
    MEDIA_TYPE_MANIFEST,
    Buffer.from(
      `${JSON.stringify({ schemaVersion: 2, mediaType: MEDIA_TYPE_MANIFEST, artifactType: ARTIFACT_TYPE, config, layers: [layer] })}\n`,
    ),
  );

  const statement = Buffer.from(
    `${JSON.stringify({
      _type: STATEMENT_TYPE,
      subject: [{ name: `${libName}.tar.zst`, digest: { sha256: layer.digest.slice('sha256:'.length) } }],
      predicateType: PREDICATE_TYPE_ARTIFACT,
      predicate,
    })}\n`,
  );
  const pae = Buffer.concat([
    Buffer.from(`DSSEv1 ${DSSE_PAYLOAD_TYPE.length} ${DSSE_PAYLOAD_TYPE} ${statement.length} `, 'ascii'),
    statement,
  ]);
  const signature = sign(null, pae, createPrivateKey(readFileSync(KEY_PEM)));
  const bundle = put(
    MEDIA_TYPE_BUNDLE,
    Buffer.from(
      JSON.stringify({
        mediaType: MEDIA_TYPE_BUNDLE,
        verificationMaterial: { publicKey: { hint: TEST_KEY.keyid } },
        dsseEnvelope: {
          payload: statement.toString('base64'),
          payloadType: DSSE_PAYLOAD_TYPE,
          signatures: [{ sig: signature.toString('base64') }],
        },
      }),
    ),
  );
  const empty = put(MEDIA_TYPE_EMPTY_CONFIG, Buffer.from('{}'));
  put(
    MEDIA_TYPE_MANIFEST,
    Buffer.from(
      `${JSON.stringify({
        schemaVersion: 2,
        mediaType: MEDIA_TYPE_MANIFEST,
        artifactType: MEDIA_TYPE_BUNDLE,
        config: empty,
        layers: [bundle],
        subject: manifest,
      })}\n`,
    ),
  );

  writeFileSync(path.join(root, 'oci-layout'), '{"imageLayoutVersion":"1.0.0"}');
  writeFileSync(
    path.join(root, 'index.json'),
    JSON.stringify({
      schemaVersion: 2,
      manifests: [{ ...manifest, artifactType: ARTIFACT_TYPE, platform: { os: String(variant.predicate.os), architecture: String(variant.predicate.arch) } }],
    }),
  );
  return { cacheDir: cache, predicate };
}

const trustTest = [TEST_KEY.ed25519Hex];

describe.skipIf(!stubsAvailable)('the registry over the real fetch derivation, on the stub', () => {
  let work: string;
  let good: Built;
  let mismatched: Built;

  beforeAll(async () => {
    work = mkdtempSync(path.join(tmpdir(), 'ts-registry-adapter-'));
    // A predicate with an extra, unknown field proves the adapter and the fetch layer carry it through verbatim.
    good = await buildLayout(path.join(work, 'good'), { x_extra_field: { kept: ['verbatim'] } });
    // A statement that is genuinely signed but disagrees with the library's own build info.
    mismatched = await buildLayout(path.join(work, 'mismatched'), { build: '19990101.000000' });
    resetSetupForTests();
  });

  afterAll(() => {
    rmSync(work, { recursive: true, force: true });
  });

  it('opens a request through resolve, the adapter and loader steps 1 to 7, and keeps the fetch record', async () => {
    const registry = await Registry.open({ fetch: { cacheDir: good.cacheDir, systemDirs: [], trustedKeys: trustTest } });
    expect(registry.libraries()).toEqual([]); // construction opens nothing
    const library = await registry.for('26.8');
    expect(library.version).toBe('26.8.15.10');
    expect(library.resolved?.signedBy).toBe(TEST_KEY.keyid);
    expect(library.resolved?.libraryPath).toBe(library.path);
    expect(library.resolved?.request).toBe('26.8');
    // The predicate went through exactly as the signed statement carried it, unknown fields included.
    expect(library.resolved?.predicate).toEqual(good.predicate);
    expect(library.compileTable('CREATE TABLE t (x Int32) ENGINE = Memory').describe().columns).toEqual([]);
  });

  it('memoizes per request for the registry life, shares one Library per image, and lists what is installed and open', async () => {
    const registry = await Registry.open({ fetch: { cacheDir: good.cacheDir, systemDirs: [], trustedKeys: trustTest } });
    const first = await registry.for('26.8');
    expect(await registry.for('26.8')).toBe(first);
    expect(await registry.for('26.8.15.10')).toBe(first); // another spelling, the same image, the same object
    expect(registry.libraries()).toEqual([first]);
    const installed = await registry.installed();
    expect(installed).toHaveLength(1);
    expect(installed[0]?.libraryPath).toBe(first.path);
    // A second registry shares the image.
    const other = await Registry.open({ fetch: { cacheDir: good.cacheDir, systemDirs: [], trustedKeys: trustTest } });
    expect(await other.for('26.8')).toBe(first);
  });

  it('opens each preload request at construction, in order, and a preload that nothing installed answers is the ordinary missing error', async () => {
    const registry = await Registry.open({ fetch: { cacheDir: good.cacheDir, systemDirs: [], trustedKeys: trustTest }, preload: ['26.8'] });
    expect(registry.libraries()).toHaveLength(1);
    await expect(
      Registry.open({ fetch: { cacheDir: good.cacheDir, systemDirs: [], trustedKeys: trustTest }, preload: ['25.8'] }),
    ).rejects.toBeInstanceOf(ArtifactMissingError);
  });

  it('names the request and the platform when nothing installed answers and autofetch is off', async () => {
    const registry = await Registry.open({ fetch: { cacheDir: good.cacheDir, systemDirs: [], trustedKeys: trustTest }, autofetch: false });
    const err = await registry.for('25.8').then(
      () => undefined,
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(ArtifactMissingError);
    expect(err).toBeInstanceOf(ArtifactError);
    expect((err as Error).message).toContain('25.8');
    expect((err as Error).message).toContain(`${process.platform}-${process.arch === 'x64' ? 'amd64' : process.arch}`);
  });

  it('refuses a version spelling the fetch layer would refuse as a UsageError, before touching the network or the cache', async () => {
    const registry = await Registry.open({ fetch: { cacheDir: good.cacheDir, systemDirs: [], trustedKeys: trustTest } });
    for (const bad of ['v26.8', '26.8-lts', '26', '', '26.8.x']) {
      await expect(registry.for(bad)).rejects.toBeInstanceOf(UsageError);
    }
  });

  it('never trusts the test key by default: under the default trust list the same layout is simply not installed', async () => {
    // A layout nothing has installed yet: an install made under the test key is a verified record that later requests may read.
    const fresh = await buildLayout(path.join(work, 'untrusted'));
    const registry = await Registry.open({ fetch: { cacheDir: fresh.cacheDir, systemDirs: [] } });
    await expect(registry.for('26.8')).rejects.toBeInstanceOf(ArtifactMissingError);
  });

  it('honors no trust override on the dev channel exactly: the trustedKeys option is ignored, with its warning, and the layout is not installed (rule r6)', async () => {
    const fresh = await buildLayout(path.join(work, 'no-override'));
    const warnings: string[] = [];
    const restoreWarnings = captureIgnoredForTests((text) => warnings.push(text));
    const restoreChannel = useDevChannelForTests();
    try {
      const registry = await Registry.open({ fetch: { cacheDir: fresh.cacheDir, systemDirs: [], trustedKeys: trustTest } });
      await expect(registry.for('26.8')).rejects.toBeInstanceOf(ArtifactMissingError);
      expect(ignoredSettingsWarned()).toContain('the trustedKeys option');
      expect(warnings.join('').match(/WARNING: the trustedKeys option is set and IGNORED/g)).toHaveLength(1);
    } finally {
      restoreChannel();
      restoreWarnings();
    }
  });

  it('refuses a pinning fetch option at construction, as a UsageError, before anything is read (rule r6)', async () => {
    for (const fetch of [{ frozen: true }, { lockPath: 'chtypes.lock' }, { lockWrite: true }, { update: true }]) {
      await expect(Registry.open({ fetch: { cacheDir: good.cacheDir, systemDirs: [], ...fetch } })).rejects.toThrow(
        /refused by a 2\.0\.0-dev SDK/,
      );
      await expect(Registry.open({ fetch: { cacheDir: good.cacheDir, systemDirs: [], ...fetch } })).rejects.toBeInstanceOf(UsageError);
    }
  });

  it('turns a signed statement that disagrees with the library into the artifact-corrupt class: one family with the fetch layer', async () => {
    const registry = await Registry.open({ fetch: { cacheDir: mismatched.cacheDir, systemDirs: [], trustedKeys: trustTest } });
    const err = await registry.for('26.8').then(
      () => undefined,
      (e: unknown) => e,
    );
    // A second image (another path), signed correctly but disagreeing with its own build info: loader step 5 refuses.
    expect(err).toBeInstanceOf(ArtifactCorruptError);
    expect((err as ArtifactError).reason).toBe('build_info_mismatch:build');
    expect((err as ArtifactError).code).toBe('CHTYPES_ARTIFACT_CORRUPT');
    expect((err as ArtifactError).name).toBe('ArtifactCorruptError');
  });
});
