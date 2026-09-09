//! `docs/fetch.md` §4: the release key, the trust policy, the signature file
//! and the ed25519 check — plus the sha256 helpers the whole chain hashes with.

use std::io::Read;
use std::path::Path;

use base64::Engine;
use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use sha2::{Digest, Sha256};

use crate::error::{Error, Result};

/// The release public key, raw, hex — `docs/fetch.md` §4. Every SDK embeds
/// this constant; `CHTYPES_TRUSTED_KEYS` replaces it.
pub const RELEASE_PUBLIC_KEY_HEX: &str =
    "fdb5f06a8d4c9918d049a5f1748fa2e3b3238c3f2000986d5bb9e31beff778fc";

/// The release public key, raw 32 bytes ([`RELEASE_PUBLIC_KEY_HEX`] decoded).
pub const RELEASE_PUBLIC_KEY: [u8; 32] = hex32(RELEASE_PUBLIC_KEY_HEX);

/// The release key's id: the first 16 hex characters of sha256 over the raw
/// 32-byte public key. Named in `SHA256SUMS.sig`'s comment line.
pub const RELEASE_KEY_ID: &str = "deb275922dbff76e";

/// `CHTYPES_TRUSTED_KEYS=<hex>[,<hex>…]` **replaces** the embedded key list —
/// for a mirror or a custom registry signed by someone else.
pub const TRUSTED_KEYS_ENV: &str = "CHTYPES_TRUSTED_KEYS";

/// `CHTYPES_ALLOW_UNSIGNED=1` skips the signature step and prints one loud
/// warning naming the source. Never the default; never silent.
pub const ALLOW_UNSIGNED_ENV: &str = "CHTYPES_ALLOW_UNSIGNED";

/// Decode 64 hex characters into 32 bytes at compile time, so the key is a
/// byte constant and not a runtime parse that could fail.
const fn hex32(s: &str) -> [u8; 32] {
    let b = s.as_bytes();
    assert!(
        b.len() == 64,
        "a raw ed25519 public key is 64 hex characters"
    );
    let mut out = [0u8; 32];
    let mut i = 0;
    while i < 32 {
        out[i] = (hex_nibble(b[2 * i]) << 4) | hex_nibble(b[2 * i + 1]);
        i += 1;
    }
    out
}

const fn hex_nibble(c: u8) -> u8 {
    match c {
        b'0'..=b'9' => c - b'0',
        b'a'..=b'f' => c - b'a' + 10,
        b'A'..=b'F' => c - b'A' + 10,
        _ => panic!("not a hex digit"),
    }
}

/// Parse a raw public key spelled as 64 hex characters.
pub fn parse_key_hex(hex: &str) -> Result<[u8; 32]> {
    let hex = hex.trim();
    let bad = || Error::Fetch {
        message: format!("{hex:?} is not a raw ed25519 public key (64 hex characters expected)"),
    };
    if hex.len() != 64 || !hex.bytes().all(|c| c.is_ascii_hexdigit()) {
        return Err(bad());
    }
    let mut out = [0u8; 32];
    for (i, byte) in out.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).map_err(|_| bad())?;
    }
    Ok(out)
}

/// The key id of a raw public key: the first 16 hex characters of its sha256.
pub fn key_id(key: &[u8; 32]) -> String {
    sha256_hex(key)[..16].to_string()
}

/// Which keys sign a trusted release, and whether an unsigned one is let
/// through (`docs/fetch.md` §4).
#[derive(Debug, Clone)]
pub struct TrustPolicy {
    keys: Vec<[u8; 32]>,
    allow_unsigned: bool,
}

impl TrustPolicy {
    /// The embedded release key only; unsigned releases refused.
    pub fn embedded() -> TrustPolicy {
        TrustPolicy {
            keys: vec![RELEASE_PUBLIC_KEY],
            allow_unsigned: false,
        }
    }

