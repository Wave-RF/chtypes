/**
 * A retired repository (`docs/guides/fetch-v1.md` §2, "A retired repository";
 * public issue #571). The registry's operator answers every route of a
 * retired repository with 410 Gone (`RETIRED_STATUSES`) and a short error
 * document. That is permanent: `http.ts` never retries it and throws
 * `SourceRetiredError`, which every loop over bases or candidates lets
 * through, so the caller sees the URL that answered and the registry's own
 * message, made safe to print by `retiredMessage`, the one function every
 * binding has (`tests/fixtures/retired-message/cases.json` is the table all
 * four answer alike).
 */

import type * as http from 'node:http';
import { RETIRED_MESSAGE_MAX_CODE_POINTS, RETIRED_STATUSES } from './constants.gen.js';
import { type Advisory, ArtifactRevokedError, FetchV1Error, SourceRetiredError } from './errors.js';

/** Is this status a retired repository's? */
export function isRetiredStatus(status: number): boolean {
  return (RETIRED_STATUSES as readonly number[]).includes(status);
}

function removed(codePoint: number): boolean {
  return (
    codePoint <= 0x1f ||
    (codePoint >= 0x7f && codePoint <= 0x9f) ||
    codePoint === 0x200e ||
    codePoint === 0x200f ||
    (codePoint >= 0x202a && codePoint <= 0x202e) ||
    (codePoint >= 0x2066 && codePoint <= 0x2069)
  );
}

/** A UTF-16 surrogate that is not half of a high-then-low pair: not a Unicode string. */
function hasLoneSurrogate(s: string): boolean {
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff) {
      const d = s.charCodeAt(i + 1);
      if (d >= 0xdc00 && d <= 0xdfff) {
        i++;
        continue;
      }
      return true;
    }
    if (c >= 0xdc00 && c <= 0xdfff) return true;
  }
  return false;
}

function objectOrUndefined(v: unknown): Record<string, unknown> | undefined {
  return v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : undefined;
}

/**
 * The registry's own message in a retired repository's response body, made
 * safe to print, or `null` when it sent none. `body` is what was read of it
 * (at most `RETIRED_BODY_MAX_BYTES`). When it is UTF-8 JSON whose
 * `errors[0].message` is a string, that string is the message: every code
 * point in U+0000-U+001F, U+007F-U+009F, U+200E, U+200F, U+202A-U+202E and
 * U+2066-U+2069 is removed, and the rest is cut to
 * `RETIRED_MESSAGE_MAX_CODE_POINTS` code points, with U+2026 appended when it
 * was cut. Nothing else is changed, and an empty result is no message.
 */
export function retiredMessage(body: Uint8Array): string | null {
  let doc: unknown;
  try {
    // fatal: invalid UTF-8 is no message. ignoreBOM: a byte order mark stays
    // in the text, so JSON.parse refuses it, as the other bindings' readers do.
    doc = JSON.parse(new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(body));
  } catch {
    return null;
  }
  const errors = objectOrUndefined(doc)?.['errors'];
  if (!Array.isArray(errors) || errors.length === 0) return null;
  const message = objectOrUndefined(errors[0])?.['message'];
  if (typeof message !== 'string') return null;
  const safe = sanitizeRetiredText(message);
  return safe === '' ? null : safe;
}

/**
 * `text` made safe to print, the retired message's rule for every served
 * string: every code point in U+0000-U+001F, U+007F-U+009F, U+200E, U+200F,
 * U+202A-U+202E and U+2066-U+2069 removed, then cut to
 * `RETIRED_MESSAGE_MAX_CODE_POINTS` code points with U+2026 appended when it was
 * cut. A string holding a lone surrogate is not Unicode text and is `''`, as is
 * one that is empty once made safe.
 */
export function sanitizeRetiredText(text: string): string {
  if (hasLoneSurrogate(text)) return '';
  const kept: string[] = [];
  for (const ch of text) {
    // A string iterates by code point, so a character outside the BMP is one.
    if (!removed(ch.codePointAt(0) ?? 0)) kept.push(ch);
  }
  if (kept.length > RETIRED_MESSAGE_MAX_CODE_POINTS) {
    kept.length = RETIRED_MESSAGE_MAX_CODE_POINTS;
    kept.push('…');
  }
  return kept.join('');
}

/** The error text for a retired repository's answer: the URL and status that answered, and the registry's message when it sent one. */
export function retiredText(url: string, status: number, body: Uint8Array): string {
  const message = retiredMessage(body);
  const text = `${url} answered ${status} Gone: the repository is retired`;
  return message === null ? text : `${text}; the registry says: ${message}`;
}

