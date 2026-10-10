//! `GET /v2/<repository>/tags/list`: the tags a registry publishes, filtered
//! to the published lines (two-part tags). The CLI's `list` and `fetch --all` read it; the
//! fetch layer's resolve never does (`--frozen` makes no discovery request,
//! docs/guides/fetch-v1.md §6).

use super::constants;
use super::ensure::{Options, configured_bases};
use super::error::{Error, Result};
use super::http::{AuthConfig, Client};
use super::oci::{self, Source, VersionRequest};

#[derive(serde::Deserialize)]
struct TagList {
    #[serde(default)]
    tags: Option<Vec<String>>,
}

/// The lines (two-part tags, no leading zeros) the first base that answers
/// publishes, oldest first, in numeric order. Every other tag (three- and
/// four-part versions, aliases, the `sha256-<hex>` fallback tags, a literal
/// channel name) is dropped. A base that answers 404 is skipped; no base answering is
/// `CHTYPES_ARTIFACT_UNPUBLISHED`.
pub fn published_versions(options: &Options) -> Result<Vec<String>> {
    let bases = configured_bases(options);
    let client = Client::new();
    let auth = AuthConfig {
        static_token: options
            .token
            .clone()
            .or_else(|| std::env::var(constants::ENV_TOKEN_NAME).ok()),
    };
    let source = Source {
        bases: &bases,
        client: &client,
        auth: &auth,
    };
    let mut last: Option<Error> = None;
    for base in &bases {
        match oci::get_from_base(
            &source,
            base,
            "tags/list",
            &[],
            constants::TAGS_LIST_MAX_BYTES,
        ) {
            Ok((200, body)) => {
                let list: TagList = serde_json::from_slice(&body)?;
                let mut versions: Vec<(Vec<u64>, String)> = list
                    .tags
                    .unwrap_or_default()
                    .into_iter()
                    .filter_map(|tag| match VersionRequest::parse(&tag) {
                        Ok(r) if !r.is_literal() && r.components.len() == 2 => {
                            Some((r.components, tag))
                        }
                        _ => None,
                    })
                    .collect();
                versions.sort();
                return Ok(versions.into_iter().map(|(_, tag)| tag).collect());
            }
            Ok((404, _)) => {
                last = Some(Error::ArtifactUnpublished(format!(
                    "{base} publishes no tag list (404)"
                )));
            }
            Ok((status, _)) => {
                last = Some(oci::status_error(
                    status,
                    &format!("{base}: unexpected status {status} listing tags"),
                ));
            }
            // A retired repository (a 410, §2) is permanent: never a reason
            // to try the next base.
            Err(e @ Error::SourceRetired(_)) => return Err(e),
            Err(e) => last = Some(e),
        }
    }
    Err(last.unwrap_or_else(|| Error::ArtifactUnpublished("no base to list".to_string())))
}
