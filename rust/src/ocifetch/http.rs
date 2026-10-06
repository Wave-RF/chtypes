//! The HTTP transport (plan §1.1.1, §3.2 "HTTP rules", §4 "Lane Rust / HTTP").
//!
//! One blocking client, built once per [`crate::ocifetch::ensure`] call:
//! `ureq`'s rustls provider with the platform certificate-verifier plugged in
//! as the root-cert source (the system CA store, honoring `SSL_CERT_FILE`/
//! `SSL_CERT_DIR` the way the platform verifier itself does), proxy settings
//! read from the environment by `ureq` itself (`ALL_PROXY`, `HTTPS_PROXY`,
//! `NO_PROXY`), and no automatic redirect following — this module follows
//! redirects itself so it can drop `Authorization` on a cross-origin hop.
//!
//! No tokio: every call in this module blocks the calling thread.

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use super::constants;
use super::error::{Error, Result};
use super::url::Url;

/// An injectable source of time and sleep, so the conformance suite can
/// assert exact retry sleeps (plan §3.2 "Sleeps are asserted exactly")
/// without a real clock. Production code uses [`RealClock`].
pub trait Clock: Send + Sync {
    /// The current wall-clock time.
    fn now(&self) -> SystemTime;
    /// Block for (approximately) this many seconds.
    fn sleep(&self, seconds: f64);
}

/// The production clock: `SystemTime::now()` and `std::thread::sleep`.
pub struct RealClock;

impl Clock for RealClock {
    fn now(&self) -> SystemTime {
        SystemTime::now()
    }

    fn sleep(&self, seconds: f64) {
        if seconds > 0.0 {
            std::thread::sleep(Duration::from_secs_f64(seconds));
        }
    }
}

/// One HTTP response, reduced to what this module acts on.
pub struct HttpResponse {
    /// The numeric status code.
    pub status: u16,
    /// Header name/value pairs, in response order. Names keep their
    /// original case; look them up with [`HttpResponse::header`].
    pub headers: Vec<(String, String)>,
    /// The body, already capped at the caller's `max_bytes`.
    pub body: Vec<u8>,
}

impl HttpResponse {
    /// The first header matching `name`, case-insensitively.
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
}

/// A bearer token sent with every request to the configured bases, per
/// `CHTYPES_DOWNLOAD_TOKEN` (plan §1.1.1, A8). Never forwarded across a
/// redirect to a different origin, and never confused with the anonymous
/// registry token the 401/`WWW-Authenticate` flow obtains for mirrors.
#[derive(Clone, Default)]
pub struct AuthConfig {
    /// The static bearer token from `CHTYPES_DOWNLOAD_TOKEN`, if set.
    pub static_token: Option<String>,
}

/// Stands in for the package version when none is available; the header is
/// never omitted.
const USER_AGENT_DEV_VERSION: &str = "0.0.0-dev";

/// `chtypes-rust/<version>`, the `User-Agent` on every request this module
/// makes (docs/guides/fetch-v1.md section 2): delivery hosts may refuse a
/// generic library agent (ureq's own is `ureq/<version>`). The version is
/// this crate's own, from `CARGO_PKG_VERSION`.
pub fn user_agent() -> String {
    let v = option_env!("CARGO_PKG_VERSION").unwrap_or(USER_AGENT_DEV_VERSION);
    let v = if v.is_empty() {
        USER_AGENT_DEV_VERSION
    } else {
        v
    };
    format!("chtypes-rust/{v}")
}

/// The blocking HTTP client used by every `https`/`http` request this module
/// makes. `file://` bases never reach this type; see `oci.rs`'s source
/// dispatch.
pub struct Client {
    agent: ureq::Agent,
    clock: Box<dyn Clock>,
}

impl Client {
    /// Build the production client: rustls with the platform verifier as the
    /// root-cert source, no automatic redirects, and 4xx/5xx returned as
    /// ordinary responses (never `Err`) so this module can inspect the
    /// status itself.
    pub fn new() -> Self {
        Self::with_clock(Box::new(RealClock))
    }