    /// The environment's policy: `CHTYPES_TRUSTED_KEYS` replaces the embedded
    /// list when set, `CHTYPES_ALLOW_UNSIGNED=1` skips the signature step.
    ///
    /// # Errors
    ///
    /// [`Error::Fetch`] when `CHTYPES_TRUSTED_KEYS` is set and one of its
    /// entries is not 64 hex characters, or it is set and empty.
    pub fn from_env() -> Result<TrustPolicy> {
        let keys = match std::env::var(TRUSTED_KEYS_ENV) {
            Ok(list) => Some(parse_key_list(&list)?),
            Err(_) => None,
        };
        let allow_unsigned = std::env::var(ALLOW_UNSIGNED_ENV)
            .map(|v| v == "1")
            .unwrap_or(false);
        Ok(TrustPolicy::build(keys, allow_unsigned))
    }

    /// An explicit policy: `keys` as raw hex (`None` = the embedded key).
    pub fn new(keys: Option<&[String]>, allow_unsigned: bool) -> Result<TrustPolicy> {
        let keys = match keys {
            Some(list) => {
                let mut out = Vec::with_capacity(list.len());
                for k in list {
                    out.push(parse_key_hex(k)?);
                }
                if out.is_empty() {
                    return Err(Error::Fetch {
                        message: "the trusted key list is empty".into(),
                    });
                }
                Some(out)
            }
            None => None,
        };
        Ok(TrustPolicy::build(keys, allow_unsigned))
    }

    fn build(keys: Option<Vec<[u8; 32]>>, allow_unsigned: bool) -> TrustPolicy {
        TrustPolicy {
            keys: keys.unwrap_or_else(|| vec![RELEASE_PUBLIC_KEY]),
            allow_unsigned,
        }
    }

    /// Whether the signature step is skipped.
    pub fn allow_unsigned(&self) -> bool {
        self.allow_unsigned
    }

    /// The ids of the trusted keys, for messages.
    pub fn key_ids(&self) -> Vec<String> {
        self.keys.iter().map(key_id).collect()
    }

    /// Verify `sig_file` (the two-line `SHA256SUMS.sig`) over the exact bytes
    /// of `sums` with every trusted key. `Ok(key id)` names the key that
    /// verified; `Err(reason)` says why none did, in words a user can act on.
    pub fn verify(&self, sums: &[u8], sig_file: &[u8]) -> std::result::Result<String, String> {
        let (signature, comment) = parse_signature_file(sig_file)?;
        for key in &self.keys {
            if verify_signature(key, sums, &signature) {
                return Ok(key_id(key));
            }
        }
        let comment = comment
            .map(|c| format!(" (the file's own comment says: {c:?})"))
            .unwrap_or_default();
        Err(format!(
            "the signature verifies under none of the trusted keys [{}]{comment}",
            self.key_ids().join(", ")
        ))
    }
}

fn parse_key_list(list: &str) -> Result<Vec<[u8; 32]>> {
    let mut out = Vec::new();
    for item in list.split(',') {
        let item = item.trim();
        if item.is_empty() {
            continue;
        }
        out.push(parse_key_hex(item)?);
    }
    if out.is_empty() {
        return Err(Error::Fetch {
            message: format!("${TRUSTED_KEYS_ENV} is set but names no key"),
        });
    }
    Ok(out)
}

/// Parse `SHA256SUMS.sig` — `docs/fetch.md` §4:
///
/// ```text
/// untrusted comment: chtypes artifacts, ed25519 key deb275922dbff76e
/// <base64 of the 64-byte ed25519 signature over the bytes of SHA256SUMS>
/// ```
///
/// Returns the signature and the comment (which is untrusted and only ever
/// quoted back in a message — the key is chosen by verifying, never by the
/// comment's say-so).
pub fn parse_signature_file(
    bytes: &[u8],
) -> std::result::Result<([u8; 64], Option<String>), String> {
    let text = std::str::from_utf8(bytes).map_err(|_| "SHA256SUMS.sig is not UTF-8".to_string())?;
    let mut lines = text.lines().map(str::trim).filter(|l| !l.is_empty());
    let first = lines.next().ok_or("SHA256SUMS.sig is empty")?;
    let (comment, sig_b64) = match first.strip_prefix("untrusted comment:") {
        Some(c) => (
            Some(c.trim().to_string()),
            lines
                .next()
                .ok_or("SHA256SUMS.sig has a comment line and no signature line")?,
        ),
        None => (None, first),
    };
    if lines.next().is_some() {
        return Err("SHA256SUMS.sig has more than two lines".into());
    }
    let raw = base64::engine::general_purpose::STANDARD
        .decode(sig_b64)
        .map_err(|e| format!("SHA256SUMS.sig's signature line is not base64: {e}"))?;
    let signature: [u8; 64] = raw.as_slice().try_into().map_err(|_| {
        format!(
            "SHA256SUMS.sig decodes to {} bytes, an ed25519 signature is 64",
            raw.len()
        )
    })?;
    Ok((signature, comment))
}

