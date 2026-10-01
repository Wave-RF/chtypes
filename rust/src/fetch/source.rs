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
        offline: bool,
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
                offline,
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
            offline,
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
        self.unreachable_retry(message, false, None)
    }

    /// The same shape as [`Source::unreachable`], but able to mark the
    /// failure retryable through the `docs/guides/fetch.md` §3a budget
    /// (chtypes#365), with the source's own requested wait when it sent one.
    fn unreachable_retry(
        &self,
        message: String,
        retryable: bool,
        retry_after: Option<Duration>,
    ) -> Error {
        Error::SourceUnreachable {
            origin: self.describe().to_string(),
            message,
            retryable,
            retry_after,
        }
    }

    /// Open one release file for streaming: `Ok(None)` when the source has no
    /// such file, the reader and (when known) its length otherwise.
    ///
    /// # Errors
    ///
    /// [`Error::SourceUnreachable`] — offline, a transport failure, or an HTTP
    /// status other than 200/404/410. `retryable` is set (chtypes#365) for an
    /// HTTP 5xx/408/429 or any transport-level failure reaching the host at
    /// all (refused, reset, timed out, DNS — `ureq`'s `call()` does not
    /// distinguish these any further, so none are singled out); never for
    /// offline, a local-file I/O error, or any other HTTP status.
    pub(crate) fn open(&self, name: &str) -> Result<Option<Opened>> {
        match self {
            Source::Dir { base, offline, .. } => {
                if *offline {
                    return Err(self.unreachable(format!("offline — {name} was not requested")));
                }
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
                let response = request.call().map_err(|e| {
                    // A failure here never even reached a status line — refused,
                    // reset, timed out, DNS — exactly the connection-level
                    // failures chtypes#365 wants retried.
                    self.unreachable_retry(format!("GET {url}: {e}"), true, None)
                })?;
                let status = response.status().as_u16();
                match status {
                    200 => {
                        let len = response.body().content_length();
                        Ok(Some((Box::new(response.into_body().into_reader()), len)))
                    }
                    404 | 410 => Ok(None),
                    status if is_retryable_status(status) => {
                        // Retry-After is honored only for the two statuses
                        // docs/guides/fetch.md §3a documents it for (chtypes#365):
                        // 503 and 429.
                        let retry_after = if status == 503 || status == 429 {
                            response
                                .headers()
                                .get("retry-after")
                                .and_then(|v| v.to_str().ok())
                                .and_then(|v| parse_retry_after(v, std::time::SystemTime::now()))
                        } else {
                            None
                        };
                        Err(self.unreachable_retry(
                            format!("GET {url}: HTTP {status}"),
                            true,
                            retry_after,
                        ))
                    }
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

/// `true` for the HTTP statuses chtypes#365 retries: any 5xx, 408 (Request
/// Timeout) and 429 (Too Many Requests). 404 and 410 are handled before this
/// is ever consulted — they are never retried.
fn is_retryable_status(status: u16) -> bool {
    (500..600).contains(&status) || status == 408 || status == 429
}

/// A `Retry-After` header value (RFC 9110 §10.2.3): either delta-seconds or
/// an HTTP-date. `None` when it is neither.
fn parse_retry_after(value: &str, now: std::time::SystemTime) -> Option<Duration> {
    let value = value.trim();
    if let Ok(secs) = value.parse::<u64>() {
        return Some(Duration::from_secs(secs));
    }
    let when = parse_http_date(value)?;
    Some(when.duration_since(now).unwrap_or(Duration::ZERO))
}

/// Parses the one HTTP-date form every real server sends (RFC 9110 §5.6.7,
/// "the preferred format"; it is what every language's own date formatter in
/// this contract's test suites emits too): `"Sun, 06 Nov 1994 08:49:37 GMT"`.
/// The two obsolete forms RFC 9110 says a recipient MAY also accept are not
/// implemented — nothing this contract talks to (or tests against) ever
/// sends them.
fn parse_http_date(value: &str) -> Option<std::time::SystemTime> {
    // "Sun, 06 Nov 1994 08:49:37 GMT" -> ["Sun,", "06", "Nov", "1994", "08:49:37", "GMT"]
    let fields: Vec<&str> = value.split_whitespace().collect();
    let [_weekday, day, month, year, time, tz] = fields[..] else {
        return None;
    };
    if tz != "GMT" {
        return None;
    }
    let day: u32 = day.parse().ok()?;
    let month = MONTHS.iter().position(|m| *m == month)? as u32 + 1;
    let year: i64 = year.parse().ok()?;
    let mut parts = time.splitn(3, ':');
    let hour: u64 = parts.next()?.parse().ok()?;
    let minute: u64 = parts.next()?.parse().ok()?;
    let second: u64 = parts.next()?.parse().ok()?;
    let days = days_from_civil(year, month, day);
    let secs = days
        .checked_mul(86_400)?
        .checked_add_unsigned(hour * 3600 + minute * 60 + second)?;
    if secs >= 0 {
        Some(std::time::UNIX_EPOCH + Duration::from_secs(secs as u64))
    } else {
        std::time::UNIX_EPOCH.checked_sub(Duration::from_secs(secs.unsigned_abs()))
    }
}

const MONTHS: [&str; 12] = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/// Days since the Unix epoch for a proleptic-Gregorian civil date — Howard
/// Hinnant's `days_from_civil` (public domain,
/// <https://howardhinnant.github.io/date_algorithms.html#days_from_civil>),
/// used here instead of a date crate dependency for one header field three
/// HTTP statuses ever carry.
fn days_from_civil(y: i64, m: u32, d: u32) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400; // [0, 399]
    let mp = (i64::from(m) + 9) % 12; // [0, 11]
    let doy = (153 * mp + 2) / 5 + i64::from(d) - 1; // [0, 365]
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy; // [0, 146096]
    era * 146_097 + doe - 719_468
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retry_after_delta_seconds_and_http_date_both_parse() {
        let now = std::time::UNIX_EPOCH + Duration::from_secs(1_000_000);
        assert_eq!(
            parse_retry_after("120", now),
            Some(Duration::from_secs(120))
        );
        assert_eq!(parse_retry_after(" 0 ", now), Some(Duration::ZERO));
        assert_eq!(parse_retry_after("not a number or a date", now), None);

        // RFC 9110's own worked example of the preferred HTTP-date form.
        let when = parse_http_date("Sun, 06 Nov 1994 08:49:37 GMT").expect("must parse");
        assert_eq!(
            when.duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_secs(),
            784_111_777
        );
        // A date already in the past is zero, never a negative wait.
        let past = std::time::UNIX_EPOCH + Duration::from_secs(784_111_777 + 10);
        assert_eq!(
            parse_retry_after("Sun, 06 Nov 1994 08:49:37 GMT", past),
            Some(Duration::ZERO)
        );
        let future = std::time::UNIX_EPOCH + Duration::from_secs(784_111_777 - 10);
        assert_eq!(
            parse_retry_after("Sun, 06 Nov 1994 08:49:37 GMT", future),
            Some(Duration::from_secs(10))
        );
    }

    #[test]
    fn is_retryable_status_matches_5xx_408_429_only() {
        for status in [500, 502, 503, 504, 599, 408, 429] {
            assert!(is_retryable_status(status), "{status}");
        }
        for status in [200, 301, 400, 401, 403, 404, 410, 499] {
            assert!(!is_retryable_status(status), "{status}");
        }
    }

    #[test]
    fn a_url_and_a_tag_are_one_choice() {
        assert!(Source::resolve(Some("file:///x"), Some("v1"), false).is_err());
    }

    #[test]
    fn file_urls_and_plain_directories_are_local_sources() {
        match Source::resolve(Some("file:///tmp/rel"), None, false).unwrap() {
            Source::Dir { base, shown, .. } => {
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