    /// Build a client with an injected [`Clock`], for the conformance suite.
    pub fn with_clock(clock: Box<dyn Clock>) -> Self {
        let tls = ureq::tls::TlsConfig::builder()
            .root_certs(ureq::tls::RootCerts::PlatformVerifier)
            .build();
        let config = ureq::Agent::config_builder()
            .tls_config(tls)
            .http_status_as_error(false)
            .max_redirects(0)
            .user_agent(user_agent())
            .timeout_connect(Some(Duration::from_secs_f64(constants::CONNECT_TIMEOUT_S)))
            .timeout_recv_response(Some(Duration::from_secs_f64(
                constants::IDLE_READ_TIMEOUT_S,
            )))
            .build();
        Client {
            agent: ureq::Agent::new_with_config(config),
            clock,
        }
    }

    /// `now()`/`sleep()` from this client's clock.
    pub fn clock(&self) -> &dyn Clock {
        self.clock.as_ref()
    }

    /// `GET url`, following redirects (dropping `Authorization` cross-origin,
    /// up to `constants::MAX_REDIRECTS` hops) and retrying per
    /// `constants::RETRY_*` on a transient status or transport error.
    /// `extra_headers` are sent on the first hop and every same-origin hop
    /// after it; `auth` supplies `CHTYPES_DOWNLOAD_TOKEN` the same way.
    pub fn get(
        &self,
        url: &str,
        extra_headers: &[(String, String)],
        auth: &AuthConfig,
        max_bytes: u64,
    ) -> Result<HttpResponse> {
        let start_url = Url::parse(url)
            .map_err(|e| Error::SourceIncompatible(format!("bad URL {url}: {e}")))?;
        let mut attempt = 0u32;
        loop {
            attempt += 1;
            let outcome = self.one_attempt(&start_url, extra_headers, auth, max_bytes);
            match outcome {
                Ok(resp) => {
                    if !is_retry_status(resp.status) || attempt >= constants::RETRY_ATTEMPTS {
                        return Ok(resp);
                    }
                    let wait = retry_after_wait(&resp, self.clock.now())?
                        .unwrap_or_else(|| backoff_wait(attempt));
                    self.clock.sleep(wait);
                }
                Err(TransportOutcome::Retryable(e)) => {
                    if attempt >= constants::RETRY_ATTEMPTS {
                        return Err(e);
                    }
                    self.clock.sleep(backoff_wait(attempt));
                }
                Err(TransportOutcome::Fatal(e)) => return Err(e),
            }
        }
    }

    /// One full hop chain (the initial request plus up to
    /// `constants::MAX_REDIRECTS` redirects, plus the anonymous Bearer-token
    /// flow on a 401), with no retry of transient statuses — that is the
    /// caller's job.
    fn one_attempt(
        &self,
        start: &Url,
        extra_headers: &[(String, String)],
        auth: &AuthConfig,
        max_bytes: u64,
    ) -> std::result::Result<HttpResponse, TransportOutcome> {
        let mut url = start.clone();
        let mut same_origin_as_start = true;
        let mut bearer: Option<String> = None;
        let mut redirects = 0u32;
        loop {
            let mut headers: Vec<(String, String)> = extra_headers.to_vec();
            if same_origin_as_start {
                if let Some(token) = &auth.static_token {
                    headers.push(("Authorization".into(), format!("Bearer {token}")));
                }
            }
            if let Some(b) = &bearer {
                headers.retain(|(k, _)| !k.eq_ignore_ascii_case("authorization"));
                headers.push(("Authorization".into(), format!("Bearer {b}")));
            }
            let resp = self.raw_request(&url, &headers, max_bytes)?;

            if resp.status == 401 && bearer.is_none() {
                if let Some(challenge) = resp.header("WWW-Authenticate") {
                    if let Some(token) = self.anonymous_token(challenge, max_bytes)? {
                        bearer = Some(token);
                        continue;
                    }
                }
                return Ok(resp);
            }

            if (300..400).contains(&resp.status) {
                let Some(location) = resp.header("Location").map(str::to_string) else {
                    return Ok(resp);
                };
                redirects += 1;
                if redirects > constants::MAX_REDIRECTS {
                    return Err(TransportOutcome::Fatal(Error::SourceUnreachable(format!(
                        "more than {} redirects following {start}",
                        constants::MAX_REDIRECTS
                    ))));
                }
                let next = url.resolve(&location).map_err(|e| {
                    TransportOutcome::Fatal(Error::SourceUnreachable(format!(
                        "redirect Location {location:?} from {url}: {e}"
                    )))
                })?;
                same_origin_as_start = same_origin_as_start && next.same_origin(start);
                url = next;
                continue;
            }

            return Ok(resp);
        }
    }

