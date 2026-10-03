/**
 * Trust (`docs/guides/fetch-v1.md` §4): DSSE PAE against the published test
 * vector, ed25519 verify/refuse, the statement content checks, and the
 * duplicate-key refusal `../../src/json.ts`'s reader makes possible. None of
 * this needs the fixtures tree (`CHTYPES_V1_CONFORMANCE`) — it exercises the
 * hand-written verifier directly, the same primitive the conformance suite
 * will drive end to end once lane 0B's fixtures exist.
 */

import type { KeyObject } from 'node:crypto';
import { generateKeyPairSync, sign as cryptoSign } from 'node:crypto';
import { describe, expect, it } from 'vitest';
import {
  checkArtifactStatement,
  checkGenericStatement,
  dssePAE,
  parseJsonNoDuplicates,
  parseStatement,
  verifyBundleSignature,
  verifyEd25519,
  versionWithinRequest,
} from '../../src/ocifetch/dsse.js';
import { ArtifactCorruptError } from '../../src/ocifetch/errors.js';
import { DSSE_PAYLOAD_TYPE } from '../../src/ocifetch/constants.gen.js';
import type { TrustedKey } from '../../src/ocifetch/types.js';

function freshKey(): { trusted: TrustedKey; privateKey: KeyObject } {
  const { publicKey, privateKey }: { publicKey: KeyObject; privateKey: KeyObject } = generateKeyPairSync('ed25519');
  const jwk = publicKey.export({ format: 'jwk' }) as { x: string };
  const hex = Buffer.from(jwk.x, 'base64url').toString('hex');
  return { trusted: { keyid: hex.slice(0, 16), ed25519Hex: hex }, privateKey };
}

function bundleFor(statement: unknown, privateKey: KeyObject, keyid: string): Buffer {
  const payload = Buffer.from(JSON.stringify(statement), 'utf8');
  const pae = dssePAE(DSSE_PAYLOAD_TYPE, payload);
  const sig = cryptoSign(null, pae, privateKey);
  const bundle = {
    mediaType: 'application/vnd.dev.sigstore.bundle.v0.3+json',
    verificationMaterial: { publicKey: { hint: keyid } },
    dsseEnvelope: {
      payload: payload.toString('base64'),
      payloadType: DSSE_PAYLOAD_TYPE,
      signatures: [{ sig: sig.toString('base64'), keyid }],
    },
  };
  return Buffer.from(JSON.stringify(bundle), 'utf8');
}

const SUBJECT_DIGEST = 'a'.repeat(64);

function statementFor(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    _type: 'https://in-toto.io/Statement/v1',
    subject: [{ name: 'chtypes-26.8.15.10-linux-arm64.tar.zst', digest: { sha256: SUBJECT_DIGEST } }],
    predicateType: 'https://artifacts.wavehouse.dev/spec/artifact/v1',
    predicate: {
      abi: 1,
      abi_fingerprint: `sha256:${'b'.repeat(64)}`,
      clickhouse_version: '26.8.15.10',
      channel: 'lts',
      clickhouse_minor: '26.8',
      clickhouse_commit: 'c'.repeat(40),
      os: 'linux',
      arch: 'arm64',
      build: '20261001.183455',
      core_commit: 'd'.repeat(40),
      inputs_sha256: 'e'.repeat(64),
      library: 'libchtypes.so',
      library_sha256: 'f'.repeat(64),
      library_bytes: 241000000,
      ...overrides,
    },
  };
}

describe('dssePAE', () => {
  it('matches the published DSSE test vector', () => {
    const pae = dssePAE('http://example.com/HelloWorld', Buffer.from('hello world', 'utf8'));
    expect(pae.toString('utf8')).toBe('DSSEv1 29 http://example.com/HelloWorld 11 hello world');
  });
});

describe('verifyEd25519', () => {
  it('verifies a real signature and refuses a tampered message or wrong key', () => {
    const { trusted, privateKey } = freshKey();
    const message = Buffer.from('chtypes trust check');
    const sig = cryptoSign(null, message, privateKey);
    expect(verifyEd25519(trusted.ed25519Hex, message, sig)).toBe(true);
    expect(verifyEd25519(trusted.ed25519Hex, Buffer.from('tampered'), sig)).toBe(false);
    const other = freshKey();
    expect(verifyEd25519(other.trusted.ed25519Hex, message, sig)).toBe(false);
  });
});

