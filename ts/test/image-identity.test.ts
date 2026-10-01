/**
 * One artifact image, one `chs_init` — keyed on the FILE, not the path (#355).
 *
 * `dlopen` maps one image per file. A hardlink is a different path to the same
 * file, so it is handed the image already mapped; a guard keyed on the realpath
 * let it re-run `chs_init` on that live image and move the first opener's zone.
 * These tests copy a real artifact first, so each owns a FRESH image no other
 * test has initialized, and open it through `Registry` — the public path, which
 * reaches the guard — so every key comes from the binding's own stat.
 */

import {
  copyFileSync,
  existsSync,
  linkSync,
  mkdirSync,
  mkdtempSync,
  renameSync,
  rmSync,
  statSync,
  symlinkSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterAll, describe, expect, it } from 'vitest';
import { NativeLibrary } from '../src/ffi.js';
import {
  Format,
  InitConflictError,
  type Library,
  Registry,
  RegistryError,
  looksLikeRegistry,
  resolveRegistryDir,
} from '../src/index.js';

const REGISTRY = resolveRegistryDir();
const HAVE_REGISTRY = REGISTRY !== null && looksLikeRegistry(REGISTRY);

if (!HAVE_REGISTRY) {
  console.warn(
    [
      '',
      '[chtypes] image-identity tests SKIPPED: no artifact registry on the search path.',
      '  Fetch one into the per-user cache with scripts/fetch.sh 25.8 (docs/guides/fetch.md),',
      '  or point CHTYPES_REGISTRY at a registry directory.',
      '',
    ].join('\n'),
  );
}

const roots: string[] = [];
/** Registries whose images this file initialized; each image is shut down once, at the end. */
const toClose: Registry[] = [];

afterAll(() => {
  for (const registry of toClose) registry.close();
  for (const root of roots) rmSync(root, { recursive: true, force: true });
});

function scratch(tag: string): string {
  const dir = mkdtempSync(path.join(tmpdir(), `chtypes-image-${tag}-`));
  roots.push(dir);
  return dir;
}

/** The newest line's library and its artifact directory. */
function source(): { line: string; dir: string; library: string } {
  const registry = new Registry(REGISTRY ?? undefined);
  const line = registry.versions().at(-1)!;
  const library = registry.for(line).path;
  return { line, dir: path.dirname(library), library };
}

/** `<root>/<line>/` holding a FRESH copy of the artifact (its own inode). */
function stage(src: ReturnType<typeof source>, root: string): string {
  const sub = path.join(root, src.line);
  mkdirSync(sub, { recursive: true });
  for (const name of ['manifest.json', 'unsafe_families.txt']) {
    if (existsSync(path.join(src.dir, name))) copyFileSync(path.join(src.dir, name), path.join(sub, name));
  }
  const library = path.join(sub, path.basename(src.library));
  copyFileSync(src.library, library);
  return library;
}

/** Open `line` from `root` alone under `timezone`, the way a caller does. */
function openUnder(root: string, line: string, timezone: string): Library {
  return new Registry(root, { timezone }).for(line);
}

/**
 * What the live image says about a zone-sensitive value: a bare DateTime column
 * given the epoch, rendered under whatever zone `chs_init` last set.
 */
function epoch(library: Library): string {
  const schema = library.compileDdl('x DateTime');
  try {
    const got = schema.rows(Format.JSONEachRow, Buffer.from('{"x":0}\n', 'utf8'));
    return got.rows[0]!.values[0]!.text;
  } finally {
    schema.close();
  }
}

describe.skipIf(!HAVE_REGISTRY)('one image per FILE over a real artifact', () => {
  it('refuses a hardlink under another zone as InitConflictError, and allows it under the same zone', () => {
    const src = source();
    const root = scratch('hardlink');
    const origRoot = path.join(root, 'orig');
    const orig = stage(src, origRoot);
    const linkRoot = path.join(root, 'link');
    mkdirSync(path.join(linkRoot, src.line), { recursive: true });
    copyFileSync(path.join(src.dir, 'manifest.json'), path.join(linkRoot, src.line, 'manifest.json'));
    const link = path.join(linkRoot, src.line, path.basename(orig));
    linkSync(orig, link);

    const firstRegistry = new Registry(origRoot, { timezone: 'UTC' });
    toClose.push(firstRegistry);
    const first = firstRegistry.for(src.line);
    const before = epoch(first);

    let refused: unknown;
    try {
      openUnder(linkRoot, src.line, 'Asia/Tokyo');
    } catch (err) {
      refused = err;
    }
    expect(refused, 'a hardlink was opened under another zone: chs_init re-ran on the live image').toBeInstanceOf(
      InitConflictError,
    );
    expect(refused).toBeInstanceOf(RegistryError);
    const conflict = refused as InitConflictError;
    expect([conflict.path, conflict.have, conflict.want]).toEqual([link, 'UTC', 'Asia/Tokyo']);
    expect(conflict.message).toContain(link);
    expect(epoch(first), "the refused open moved the live image's zone").toBe(before);

    // The same path twice, and the hardlink, under the same zone: allowed.
    expect(epoch(openUnder(origRoot, src.line, 'UTC'))).toBe(before);
    expect(epoch(openUnder(linkRoot, src.line, 'UTC'))).toBe(before);
    console.log('image identity: ran the hardlink case');
  });

  it('keeps a replaced file at an open path as the open image', () => {
    // The loader matches an open path before it looks at the file, so a NEW
    // file (a fresh inode) renamed over an open path is still the open image —
    // keyed on the inode alone it would look new, and chs_init would re-run.
    const src = source();
    const root = scratch('replaced');
    const library = stage(src, root);
    const firstRegistry = new Registry(root, { timezone: 'UTC' });
    toClose.push(firstRegistry);
    const first = firstRegistry.for(src.line);
    const before = epoch(first);

    const old = statSync(library, { bigint: true });
    copyFileSync(src.library, `${library}.new`);
    renameSync(`${library}.new`, library);
    const now = statSync(library, { bigint: true });
    expect(`${now.dev}:${now.ino}`, 'precondition: a different file at the path').not.toBe(`${old.dev}:${old.ino}`);

    expect(() => openUnder(root, src.line, 'Asia/Tokyo')).toThrowError(InitConflictError);
    expect(epoch(first), "the refused open moved the live image's zone").toBe(before);
    expect(epoch(openUnder(root, src.line, 'UTC'))).toBe(before);
    console.log('image identity: ran the replaced-file case');
  });
});

describe('one image per FILE, without an artifact', () => {
  it('refuses a path it cannot stat, naming it, before anything is dlopened', () => {
    // A dangling symlink is the case that tells stat (which follows it) from
    // lstat (which does not); the path is never keyed on its spelling instead.
    const root = scratch('unstattable');
    const dangling = path.join(root, 'libchtypes.so');
    symlinkSync(path.join(root, 'missing.so'), dangling);
    let refused: unknown;
    try {
      NativeLibrary.open(dangling);
    } catch (err) {
      refused = err;
    }
    expect(refused).toBeInstanceOf(RegistryError);
    expect(refused).not.toBeInstanceOf(InitConflictError);
    expect((refused as Error).message).toContain('cannot identify');
    expect((refused as Error).message).toContain(dangling);
    expect(((refused as Error).cause as NodeJS.ErrnoException).code).toBe('ENOENT');
  });
});
