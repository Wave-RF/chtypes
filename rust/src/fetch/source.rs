//! `docs/guides/fetch.md` §2: where a release comes from — the artifacts host under
//! a tag, any other HTTP(S) base, a `file://` path, or a plain directory.

use std::io::Read;
use std::path::PathBuf;
use std::time::Duration;

use crate::error::{Error, Result};

/// The artifacts host, `CHTYPES_ARTIFACTS_URL`'s default.
pub const DEFAULT_ARTIFACTS_URL: &str = "https://artifacts.wavehouse.dev";

/// The rolling release tag under the artifacts host.
pub const DEFAULT_TAG: &str = "artifacts";

/// Overrides the artifacts host.
pub const ARTIFACTS_URL_ENV: &str = "CHTYPES_ARTIFACTS_URL";

/// An optional bearer token sent to an HTTP source (the delivery Worker's
/// download tokens); `scripts/fetch.sh` sends the same header.
pub const DOWNLOAD_TOKEN_ENV: &str = "CHTYPES_DOWNLOAD_TOKEN";

/// Small release files (`index.json`, `SHA256SUMS`, `SHA256SUMS.sig`) are
/// read into memory; anything past this is not a release file.
const SMALL_FILE_LIMIT: u64 = 8 << 20;

/// A release file opened for streaming, with its length when the source
/// knows it.
pub(crate) type Opened = (Box<dyn Read>, Option<u64>);

/// One place release files are read from.
pub(crate) enum Source {
    Http {
        base: String,
        agent: ureq::Agent,
        token: Option<String>,
        offline: bool,
    },
    Dir {
        base: PathBuf,
        shown: String,
    },
}

impl Source {
    /// Resolve the source from `--url` / `--tag` / the environment.
    pub(crate) fn resolve(url: Option<&str>, tag: Option<&str>, offline: bool) -> Result<Source> {
        if url.is_some() && tag.is_some() {
            return Err(Error::Fetch {
                message: "--url names a full base; --tag selects a release on the artifacts \
                          host — pass one"
                    .into(),
            });
        }
        let base = match url {
            Some(u) => u.trim_end_matches('/').to_string(),
            None => {
                let host = std::env::var(ARTIFACTS_URL_ENV)
                    .ok()
                    .filter(|v| !v.trim().is_empty())
                    .unwrap_or_else(|| DEFAULT_ARTIFACTS_URL.to_string());
                format!(
                    "{}/{}",
                    host.trim_end_matches('/'),
                    tag.unwrap_or(DEFAULT_TAG)
                )
            }
        };
        if let Some(path) = base.strip_prefix("file://") {
            return Ok(Source::Dir {
                base: PathBuf::from(path),
                shown: base.clone(),
            });
        }
        if base.starts_with("http://") || base.starts_with("https://") {
            let agent = ureq::Agent::config_builder()
                .http_status_as_error(false)
                .timeout_connect(Some(Duration::from_secs(20)))
                .timeout_recv_response(Some(Duration::from_secs(60)))
                .timeout_recv_body(Some(Duration::from_secs(3600)))
                .user_agent(concat!("chtypes/", env!("CARGO_PKG_VERSION"), " (rust)"))
                .build()
                .new_agent();
            let token = std::env::var(DOWNLOAD_TOKEN_ENV)
                .ok()
                .filter(|t| !t.is_empty());
            return Ok(Source::Http {
                base,
                agent,
                token,
                offline,
            });
        }
        Ok(Source::Dir {
            shown: base.clone(),
            base: PathBuf::from(base),
        })
    }

    /// The source as the user spelled it, for messages.
    pub(crate) fn describe(&self) -> &str {
        match self {
            Source::Http { base, .. } => base,
            Source::Dir { shown, .. } => shown,
        }
    }

    fn unreachable(&self, message: String) -> Error {
        Error::SourceUnreachable {
            origin: self.describe().to_string(),
            message,
        }
    }