describe('verifyBundleSignature', () => {
  it('verifies against a trusted key and returns the payload', () => {
    const { trusted, privateKey } = freshKey();
    const statement = statementFor();
    const bundleBytes = bundleFor(statement, privateKey, trusted.keyid);
    const result = verifyBundleSignature(bundleBytes, [trusted]);
    expect(result).toBeDefined();
    expect(result?.signedBy).toBe(trusted.keyid);
    expect(JSON.parse(result?.payload.toString('utf8') ?? '{}')).toEqual(statement);
  });

  it('returns undefined (never throws) for a bundle no trusted key signed — the caller tries the next referrer', () => {
    const { privateKey } = freshKey();
    const untrusted = freshKey();
    const statement = statementFor();
    const bundleBytes = bundleFor(statement, privateKey, 'whoever');
    const result = verifyBundleSignature(bundleBytes, [untrusted.trusted]);
    expect(result).toBeUndefined();
  });

  it('returns undefined for unparseable bundle JSON rather than throwing', () => {
    const { trusted } = freshKey();
    const result = verifyBundleSignature(Buffer.from('not json'), [trusted]);
    expect(result).toBeUndefined();
  });

  it('returns undefined when the envelope payloadType is not the in-toto one', () => {
    const { trusted, privateKey } = freshKey();
    const payload = Buffer.from(JSON.stringify(statementFor()), 'utf8');
    const wrongType = 'application/something-else';
    const pae = dssePAE(wrongType, payload);
    const sig = cryptoSign(null, pae, privateKey);
    const bundle = {
      dsseEnvelope: {
        payload: payload.toString('base64'),
        payloadType: wrongType,
        signatures: [{ sig: sig.toString('base64'), keyid: trusted.keyid }],
      },
    };
    const result = verifyBundleSignature(Buffer.from(JSON.stringify(bundle)), [trusted]);
    expect(result).toBeUndefined();
  });
});

describe('parseJsonNoDuplicates', () => {
  it('parses an ordinary object', () => {
    expect(parseJsonNoDuplicates(Buffer.from('{"a":1,"b":"two"}'))).toEqual({ a: 1, b: 'two' });
  });

  it('refuses a duplicate key at the top level', () => {
    expect(() => parseJsonNoDuplicates(Buffer.from('{"a":1,"a":2}'))).toThrow(ArtifactCorruptError);
  });

  it('refuses a duplicate key nested inside the predicate', () => {
    const bytes = JSON.stringify(statementFor()).replace('"abi":1,', '"abi":1,"abi":2,');
    expect(() => parseJsonNoDuplicates(Buffer.from(bytes))).toThrow(ArtifactCorruptError);
  });

  it('refuses bytes that are not a single JSON value', () => {
    expect(() => parseJsonNoDuplicates(Buffer.from('not json'))).toThrow(ArtifactCorruptError);
  });
});

function asBuffer(value: string): Buffer {
  return Buffer.from(value, 'utf8');
}

describe('parseStatement', () => {
  it('parses a well-formed statement', () => {
    const statement = parseStatement(asBuffer(JSON.stringify(statementFor())));
    expect(statement.predicateType).toBe('https://artifacts.wavehouse.dev/spec/artifact/v1');
    expect(statement.subject[0]?.digest.sha256).toBe(SUBJECT_DIGEST);
  });

  it('refuses a statement with no subject', () => {
    const bad = statementFor();
    delete (bad as Record<string, unknown>)['subject'];
    expect(() => parseStatement(asBuffer(JSON.stringify(bad)))).toThrow(ArtifactCorruptError);
  });
});