    /// The anonymous Bearer-token flow for mirrors (plan §1.1.1): a 401
    /// carrying `WWW-Authenticate: Bearer realm="...",service="...",scope="..."`
    /// is answered by a plain `GET` of the realm (with `service`/`scope` as
    /// query parameters) returning `{"token": "..."}` or
    /// `{"access_token": "..."}`.
    fn anonymous_token(
        &self,
        challenge: &str,
        max_bytes: u64,
    ) -> std::result::Result<Option<String>, TransportOutcome> {
        let Some(params) = parse_bearer_challenge(challenge) else {
            return Ok(None);
        };
        let Some(realm) = params.get("realm") else {
            return Ok(None);
        };
        let mut token_url = realm.clone();
        let mut sep = if token_url.contains('?') { '&' } else { '?' };
        for key in ["service", "scope"] {
            if let Some(v) = params.get(key) {
                token_url.push(sep);
                token_url.push_str(key);
                token_url.push('=');
                token_url.push_str(&urlencode(v));
                sep = '&';
            }
        }
        let parsed = Url::parse(&token_url).map_err(|e| {
            TransportOutcome::Fatal(Error::SourceUnreachable(format!(
                "WWW-Authenticate realm {realm:?}: {e}"
            )))
        })?;
        let resp = self.raw_request(&parsed, &[], max_bytes)?;
        if resp.status != 200 {
            return Ok(None);
        }
        let value: serde_json::Value = serde_json::from_slice(&resp.body).map_err(|e| {
            TransportOutcome::Fatal(Error::SourceUnreachable(format!(
                "token endpoint {realm:?} returned non-JSON: {e}"
            )))
        })?;
        let token = value
            .get("token")
            .or_else(|| value.get("access_token"))
            .and_then(|v| v.as_str())
            .map(str::to_string);
        Ok(token)
    }

    /// One request, no redirect or retry handling: a single round trip.
    fn raw_request(
        &self,
        url: &Url,
        headers: &[(String, String)],
        max_bytes: u64,
    ) -> std::result::Result<HttpResponse, TransportOutcome> {
        // The one place a request is built: manifests, blobs, referrers, tag
        // lists, token exchanges and every redirect hop all pass through
        // here, so the agent is set here (replacing any caller-supplied one)
        // and the agent config above only backs it up.
        let mut builder = self.agent.get(url.to_string());
        for (k, v) in headers {
            if k.eq_ignore_ascii_case("user-agent") {
                continue;
            }
            builder = builder.header(k.as_str(), v.as_str());
        }
        builder = builder.header("User-Agent", user_agent());
        match builder.call() {
            Ok(mut resp) => {
                let status = resp.status().as_u16();
                let headers = resp
                    .headers()
                    .iter()
                    .filter_map(|(name, value)| {
                        value
                            .to_str()
                            .ok()
                            .map(|v| (name.as_str().to_string(), v.to_string()))
                    })
                    .collect();
                let body = resp
                    .body_mut()
                    .with_config()
                    .limit(max_bytes)
                    .read_to_vec()
                    .map_err(|e| {
                        TransportOutcome::Fatal(Error::ArtifactCorrupt(format!(
                            "response body from {url} exceeded {max_bytes} bytes or failed to read: {e}"
                        )))
                    })?;
                Ok(HttpResponse {
                    status,
                    headers,
                    body,
                })
            }
            Err(e) => Err(classify_transport_error(url, e)),
        }
    }
}

impl Default for Client {
    fn default() -> Self {
        Self::new()
    }
}

/// How a single hop chain failed: a transient transport problem the caller's
/// retry loop should retry, or a fatal one it should not.
enum TransportOutcome {
    Retryable(Error),
    Fatal(Error),
}

