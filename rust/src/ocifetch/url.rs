//! A minimal URL type: just enough to join a `Location` header against the
//! request it answered and decide whether the result shares an origin
//! (plan §1.1.1: "dropping `Authorization` on any cross-origin hop"). This is
//! not a general-purpose URL library; `oci.rs` and `referrers.rs` build
//! registry request paths with plain string concatenation (`base` is always
//! a clean, trailing-slash-free repository root per `constants.json`).

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Url {
    pub scheme: String,
    pub host: String,
    pub port: Option<u16>,
    /// Path plus an optional `?query`, always starting with `/`.
    pub path: String,
}

impl Url {
    pub fn parse(s: &str) -> Result<Url, String> {
        let (scheme, rest) = s
            .split_once("://")
            .ok_or_else(|| format!("{s:?} has no scheme"))?;
        let scheme = scheme.to_ascii_lowercase();
        if scheme == "file" {
            // file:///abs/path — authority is always empty.
            return Ok(Url {
                scheme,
                host: String::new(),
                port: None,
                path: if rest.is_empty() {
                    "/".to_string()
                } else {
                    rest.to_string()
                },
            });
        }
        let (authority, path) = match rest.find('/') {
            Some(i) => (&rest[..i], rest[i..].to_string()),
            None => (rest, "/".to_string()),
        };
        let (host, port) = match authority.rsplit_once(':') {
            Some((h, p)) => (
                h.to_string(),
                Some(
                    p.parse::<u16>()
                        .map_err(|_| format!("bad port in {authority:?}"))?,
                ),
            ),
            None => (authority.to_string(), None),
        };
        if host.is_empty() {
            return Err(format!("{s:?} has no host"));
        }
        Ok(Url {
            scheme,
            host,
            port,
            path,
        })
    }

    /// Resolve a (possibly relative) `Location` header against this URL.
    pub fn resolve(&self, location: &str) -> Result<Url, String> {
        if location.contains("://") {
            return Url::parse(location);
        }
        if let Some(rest) = location.strip_prefix("//") {
            return Url::parse(&format!("{}://{}", self.scheme, rest));
        }
        if let Some(abs_path) = location.strip_prefix('/') {
            return Ok(Url {
                path: format!("/{abs_path}"),
                ..self.clone()
            });
        }
        // Relative to the current path's directory.
        let dir = match self.path.rfind('/') {
            Some(i) => &self.path[..=i],
            None => "/",
        };
        Ok(Url {
            path: format!("{dir}{location}"),
            ..self.clone()
        })
    }

    /// Same scheme, host and effective port (443/80 defaults applied).
    pub fn same_origin(&self, other: &Url) -> bool {
        self.scheme == other.scheme
            && self.host == other.host
            && self.effective_port() == other.effective_port()
    }

    fn effective_port(&self) -> u16 {
        self.port.unwrap_or(match self.scheme.as_str() {
            "https" => 443,
            _ => 80,
        })
    }
}

impl std::fmt::Display for Url {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        if self.scheme == "file" {
            return write!(f, "file://{}", self.path);
        }
        match self.port {
            Some(p) => write!(f, "{}://{}:{}{}", self.scheme, self.host, p, self.path),
            None => write!(f, "{}://{}{}", self.scheme, self.host, self.path),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn absolute_redirect_changes_origin() {
        let base = Url::parse("https://registry.test/v2/x/manifests/26.8").unwrap();
        let next = base.resolve("https://cdn.example/blob/1").unwrap();
        assert!(!base.same_origin(&next));
    }

    #[test]
    fn absolute_path_redirect_keeps_origin() {
        let base = Url::parse("https://registry.test/v2/x/manifests/26.8").unwrap();
        let next = base.resolve("/v2/x/blobs/sha256:abc").unwrap();
        assert!(base.same_origin(&next));
        assert_eq!(next.path, "/v2/x/blobs/sha256:abc");
    }

    #[test]
    fn scheme_relative_redirect() {
        let base = Url::parse("https://registry.test/v2/x/manifests/26.8").unwrap();
        let next = base.resolve("//other.test/path").unwrap();
        assert_eq!(next.host, "other.test");
        assert!(!base.same_origin(&next));
    }

    #[test]
    fn file_url_roundtrip() {
        let u = Url::parse("file:///tmp/trees/basic/v2/chtypes/v1").unwrap();
        assert_eq!(u.scheme, "file");
        assert_eq!(u.path, "/tmp/trees/basic/v2/chtypes/v1");
    }
}