describe('versionWithinRequest', () => {
  it('an exact four-part request must equal exactly', () => {
    expect(versionWithinRequest('26.8.15.10', '26.8.15.10')).toBe(true);
    expect(versionWithinRequest('26.8.15.11', '26.8.15.10')).toBe(false);
  });

  it('a floating request is a component prefix', () => {
    expect(versionWithinRequest('26.8.15.10', '26.8')).toBe(true);
    expect(versionWithinRequest('26.8.15.10', '26.8.15')).toBe(true);
    expect(versionWithinRequest('26.7.15.10', '26.8')).toBe(false);
  });

  it('a request with more components than the actual version is never within it', () => {
    expect(versionWithinRequest('26.8', '26.8.15.10')).toBe(false);
  });
});

describe('checkArtifactStatement', () => {
  const layerDigest = `sha256:${SUBJECT_DIGEST}`;

  it('accepts a matching statement and returns the predicate', () => {
    const statement = parseStatement(asBuffer(JSON.stringify(statementFor())));
    const predicate = checkArtifactStatement(statement, 'https://artifacts.wavehouse.dev/spec/artifact/v1', layerDigest, {
      os: 'linux',
      arch: 'arm64',
      requestedSpelling: '26.8',
    });
    expect(predicate.clickhouse_version).toBe('26.8.15.10');
  });

  it('refuses predicateType mismatch', () => {
    const statement = parseStatement(asBuffer(JSON.stringify(statementFor())));
    expect(() =>
      checkArtifactStatement(statement, 'https://artifacts.wavehouse.dev/spec/goldens/v1', layerDigest, {
        os: 'linux',
        arch: 'arm64',
        requestedSpelling: '26.8',
      }),
    ).toThrow(ArtifactCorruptError);
  });

  it('refuses a subject digest that does not match the manifest layer', () => {
    const statement = parseStatement(asBuffer(JSON.stringify(statementFor())));
    expect(() =>
      checkArtifactStatement(statement, 'https://artifacts.wavehouse.dev/spec/artifact/v1', `sha256:${'0'.repeat(64)}`, {
        os: 'linux',
        arch: 'arm64',
        requestedSpelling: '26.8',
      }),
    ).toThrow(ArtifactCorruptError);
  });

  it('refuses a wrong platform', () => {
    const statement = parseStatement(asBuffer(JSON.stringify(statementFor())));
    expect(() =>
      checkArtifactStatement(statement, 'https://artifacts.wavehouse.dev/spec/artifact/v1', layerDigest, {
        os: 'darwin',
        arch: 'arm64',
        requestedSpelling: '26.8',
      }),
    ).toThrow(ArtifactCorruptError);
  });

  it('refuses a version outside the request', () => {
    const statement = parseStatement(asBuffer(JSON.stringify(statementFor())));
    expect(() =>
      checkArtifactStatement(statement, 'https://artifacts.wavehouse.dev/spec/artifact/v1', layerDigest, {
        os: 'linux',
        arch: 'arm64',
        requestedSpelling: '26.9',
      }),
    ).toThrow(ArtifactCorruptError);
  });

  it('refuses abi_revision in place of abi (never readable as v0 revision 1)', () => {
    const bad = statementFor({ abi: undefined, abi_revision: 1 });
    delete (bad['predicate'] as Record<string, unknown>)['abi'];
    const statement = parseStatement(asBuffer(JSON.stringify(bad)));
    expect(() =>
      checkArtifactStatement(statement, 'https://artifacts.wavehouse.dev/spec/artifact/v1', layerDigest, {
        os: 'linux',
        arch: 'arm64',
        requestedSpelling: '26.8',
      }),
    ).toThrow(ArtifactCorruptError);
  });
});

describe('checkGenericStatement', () => {
  it('checks only predicateType and subject digest', () => {
    const statement = parseStatement(asBuffer(JSON.stringify(statementFor({ library: undefined }))));
    expect(() =>
      checkGenericStatement(statement, 'https://artifacts.wavehouse.dev/spec/artifact/v1', `sha256:${SUBJECT_DIGEST}`),
    ).not.toThrow();
    expect(() => checkGenericStatement(statement, 'https://artifacts.wavehouse.dev/spec/goldens/v1', `sha256:${SUBJECT_DIGEST}`)).toThrow(
      ArtifactCorruptError,
    );
  });
});