fn classify_transport_error(url: &Url, e: ureq::Error) -> TransportOutcome {
    use ureq::Error as E;
    match e {
        // A connection refused (nothing is listening) will never be fixed by
        // retrying the SAME base: it fails this base at once, consuming none
        // of the retry schedule, so the caller moves to the next base
        // without a sleep — the same rule Go's `permanentTransportError`
        // applies (`connection-refused-then-next-base`).
        E::ConnectionFailed => {
            TransportOutcome::Fatal(Error::SourceUnreachable(format!("{url}: {e}")))
        }
        E::Io(ref io) if io.kind() == std::io::ErrorKind::ConnectionRefused => {
            TransportOutcome::Fatal(Error::SourceUnreachable(format!("{url}: {e}")))
        }
        E::Timeout(_) | E::Io(_) | E::HostNotFound => {
            TransportOutcome::Retryable(Error::SourceUnreachable(format!("{url}: {e}")))
        }
        other => TransportOutcome::Fatal(Error::SourceUnreachable(format!("{url}: {other}"))),
    }
}

fn is_retry_status(status: u16) -> bool {
    constants::RETRY_STATUSES.contains(&status)
}

/// `2^(attempt-1) * RETRY_FIRST_WAIT_S`: 4, 8, 16, 32 for attempts 1-4.
fn backoff_wait(attempt: u32) -> f64 {
    constants::RETRY_FIRST_WAIT_S * constants::RETRY_MULTIPLIER.powi((attempt - 1) as i32)
}

/// `Retry-After`, honored only on the statuses `constants::RETRY_AFTER_STATUSES`
/// names. Returns `Ok(None)` when absent or the status does not use it,
/// `Err` when present but it would exceed the remaining retry budget
/// (`retry_after_over_budget: "refuse"`, plan §3.1 A6).
fn retry_after_wait(resp: &HttpResponse, now: SystemTime) -> Result<Option<f64>> {
    if !constants::RETRY_AFTER_STATUSES.contains(&resp.status) {
        return Ok(None);
    }
    let Some(raw) = resp.header("Retry-After") else {
        return Ok(None);
    };
    let seconds = if let Ok(secs) = raw.trim().parse::<f64>() {
        secs
    } else if let Some(when) = parse_http_date(raw.trim()) {
        let base = resp
            .header("Date")
            .and_then(|d| parse_http_date(d.trim()))
            .unwrap_or(now);
        when.duration_since(base)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0)
    } else {
        return Ok(None);
    };
    // The retry budget left after this attempt: the sum of the backoff waits
    // the remaining attempts would otherwise take. A generous, simple bound:
    // refuse anything past the single largest remaining backoff step times
    // the number of attempts left, which is always >= the real schedule.
    let max_reasonable = constants::RETRY_FIRST_WAIT_S
        * constants::RETRY_MULTIPLIER.powi((constants::RETRY_ATTEMPTS.max(1) - 1) as i32)
        * constants::RETRY_ATTEMPTS as f64;
    if seconds > max_reasonable || seconds < 0.0 {
        return Err(Error::SourceUnreachable(format!(
            "Retry-After {raw:?} ({seconds}s) exceeds the retry budget; refusing rather than sleeping a shortened time"
        )));
    }
    Ok(Some(seconds))
}

/// Parse `realm="...",service="...",scope="..."` from a `WWW-Authenticate:
/// Bearer ...` challenge (RFC 6750 §3), tolerant of the exact quoting and
/// spacing registries use.
fn parse_bearer_challenge(value: &str) -> Option<std::collections::HashMap<String, String>> {
    let rest = value.trim();
    let rest = rest
        .strip_prefix("Bearer")
        .or_else(|| rest.strip_prefix("bearer"))?;
    let mut map = std::collections::HashMap::new();
    for part in rest.split(',') {
        let part = part.trim();
        let Some((k, v)) = part.split_once('=') else {
            continue;
        };
        let v = v.trim().trim_matches('"');
        map.insert(k.trim().to_ascii_lowercase(), v.to_string());
    }
    if map.is_empty() { None } else { Some(map) }
}

