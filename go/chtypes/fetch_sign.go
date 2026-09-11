package chtypes

// fetch_sign.go — the signature (docs/guides/fetch.md §4).
//
// SHA256SUMS.sig is two lines: an "untrusted comment:" line naming the key
// id, and the base64 of a 64-byte ed25519 signature over the exact bytes of
// SHA256SUMS. The key id is the first 16 hex characters of sha256 over the
// raw 32-byte public key. The release key is embedded below; the policy —
// CHTYPES_TRUSTED_KEYS replaces it, CHTYPES_ALLOW_UNSIGNED=1 skips
// verification with one loud warning — is the same in all four SDKs.

import (
	"bufio"
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"os"
	"strings"
)

// ReleasePublicKeyHex is the raw 32-byte ed25519 public key the chtypes
// artifact releases are signed with, as docs/guides/fetch.md §4 publishes it.
// The private half is held offline and never leaves the release pipeline.
const ReleasePublicKeyHex = "fdb5f06a8d4c9918d049a5f1748fa2e3b3238c3f2000986d5bb9e31beff778fc"

// ReleaseKeyID is KeyID(ReleasePublicKey()) — the id the signature file's
// comment line names.
const ReleaseKeyID = "deb275922dbff76e"

// ReleasePublicKey returns the embedded release key.
func ReleasePublicKey() ed25519.PublicKey {
	b, err := hex.DecodeString(ReleasePublicKeyHex)
	if err != nil || len(b) != ed25519.PublicKeySize {
		panic("chtypes: embedded release key is malformed") // a build-time constant; cannot happen
	}
	return ed25519.PublicKey(b)
}

// KeyID is the §4 key id: the first 16 hex characters of sha256 over the
// raw public key.
func KeyID(pub ed25519.PublicKey) string {
	sum := sha256.Sum256(pub)
	return hex.EncodeToString(sum[:8])
}

// ParseTrustedKeys reads the CHTYPES_TRUSTED_KEYS spelling — raw 32-byte
// public keys as hex, comma-separated, whitespace ignored — and refuses
// anything that is not exactly that: a trust list that is silently shorter
// than the operator wrote is a policy hole.
func ParseTrustedKeys(spec string) ([]ed25519.PublicKey, error) {
	var out []ed25519.PublicKey
	for _, part := range strings.Split(spec, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		b, err := hex.DecodeString(part)
		if err != nil {
			return nil, fmt.Errorf("chtypes: %s: %q is not hex: %w", envTrustedKeys, part, err)
		}
		if len(b) != ed25519.PublicKeySize {
			return nil, fmt.Errorf("chtypes: %s: %q is %d bytes, an ed25519 public key is %d", envTrustedKeys, part, len(b), ed25519.PublicKeySize)
		}
		out = append(out, ed25519.PublicKey(b))
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("chtypes: %s is set but names no key", envTrustedKeys)
	}
	return out, nil
}

// trustedKeys resolves the trust list for one fetch: the keys the caller
// passed, else CHTYPES_TRUSTED_KEYS (which REPLACES the embedded key), else
// the embedded release key. The second value names the origin for messages.
func trustedKeys(explicit []ed25519.PublicKey) ([]ed25519.PublicKey, string, error) {
	if len(explicit) > 0 {
		return explicit, "TrustedKeys option", nil
	}
	if env := strings.TrimSpace(os.Getenv(envTrustedKeys)); env != "" {
		keys, err := ParseTrustedKeys(env)
		if err != nil {
			return nil, "", err
		}
		return keys, envTrustedKeys, nil
	}
	return []ed25519.PublicKey{ReleasePublicKey()}, "the embedded release key", nil
}

// ParseSignatureFile reads the two-line SHA256SUMS.sig format and returns
// the comment line (without its prefix) and the 64-byte signature.
func ParseSignatureFile(b []byte) (comment string, sig []byte, err error) {
	sc := bufio.NewScanner(bytes.NewReader(b))
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" {
			continue
		}
		if strings.HasPrefix(line, "untrusted comment:") {
			comment = strings.TrimSpace(strings.TrimPrefix(line, "untrusted comment:"))
			continue
		}
		raw, derr := base64.StdEncoding.DecodeString(line)
		if derr != nil {
			raw, derr = base64.RawStdEncoding.DecodeString(line)
		}
		if derr != nil {
			return comment, nil, fmt.Errorf("signature line is not base64: %w", derr)
		}
		if len(raw) != ed25519.SignatureSize {
			return comment, nil, fmt.Errorf("signature decodes to %d bytes, an ed25519 signature is %d", len(raw), ed25519.SignatureSize)
		}
		return comment, raw, nil
	}
	return comment, nil, fmt.Errorf("signature file has no signature line")
}

// VerifySignature checks sigFile (SHA256SUMS.sig) over message (the exact
// bytes of SHA256SUMS) against every trusted key and returns the key that
// verified. It never picks a key from the comment line — that line is
// untrusted by name.
func VerifySignature(message, sigFile []byte, keys []ed25519.PublicKey) (ed25519.PublicKey, error) {
	_, sig, err := ParseSignatureFile(sigFile)
	if err != nil {
		return nil, err
	}
	for _, k := range keys {
		if ed25519.Verify(k, message, sig) {
			return k, nil
		}
	}
	ids := make([]string, 0, len(keys))
	for _, k := range keys {
		ids = append(ids, KeyID(k))
	}
	return nil, fmt.Errorf("signature does not verify under any trusted key (%s)", strings.Join(ids, ", "))
}