/// One ed25519 verification of `message` under `key` — RFC 8032, the same
/// check every SDK's standard library performs.
pub fn verify_signature(key: &[u8; 32], message: &[u8], signature: &[u8; 64]) -> bool {
    let Ok(key) = VerifyingKey::from_bytes(key) else {
        return false;
    };
    key.verify(message, &Signature::from_bytes(signature))
        .is_ok()
}

/// Lowercase hex sha256 of `bytes`.
pub fn sha256_hex(bytes: &[u8]) -> String {
    hex(&Sha256::digest(bytes))
}

/// Lowercase hex sha256 of a file, streamed.
pub fn sha256_file(path: &Path) -> std::io::Result<String> {
    let mut file = std::fs::File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hex(&hasher.finalize()))
}

/// A streaming sha256 for the download path: hash what is written.
pub(crate) struct Hashing<W> {
    inner: W,
    hasher: Sha256,
    pub(crate) bytes: u64,
}

impl<W: std::io::Write> Hashing<W> {
    pub(crate) fn new(inner: W) -> Hashing<W> {
        Hashing {
            inner,
            hasher: Sha256::new(),
            bytes: 0,
        }
    }

    pub(crate) fn finish(mut self) -> std::io::Result<(W, String)> {
        self.inner.flush()?;
        let digest = hex(&self.hasher.finalize());
        Ok((self.inner, digest))
    }
}

impl<W: std::io::Write> std::io::Write for Hashing<W> {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        let n = self.inner.write(buf)?;
        self.hasher.update(&buf[..n]);
        self.bytes += n as u64;
        Ok(n)
    }

    fn flush(&mut self) -> std::io::Result<()> {
        self.inner.flush()
    }
}