    /// Open one release file for streaming: `Ok(None)` when the source has no
    /// such file, the reader and (when known) its length otherwise.
    ///
    /// # Errors
    ///
    /// [`Error::SourceUnreachable`] — offline, a transport failure, or an HTTP
    /// status other than 200/404.
    pub(crate) fn open(&self, name: &str) -> Result<Option<Opened>> {
        match self {
            Source::Dir { base, .. } => {
                let path = base.join(name);
                match std::fs::File::open(&path) {
                    Ok(file) => {
                        let len = file.metadata().ok().map(|m| m.len());
                        Ok(Some((Box::new(file), len)))
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
                    Err(e) => Err(self.unreachable(format!("{}: {e}", path.display()))),
                }
            }
            Source::Http {
                base,
                agent,
                token,
                offline,
            } => {
                if *offline {
                    return Err(self.unreachable(format!("offline — {name} was not requested")));
                }
                let url = format!("{base}/{name}");
                let mut request = agent.get(&url);
                if let Some(t) = token {
                    request = request.header("Authorization", format!("Bearer {t}"));
                }
                let response = request
                    .call()
                    .map_err(|e| self.unreachable(format!("GET {url}: {e}")))?;
                match response.status().as_u16() {
                    200 => {
                        let len = response.body().content_length();
                        Ok(Some((Box::new(response.into_body().into_reader()), len)))
                    }
                    404 | 410 => Ok(None),
                    status => Err(self.unreachable(format!("GET {url}: HTTP {status}"))),
                }
            }
        }
    }

    /// Read one small release file entirely; `Ok(None)` when absent.
    pub(crate) fn read(&self, name: &str) -> Result<Option<Vec<u8>>> {
        let Some((reader, len)) = self.open(name)? else {
            return Ok(None);
        };
        if let Some(len) = len {
            if len > SMALL_FILE_LIMIT {
                return Err(self.unreachable(format!(
                    "{name} is {len} bytes, which is not a release file"
                )));
            }
        }
        let mut buf = Vec::new();
        reader
            .take(SMALL_FILE_LIMIT + 1)
            .read_to_end(&mut buf)
            .map_err(|e| self.unreachable(format!("reading {name}: {e}")))?;
        if buf.len() as u64 > SMALL_FILE_LIMIT {
            return Err(self.unreachable(format!(
                "{name} exceeds {SMALL_FILE_LIMIT} bytes, which is not a release file"
            )));
        }
        Ok(Some(buf))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_url_and_a_tag_are_one_choice() {
        assert!(Source::resolve(Some("file:///x"), Some("v1"), false).is_err());
    }

    #[test]
    fn file_urls_and_plain_directories_are_local_sources() {
        match Source::resolve(Some("file:///tmp/rel"), None, false).unwrap() {
            Source::Dir { base, shown } => {
                assert_eq!(base, PathBuf::from("/tmp/rel"));
                assert_eq!(shown, "file:///tmp/rel");
            }
            _ => panic!("file:// must be a directory source"),
        }
        match Source::resolve(Some("/tmp/rel/"), None, false).unwrap() {
            Source::Dir { base, .. } => assert_eq!(base, PathBuf::from("/tmp/rel")),
            _ => panic!("a plain path must be a directory source"),
        }
        match Source::resolve(Some("https://mirror.example/x/"), None, true).unwrap() {
            Source::Http { base, offline, .. } => {
                assert_eq!(base, "https://mirror.example/x");
                assert!(offline);
            }
            _ => panic!("https must be an HTTP source"),
        }
    }

    #[test]
    fn offline_never_touches_the_network() {
        // A closed port would answer "connection refused"; offline must answer
        // before any connection is attempted.
        let src = Source::resolve(Some("http://127.0.0.1:1/rel"), None, true).unwrap();
        let err = src.read("index.json").unwrap_err();
        assert!(matches!(err, Error::SourceUnreachable { .. }), "{err:?}");
        assert!(err.to_string().contains("offline"), "{err}");
    }

    #[test]
    fn a_missing_file_in_a_directory_source_is_none_not_an_error() {
        let dir = std::env::temp_dir().join(format!("chtypes-rs-src-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("SHA256SUMS"), b"abc  x\n").unwrap();
        let src = Source::resolve(Some(dir.to_str().unwrap()), None, false).unwrap();
        assert_eq!(src.read("SHA256SUMS").unwrap().unwrap(), b"abc  x\n");
        assert!(src.read("SHA256SUMS.sig").unwrap().is_none());
        std::fs::remove_dir_all(&dir).ok();
    }
}