fn urlencode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// Parse an RFC 1123 HTTP-date (`Sun, 06 Nov 1994 08:49:37 GMT`), the only
/// form `Date`/`Retry-After` use on these hosts.
fn parse_http_date(s: &str) -> Option<SystemTime> {
    let parts: Vec<&str> = s.split_whitespace().collect();
    if parts.len() != 6 || parts[5] != "GMT" {
        return None;
    }
    let day: u32 = parts[1].parse().ok()?;
    let month = match parts[2] {
        "Jan" => 1,
        "Feb" => 2,
        "Mar" => 3,
        "Apr" => 4,
        "May" => 5,
        "Jun" => 6,
        "Jul" => 7,
        "Aug" => 8,
        "Sep" => 9,
        "Oct" => 10,
        "Nov" => 11,
        "Dec" => 12,
        _ => return None,
    };
    let year: i64 = parts[3].parse().ok()?;
    let mut hms = parts[4].split(':');
    let hour: u64 = hms.next()?.parse().ok()?;
    let minute: u64 = hms.next()?.parse().ok()?;
    let second: u64 = hms.next()?.parse().ok()?;
    let days = days_from_civil(year, month, day);
    let secs = days * 86_400 + (hour * 3600 + minute * 60 + second) as i64;
    if secs < 0 {
        return None;
    }
    Some(UNIX_EPOCH + Duration::from_secs(secs as u64))
}

/// Days since the Unix epoch for a (year, month, day) civil date.
/// Howard Hinnant's `days_from_civil` algorithm (proleptic Gregorian).
fn days_from_civil(y: i64, m: u32, d: u32) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400; // [0, 399]
    let mp = (m as i64 + 9) % 12; // [0, 11]
    let doy = (153 * mp + 2) / 5 + d as i64 - 1; // [0, 365]
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy; // [0, 146096]
    era * 146_097 + doe - 719_468
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn http_date_round_trips_known_value() {
        // RFC 7231's own example.
        let t = parse_http_date("Sun, 06 Nov 1994 08:49:37 GMT").unwrap();
        let secs = t.duration_since(UNIX_EPOCH).unwrap().as_secs();
        assert_eq!(secs, 784_111_777);
    }

    #[test]
    fn bearer_challenge_parses_quoted_params() {
        let m = parse_bearer_challenge(
            r#"Bearer realm="https://auth.example/token",service="registry.example",scope="repository:x:pull""#,
        )
        .unwrap();
        assert_eq!(m.get("realm").unwrap(), "https://auth.example/token");
        assert_eq!(m.get("service").unwrap(), "registry.example");
        assert_eq!(m.get("scope").unwrap(), "repository:x:pull");
    }

    #[test]
    fn user_agent_is_chtypes_rust_with_the_crate_version() {
        let ua = user_agent();
        assert_eq!(ua, format!("chtypes-rust/{}", env!("CARGO_PKG_VERSION")));
        let (name, version) = ua.split_once('/').unwrap();
        assert_eq!(name, "chtypes-rust");
        assert!(!version.is_empty());
        assert!(
            version
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '+' | '-')),
            "version {version:?} outside [0-9A-Za-z.+-]"
        );
    }

    #[test]
    fn requests_carry_the_user_agent_on_every_hop() {
        use std::io::{Read, Write};
        use std::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let handle = std::thread::spawn(move || {
            let mut agents = Vec::new();
            for hop in 0..2 {
                let (mut sock, _) = listener.accept().unwrap();
                let mut buf = [0u8; 4096];
                let n = sock.read(&mut buf).unwrap();
                let req = String::from_utf8_lossy(&buf[..n]).to_string();
                let agent = req
                    .lines()
                    .find_map(|l| {
                        let (k, v) = l.split_once(':')?;
                        k.eq_ignore_ascii_case("user-agent")
                            .then(|| v.trim().to_string())
                    })
                    .unwrap_or_default();
                agents.push(agent);
                let resp = if hop == 0 {
                    "HTTP/1.1 302 Found\r\nLocation: /end\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                } else {
                    "HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
                };
                sock.write_all(resp.as_bytes()).unwrap();
            }
            agents
        });
        let client = Client::new();
        let resp = client
            .get(
                &format!("http://127.0.0.1:{port}/start"),
                &[("User-Agent".to_string(), "Something-Else/1".to_string())],
                &AuthConfig::default(),
                1024,
            )
            .unwrap();
        assert_eq!(resp.status, 200);
        let agents = handle.join().unwrap();
        assert_eq!(agents, vec![user_agent(), user_agent()]);
    }

    #[test]
    fn backoff_doubles_from_4() {
        assert_eq!(backoff_wait(1), 4.0);
        assert_eq!(backoff_wait(2), 8.0);
        assert_eq!(backoff_wait(3), 16.0);
        assert_eq!(backoff_wait(4), 32.0);
    }
}