/**
 * A 410's body read as a revocation (guide, "Build-level revocation", "A `410`
 * on a tag"): `undefined` unless it is UTF-8 JSON whose `errors[0].detail.revoked`
 * is an object; otherwise that object's advisories, each field made safe, and one
 * that is absent, not a string, empty once made safe or a lone surrogate left
 * `''`. An entry that is not an object, or leaves every field empty, is skipped.
 * A repeated key reads as its last value, and a byte order mark is not JSON.
 * `tests/fixtures/revoked-410/cases.json` is the table all four bindings answer
 * alike.
 */
export function revoked410(body: Uint8Array): readonly Advisory[] | undefined {
  let doc: unknown;
  try {
    doc = JSON.parse(new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(body));
  } catch {
    return undefined;
  }
  const errors = objectOrUndefined(doc)?.['errors'];
  if (!Array.isArray(errors) || errors.length === 0) return undefined;
  const detail = objectOrUndefined(objectOrUndefined(errors[0])?.['detail']);
  const revoked = objectOrUndefined(detail?.['revoked']);
  if (revoked === undefined) return undefined;
  const entries = revoked['advisories'];
  const advisories: Advisory[] = [];
  if (!Array.isArray(entries)) return advisories;
  for (const e of entries) {
    const obj = objectOrUndefined(e);
    if (obj === undefined) continue;
    const field = (key: string): string => {
      const v = obj[key];
      return typeof v === 'string' ? sanitizeRetiredText(v) : '';
    };
    const a: Advisory = {
      id: field('id'),
      url: field('url'),
      class: field('class'),
      summary: field('summary'),
      build: field('build'),
      supersededBy: field('superseded_by'),
      revokedAt: '',
    };
    if (a.id === '' && a.url === '' && a.class === '' && a.summary === '' && a.build === '' && a.supersededBy === '') continue;
    advisories.push(a);
  }
  return advisories;
}

/**
 * One 410 advisory: `<id> <url> (<class>) build <build>, superseded by build
 * <superseded_by>: <summary>`, a part whose field is empty left out with the
 * separator before it, and the first part present taking none.
 */
function advisoryText(a: Advisory): string {
  let s = '';
  const join = (sep: string, part: string): void => {
    if (s !== '') s += sep;
    s += part;
  };
  if (a.id !== '') join(' ', a.id);
  if (a.url !== '') join(' ', a.url);
  if (a.class !== '') join(' ', `(${a.class})`);
  if (a.build !== '') join(' ', `build ${a.build}`);
  if (a.supersededBy !== '') join(', ', `superseded by build ${a.supersededBy}`);
  if (a.summary !== '') join(': ', a.summary);
  return s;
}

/** What follows `<url> answered 410 Gone: ` for a revoked tag. */
export function revoked410Text(advisories: readonly Advisory[]): string {
  const parts = advisories.map(advisoryText).filter((t) => t !== '');
  return parts.length === 0 ? 'revoked, and the registry names no advisory' : `revoked: ${parts.join('; ')}`;
}

/**
 * The error for a 410 (guide §2): `ArtifactRevokedError` when `revocation` is on
 * (generation 2) and the body's `errors[0].detail.revoked` is an object, else a
 * retired repository's `SourceRetiredError`. Both are permanent.
 */
export function goneError(url: string, status: number, body: Uint8Array, revocation: boolean): FetchV1Error {
  if (revocation) {
    const advisories = revoked410(body);
    if (advisories !== undefined) {
      return new ArtifactRevokedError(`chtypes: ${url} answered ${status} Gone: ${revoked410Text(advisories)}`, {
        signed: false,
        manifest: '',
        advisories,
      });
    }
  }
  return new SourceRetiredError(`chtypes: ${retiredText(url, status, body)}`);
}

/** Is `err` a 410's: `SourceRetiredError` or, under generation 2, `ArtifactRevokedError`? Either is permanent, so every loop over bases or candidates throws it at once. */
export function isGone(err: unknown): err is SourceRetiredError | ArtifactRevokedError {
  return err instanceof SourceRetiredError || err instanceof ArtifactRevokedError;
}

/** At most `maxBytes` of a response body. A body that fails part way is no body. */
export function readRetiredBody(stream: http.IncomingMessage, maxBytes: number): Promise<Buffer> {
  return new Promise((resolve) => {
    const chunks: Buffer[] = [];
    let total = 0;
    let done = false;
    const finish = (body: Buffer): void => {
      if (done) return;
      done = true;
      resolve(body);
    };
    stream.on('data', (chunk: Buffer) => {
      if (done) return;
      chunks.push(chunk);
      total += chunk.length;
      if (total >= maxBytes) {
        finish(Buffer.concat(chunks).subarray(0, maxBytes));
        stream.destroy();
      }
    });
    stream.on('end', () => finish(Buffer.concat(chunks)));
    stream.on('error', () => finish(Buffer.alloc(0)));
    stream.on('close', () => finish(Buffer.alloc(0)));
  });
}