pub(crate) fn hex(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        use std::fmt::Write;
        let _ = write!(s, "{b:02x}");
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    /// docs/fetch.md §4, the reference vector: "hello\n" signs under the
    /// release key to the signature below (openssl, -rawin); flipping one
    /// byte of the message fails.
    const VECTOR_SIG_HEX: &str = "0fee686f7ed7c64b86a7dce0ffd66b15d1504178153c3b0cc118e2c9456afa6d3e2e55019eca8f75e44ab507d65b0714523e92c7f92452821930691212e76c04";

    fn vector_signature() -> [u8; 64] {
        let mut out = [0u8; 64];
        for (i, byte) in out.iter_mut().enumerate() {
            *byte = u8::from_str_radix(&VECTOR_SIG_HEX[2 * i..2 * i + 2], 16).unwrap();
        }
        out
    }

    #[test]
    fn the_embedded_key_has_the_spec_s_id() {
        assert_eq!(key_id(&RELEASE_PUBLIC_KEY), RELEASE_KEY_ID);
        assert_eq!(hex(&RELEASE_PUBLIC_KEY), RELEASE_PUBLIC_KEY_HEX);
        assert_eq!(
            parse_key_hex(RELEASE_PUBLIC_KEY_HEX).unwrap(),
            RELEASE_PUBLIC_KEY
        );
    }

    #[test]
    fn the_reference_vector_verifies_and_a_flipped_byte_does_not() {
        let sig = vector_signature();
        assert!(verify_signature(&RELEASE_PUBLIC_KEY, b"hello\n", &sig));
        assert!(!verify_signature(&RELEASE_PUBLIC_KEY, b"hellp\n", &sig));
        assert!(!verify_signature(&RELEASE_PUBLIC_KEY, b"hello", &sig));
        let mut bad = sig;
        bad[10] ^= 1;
        assert!(!verify_signature(&RELEASE_PUBLIC_KEY, b"hello\n", &bad));
        let mut other_key = RELEASE_PUBLIC_KEY;
        other_key[0] ^= 1;
        assert!(!verify_signature(&other_key, b"hello\n", &sig));
    }

    #[test]
    fn the_signature_file_shape_is_two_lines_and_the_comment_is_not_trusted() {
        let b64 = base64::engine::general_purpose::STANDARD.encode(vector_signature());
        let file =
            format!("untrusted comment: chtypes artifacts, ed25519 key {RELEASE_KEY_ID}\n{b64}\n");
        let policy = TrustPolicy::embedded();
        assert_eq!(
            policy.verify(b"hello\n", file.as_bytes()).unwrap(),
            RELEASE_KEY_ID
        );

        // The comment naming a different key changes nothing: verification,
        // not the comment, picks the key.
        let lying = format!("untrusted comment: ed25519 key 0000000000000000\n{b64}\n");
        assert_eq!(
            policy.verify(b"hello\n", lying.as_bytes()).unwrap(),
            RELEASE_KEY_ID
        );

        // Bare signature line, no comment: accepted.
        assert_eq!(
            policy
                .verify(b"hello\n", format!("{b64}\n").as_bytes())
                .unwrap(),
            RELEASE_KEY_ID
        );

        // Wrong message under a well-formed file: refused, naming the key ids.
        let err = policy.verify(b"goodbye\n", file.as_bytes()).unwrap_err();
        assert!(err.contains(RELEASE_KEY_ID), "{err}");

        // Malformed files are refused with a reason, never a panic.
        assert!(parse_signature_file(b"").is_err());
        assert!(parse_signature_file(b"untrusted comment: x\n").is_err());
        assert!(parse_signature_file(b"untrusted comment: x\nnot base64!\n").is_err());
        assert!(parse_signature_file(b"untrusted comment: x\nAAAA\n").is_err());
        assert!(parse_signature_file(format!("{b64}\n{b64}\n{b64}\n").as_bytes()).is_err());
    }

    #[test]
    fn a_trusted_key_list_replaces_the_embedded_key() {
        let mut other = RELEASE_PUBLIC_KEY;
        other[0] ^= 1;
        let policy = TrustPolicy::new(Some(&[hex(&other)]), false).unwrap();
        assert_eq!(policy.key_ids(), vec![key_id(&other)]);
        let b64 = base64::engine::general_purpose::STANDARD.encode(vector_signature());
        // The release key is no longer trusted, so the reference vector fails.
        assert!(
            policy
                .verify(b"hello\n", format!("{b64}\n").as_bytes())
                .is_err()
        );

        assert!(TrustPolicy::new(Some(&["zz".to_string()]), false).is_err());
        assert!(TrustPolicy::new(Some(&[]), false).is_err());
        assert!(parse_key_list("").is_err());
        assert_eq!(
            parse_key_list(&format!(" {RELEASE_PUBLIC_KEY_HEX} , {} ", hex(&other))).unwrap(),
            vec![RELEASE_PUBLIC_KEY, other]
        );
    }

    #[test]
    fn sha256_helpers_agree_with_the_known_answer() {
        assert_eq!(
            sha256_hex(b"hello\n"),
            "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"
        );
        let dir = std::env::temp_dir().join(format!("chtypes-rs-sha-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("hello");
        std::fs::write(&path, b"hello\n").unwrap();
        assert_eq!(sha256_file(&path).unwrap(), sha256_hex(b"hello\n"));
        let mut w = Hashing::new(Vec::new());
        std::io::Write::write_all(&mut w, b"hel").unwrap();
        std::io::Write::write_all(&mut w, b"lo\n").unwrap();
        assert_eq!(w.bytes, 6);
        let (buf, digest) = w.finish().unwrap();
        assert_eq!(buf, b"hello\n");
        assert_eq!(digest, sha256_hex(b"hello\n"));
        std::fs::remove_dir_all(&dir).ok();
    }
}
